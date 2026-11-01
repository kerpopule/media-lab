# Media Lab patch for Sol-H3-Spark (NVlabs/Sana, Apache-2.0), installed by
# media-lab/tools/sol-h3-runtime-patch.py into runtime/stage1_ops/geometry.py.
"""Prompt-independent VSA geometry inside the 52 compiled stage-1 regions.

The stage-1 regions are compiled with ``fullgraph=True`` and the vendored
FastVideo caps Dynamo at 16 cache entries per code object. Stock tracing
specializes every region on three per-request values, so each different
prompt (text length) added five cache entries and the fourth different
geometry in one process (warm-up plus three prompts) failed with
``FailOnRecompileLimitHit``:

* the Python ints ``current_timestep``, ``num_prefix_tiles`` and
  ``num_video_tiles`` passed to the opaque body op (one entry per step, and
  again for every text length);
* ``total_seq_length`` compared against the traced sequence length in the
  native ``tile()``, which pins that length to a constant;
* ``tile_buf_holder.buffer is None`` on the first call of every request,
  because the denoising stage builds a fresh metadata builder per request.

This seam changes none of the attention math. The three scalars travel as one
CPU int64 tensor (read only inside the eager body op, like the layer
identity), one shared holder object replaces the per-request holder (its
identity never changes, so no ``is None`` guard per request), and the
per-instance ``preprocess_qkv`` scatters into it without Python-int
comparisons. The buffer itself still lives exactly as long as it did: it is
dropped when the request's metadata builder is freed, so nothing extra stays
resident between takes (a first version kept it for the process lifetime,
which cost ~0.4 GiB of warm headroom and failed the next warm admission). Pads stay zero: a new geometry gets a freshly zeroed buffer
before any region runs (outside the compiled code), exactly when the native
``tile()`` would have cleared or reallocated it. The native FastVideo files
are not modified.
"""
import itertools
import weakref
from types import MethodType

SCALARS = "sol_h3_scalars"


def scalars(tensor):
    """(step, prefix_tiles, video_tiles) from the CPU tensor; never a GPU read."""
    if tensor.device.type != "cpu" or tuple(tensor.shape) != (3,):
        raise RuntimeError("stage-1 geometry scalars must be a CPU tensor of three ints")
    step, prefix, video = (int(v) for v in tensor.tolist())
    return step, prefix, video


def adopt(metadata, holder):
    """Eager, per build(): shared holder, tensor scalars, zeroed buffer on a new geometry."""
    import torch
    if metadata.tile_elems != 64:
        raise RuntimeError("the stage-1 geometry seam supports only the official tile-64 route")
    metadata.tile_buf_holder = holder
    setattr(metadata, SCALARS, torch.tensor(
        [int(metadata.current_timestep), int(metadata.num_prefix_tiles), int(metadata.num_video_tiles)],
        dtype=torch.int64, device="cpu"))
    if holder.untile_geometry is not metadata.untile_combined_index:
        if holder.buffer is not None:
            batch, _, heads, dim = holder.buffer.shape
            holder.buffer = holder.buffer.new_zeros(
                (batch, metadata.variable_block_sizes.numel() * metadata.tile_elems, heads, dim))
        holder.untile_geometry = metadata.untile_combined_index
    return metadata


def preprocess_qkv(self, qkv, attn_metadata):
    """Scatter rows into the shared padded tile buffer (pads stay zero)."""
    holder = attn_metadata.tile_buf_holder
    buffer = holder.buffer
    if buffer is None:
        # First region call of the process only; later geometries are sized in adopt().
        buffer = qkv.new_zeros((qkv.shape[0], attn_metadata.variable_block_sizes.shape[0]
                                * attn_metadata.tile_elems, qkv.shape[2], qkv.shape[3]))
        holder.buffer = buffer
    buffer[:, attn_metadata.untile_combined_index] = qkv
    return buffer


def release(holder, token=None):
    """Drop the request's tile buffer (the stock per-request lifetime).

    With ``token``: only if that request still owns the holder, so a late
    finalizer never clears a newer request's buffer. A compiled call already
    holding the old tensor keeps it alive until it returns; the next call
    allocates a fresh zeroed buffer."""
    if token is None or holder.owner == token:
        holder.buffer = None
        holder.untile_geometry = None


def install(native, bodies):
    """Install once per stage-1 worker, after the VSA audit and before compile."""
    if getattr(native, "_sol_h3_geometry_installed", False):
        raise RuntimeError("stage-1 geometry seam installed twice")
    holder = native._MiniMaxH3VSATileBufferHolder()
    holder.owner = None
    builder = native.MiniMaxH3VSAMetadataBuilder
    original_build = builder.build
    tokens = itertools.count(1)

    def build(self, *args, **kwargs):
        if getattr(self, "_sol_h3_geometry_token", None) is None:
            # A new request's builder: the buffer is its own, as in stock FastVideo.
            self._sol_h3_geometry_token = token = next(tokens)
            release(holder)
            holder.owner = token
            weakref.finalize(self, release, holder, token)
        return adopt(original_build(self, *args, **kwargs), holder)

    builder.build = build
    for impl in bodies:
        if type(impl).__name__ != "MiniMaxH3VSAImpl" or impl._regional_compile_sm100a_enabled is True:
            raise RuntimeError("the geometry seam requires the SM121 VSA route without the sm_100a pair tile")
        impl.preprocess_qkv = MethodType(preprocess_qkv, impl)
    native._sol_h3_geometry_installed = True
    return {"shared_tile_holder": True, "tile_buffer_lifetime": "per request (builder)", "tensor_scalars": ["current_timestep", "num_prefix_tiles", "num_video_tiles"],
            "preprocess_bodies": len(bodies), "attention_math_changed": False}
