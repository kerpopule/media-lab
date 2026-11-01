#!/usr/bin/env python3
"""CPU probe: does a Sol-H3 stage-1 region stay under Dynamo's recompile limit?

Run it with the Sol stage-1 interpreter on the studio host, from any directory:

    ~/.local/share/sol-h3-spark/envs/stage1/bin/python tools/sol_h3_recompile_probe.py \\
        --sol-pkg "$SOL_PKG" stock|patched [--prompts 14]

It drives the installed FastVideo VSA-H3 metadata builder and ``tile()`` through
one fullgraph-compiled region the way the denoising stage does (a fresh builder
per request, four steps, several blocks sharing one code object, a different text
length per prompt) with the worker's own recompile limit. Dynamo's backend is
``eager``, so nothing is compiled for or run on a GPU: the probe measures guards
only, on CPU, in seconds.

* ``stock`` reproduces the production failure: 5 cache entries per different
  prompt, FailOnRecompileLimitHit on the 4th different geometry.
* ``patched`` uses patches/sol-h3-spark/geometry.py and additionally checks that
  the padded buffer handed to attention equals the stock ``tile()`` result
  (pads zero) and that the step and tile counts arrive intact.

Prints one JSON line; exit 0 when the mode behaved as described above.
"""
# No `from __future__ import annotations`: torch.library.custom_op infers its
# schema from real annotation objects.
import argparse
import gc
import importlib.util
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY = ROOT / "patches" / "sol-h3-spark" / "geometry.py"
LENGTHS = (211, 237, 301, 356, 402, 433, 517, 190, 260, 611, 237, 88, 1000, 211)


def run(mode, sol_pkg, prompts):
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    sys.path.insert(0, str(sol_pkg / "dependencies" / "FastVideo"))
    import torch
    # The CUDA kernel package cannot initialise without a GPU; attention kernels
    # are opaque custom ops in the worker, so the probe never needs them.
    sys.modules["fastvideo_kernel"] = None
    from fastvideo.attention.backends import video_sparse_attn_h3 as vsa
    from fastvideo.forward_context import get_forward_context, set_forward_context
    from fastvideo.layers.lora import linear as _worker_limit  # noqa: F401  (sets recompile_limit)
    from torch._dynamo.eval_frame import _debug_get_cache_entry_list

    impl = vsa.MiniMaxH3VSAImpl(num_heads=2, head_size=8, causal=False, softmax_scale=1.0,
                                prefix="blocks.0.attn")
    geometry = None
    if mode == "patched":
        spec = importlib.util.spec_from_file_location("sol_h3_geometry_probe", GEOMETRY)
        geometry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(geometry)
        geometry.install(vsa, [impl])
    seen = []

    @torch.library.custom_op("sol_h3_probe::body_ints", mutates_args=(), device_types="cpu")
    def body_ints(q: torch.Tensor, sizes: torch.Tensor, prefix_tiles: int, video_tiles: int,
                  step: int) -> torch.Tensor:
        return q.clone()

    @body_ints.register_fake
    def _(q, sizes, prefix_tiles, video_tiles, step):
        return q.new_empty(q.shape)

    @torch.library.custom_op("sol_h3_probe::body_tensor", mutates_args=(), device_types="cpu")
    def body_tensor(q: torch.Tensor, sizes: torch.Tensor, scalars: torch.Tensor) -> torch.Tensor:
        seen.append(geometry.scalars(scalars))
        return q.clone()

    @body_tensor.register_fake
    def _(q, sizes, scalars):
        return q.new_empty(q.shape)

    def region(x):
        md = get_forward_context().attn_metadata
        tiled = impl.preprocess_qkv(x, md)
        if geometry is not None:   # the patched regional.py body_forward
            out = body_tensor(tiled, md.variable_block_sizes, getattr(md, geometry.SCALARS))
        else:                      # the upstream regional.py body_forward
            out = body_ints(tiled, md.variable_block_sizes, md.num_prefix_tiles, md.num_video_tiles,
                            md.current_timestep)
        return impl.postprocess_output(out, md) * 2

    compiled = torch.compile(region, fullgraph=True, backend="eager")
    dit, audio, blocks, steps = (8, 4, 4), 40, 3, 4
    result = {"mode": mode, "torch": torch.__version__,
              "recompile_limit": torch._dynamo.config.recompile_limit, "prompts": []}
    for index, n_text in enumerate((LENGTHS * 2)[:prompts]):
        builder = vsa.MiniMaxH3VSAMetadataBuilder()   # per request, like the denoising stage
        x = torch.randn(1, n_text + audio + math.prod(dit), 2, 8)
        try:
            for step in range(steps):
                md = builder.build(current_timestep=step, raw_latent_shape=dit, patch_size=(1, 1, 1),
                                   VSA_sparsity=0.9, prefix_segments=(n_text, 0, audio),
                                   device=torch.device("cpu"), exempt=True, dense_layers=(), tile_size=64)
                with set_forward_context(current_timestep=step, attn_metadata=md):
                    for _ in range(blocks):
                        if not torch.equal(compiled(x), x * 2):
                            raise AssertionError("tile/untile round trip changed the rows")
                        if geometry is not None:
                            fields = {f: getattr(md, f) for f in md.__dataclass_fields__ if f != "tile_buf_holder"}
                            stock = vsa.MiniMaxH3VSAMetadata(**fields,
                                                            tile_buf_holder=vsa._MiniMaxH3VSATileBufferHolder())
                            if not torch.equal(md.tile_buf_holder.buffer, vsa.MiniMaxH3VSAImpl.tile(impl, x, stock)):
                                raise AssertionError("padded buffer differs from the stock tile() result")
                if geometry is not None and seen[-blocks:] != [(step, md.num_prefix_tiles, md.num_video_tiles)] * blocks:
                    raise AssertionError(f"scalars arrived as {seen[-1]}")
        except Exception as exc:  # noqa: BLE001 - the probe reports what failed
            result["prompts"].append({"text_tokens": n_text, "error": type(exc).__name__})
            break
        row = {"text_tokens": n_text, "cache_entries": len(_debug_get_cache_entry_list(region.__code__))}
        if geometry is not None:
            holder = md.tile_buf_holder
            del builder, md
            gc.collect()
            # the stock lifetime: nothing stays resident once the request's builder is gone
            row["buffer_released"] = holder.buffer is None
        result["prompts"].append(row)
    failed = [p for p in result["prompts"] if "error" in p]
    if mode == "stock":
        result["ok"] = bool(failed) and failed[0]["error"] == "FailOnRecompileLimitHit"
    else:
        entries = [p["cache_entries"] for p in result["prompts"] if "cache_entries" in p]
        released = all(p.get("buffer_released") for p in result["prompts"])
        result["ok"] = (not failed and released and len(entries) == prompts and max(entries) <= 6
                        and entries[-1] == entries[len(entries) // 2])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("stock", "patched"))
    parser.add_argument("--sol-pkg", type=Path, required=True)
    parser.add_argument("--prompts", type=int, default=14)
    args = parser.parse_args(argv)
    result = run(args.mode, args.sol_pkg.expanduser(), args.prompts)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
