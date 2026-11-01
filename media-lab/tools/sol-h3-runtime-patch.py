#!/usr/bin/env python3
"""Install, check or remove Media Lab's Sol-H3-Spark stage-1 geometry patch.

Why: the Sol-H3 stage-1 transformer runs as 52 regions compiled with
``fullgraph=True``, and its vendored FastVideo caps Dynamo at 16 cache entries.
Stock tracing specializes each region on per-request values (the denoising
step, the prompt's tile counts and sequence length, and a fresh tile buffer
per request), so every different prompt added five cache entries and the
third different prompt after the warm-up failed with FailOnRecompileLimitHit,
a sticky safety stop and a GPU recovery hold. The patch keeps those values out
of the compiled code (``patches/sol-h3-spark/geometry.py``); the attention
math and the recompile limit are unchanged. docs/SOL-H3-SPARK.md has the story.

The patch touches two files under SOL_PKG (the Sol-H3-Spark package):

  runtime/stage1_ops/regional.py   edited in five exact, pinned places
  runtime/stage1_ops/geometry.py   new, copied from patches/sol-h3-spark/

Every step is pinned by sha256: ``apply`` refuses a regional.py that is not the
known upstream file (NVlabs/Sana 8e0db4f) and ``revert`` refuses one that is not
exactly the patched file, so a hand edit or an upstream update is never
overwritten. A running Sol process keeps the code it loaded; the patch takes
effect at the next Sol-H3 load.

usage: sol-h3-runtime-patch.py status|apply|revert [--sol-pkg DIR]
SOL_PKG defaults to the SOL_PKG in config/local.env.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_SOURCE = ROOT / "patches" / "sol-h3-spark" / "geometry.py"
REGIONAL = Path("runtime/stage1_ops/regional.py")
GEOMETRY = Path("runtime/stage1_ops/geometry.py")

UPSTREAM_REGIONAL_SHA = "57ef1889894b882467c9309a70ad9395c90d27b13c93c2bb16bfe90273ad458d"
PATCHED_REGIONAL_SHA = "8a3bc1e1f042ca1eb23b79b3f5517b19aa1f08449d4e3707ef9326e0aea84dc3"
# Earlier geometry.py releases this tool may replace in place (``apply`` upgrades them).
PREVIOUS_GEOMETRY_SHAS = frozenset({
    "45bc0c5ecdb5e6652385b0ef05a04e5ed53472f8f122b95f34a872baf1b8643d",  # r22: process-lifetime buffer
})

# (upstream text, patched text): each upstream text occurs exactly once.
EDITS: tuple[tuple[str, str], ...] = (
    ("    from . import vsa as policy\n",
     ("    from . import vsa as policy\n"
      "    from . import geometry  # Media Lab: prompt-independent compiled geometry\n"
      "    from fastvideo.attention.backends import video_sparse_attn_h3 as native_vsa\n")),
    (("                variable_sizes: torch.Tensor, layer: torch.Tensor,\n"
      "                prefix_tiles: int, video_tiles: int, step: int) -> torch.Tensor:\n"),
     ("                variable_sizes: torch.Tensor, layer: torch.Tensor,\n"
      "                geometry_scalars: torch.Tensor) -> torch.Tensor:\n"
      "        step, prefix_tiles, video_tiles = geometry.scalars(geometry_scalars)\n")),
    ("    def body_fake(q, k, v, gate, variable_sizes, layer, prefix_tiles, video_tiles, step):\n",
     "    def body_fake(q, k, v, gate, variable_sizes, layer, geometry_scalars):\n"),
    (("            self._sol_h3_cpu_layer, attn_metadata.num_prefix_tiles,\n"
      "            attn_metadata.num_video_tiles, attn_metadata.current_timestep)\n"),
     "            self._sol_h3_cpu_layer, getattr(attn_metadata, geometry.SCALARS))\n"),
    ("    # Deliberately not prepare_for_regional_compile(): that hook probes SM100a.\n",
     ('    receipt["geometry"] = geometry.install(native_vsa, bodies)\n'
      "    # Deliberately not prepare_for_regional_compile(): that hook probes SM100a.\n")),
)


class PatchError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patched_text(upstream: str, edits=EDITS) -> str:
    text = upstream
    for old, new in edits:
        if text.count(old) != 1:
            raise PatchError(f"anchor not found exactly once: {old.strip()[:60]!r}")
        text = text.replace(old, new)
    return text


def upstream_text(patched: str, edits=EDITS) -> str:
    text = patched
    for old, new in reversed(edits):
        if text.count(new) != 1:
            raise PatchError(f"patched text not found exactly once: {new.strip()[:60]!r}")
        text = text.replace(new, old)
    return text


def state(sol_pkg: Path, *, upstream_sha=UPSTREAM_REGIONAL_SHA, patched_sha=PATCHED_REGIONAL_SHA,
          geometry_source: Path = GEOMETRY_SOURCE, previous_geometry=PREVIOUS_GEOMETRY_SHAS) -> str:
    """'patched', 'outdated' (an earlier geometry.py), 'upstream', or 'unknown'
    (anything else: never touched)."""
    regional = sol_pkg / REGIONAL
    if not regional.is_file():
        return "unknown"
    digest = sha256(regional.read_bytes())
    geometry = sol_pkg / GEOMETRY
    if digest == patched_sha:
        if geometry.is_file() and geometry.read_bytes() == geometry_source.read_bytes():
            return "patched"
        if geometry.is_file() and sha256(geometry.read_bytes()) in previous_geometry:
            return "outdated"
        return "unknown"
    if digest == upstream_sha and not geometry.exists():
        return "upstream"
    return "unknown"


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def apply(sol_pkg: Path, *, upstream_sha=UPSTREAM_REGIONAL_SHA, patched_sha=PATCHED_REGIONAL_SHA,
          geometry_source: Path = GEOMETRY_SOURCE, edits=EDITS) -> str:
    now = state(sol_pkg, upstream_sha=upstream_sha, patched_sha=patched_sha,
                geometry_source=geometry_source)
    if now == "patched":
        return "already patched"
    if now == "outdated":
        regional = sol_pkg / REGIONAL
        _atomic_write(sol_pkg / GEOMETRY, geometry_source.read_bytes(), regional.stat().st_mode & 0o777)
        return "patched"
    if now != "upstream":
        raise PatchError(f"{sol_pkg / REGIONAL} is neither the pinned upstream nor the patched file; "
                         "refusing to touch it")
    regional = sol_pkg / REGIONAL
    original = regional.read_bytes()
    new = patched_text(original.decode(), edits).encode()
    if sha256(new) != patched_sha:
        raise PatchError("patched regional.py does not match its pinned sha256")
    mode = regional.stat().st_mode & 0o777
    _atomic_write(sol_pkg / GEOMETRY, geometry_source.read_bytes(), mode)
    _atomic_write(regional, new, mode)
    return "patched"


def revert(sol_pkg: Path, *, upstream_sha=UPSTREAM_REGIONAL_SHA, patched_sha=PATCHED_REGIONAL_SHA,
           geometry_source: Path = GEOMETRY_SOURCE, edits=EDITS) -> str:
    now = state(sol_pkg, upstream_sha=upstream_sha, patched_sha=patched_sha,
                geometry_source=geometry_source)
    if now == "upstream":
        return "already upstream"
    if now not in ("patched", "outdated"):
        raise PatchError(f"{sol_pkg / REGIONAL} is not exactly the patched file; refusing to touch it")
    regional = sol_pkg / REGIONAL
    old = upstream_text(regional.read_bytes().decode(), edits).encode()
    if sha256(old) != upstream_sha:
        raise PatchError("reverted regional.py does not match the pinned upstream sha256")
    _atomic_write(regional, old, regional.stat().st_mode & 0o777)
    (sol_pkg / GEOMETRY).unlink()
    return "upstream"


def configured_sol_pkg() -> Path | None:
    sys.path.insert(0, str(ROOT))
    from media_lab_core import local_config
    value = (local_config.get("SOL_PKG") or "").strip()
    return Path(os.path.expanduser(value)) if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("status", "apply", "revert"))
    parser.add_argument("--sol-pkg", type=Path, help="Sol-H3-Spark package (default: SOL_PKG in config/local.env)")
    args = parser.parse_args(argv)
    sol_pkg = args.sol_pkg or configured_sol_pkg()
    if sol_pkg is None:
        print("SOL_PKG is not set; Sol-H3 is not installed on this host", file=sys.stderr)
        return 2
    try:
        if args.action == "status":
            result = state(sol_pkg)
        elif args.action == "apply":
            result = apply(sol_pkg)
        else:
            result = revert(sol_pkg)
    except PatchError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"sol_pkg": str(sol_pkg), "stage1_geometry": result,
                      "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}))
    if args.action != "status" and result in ("patched", "upstream"):
        print("takes effect at the next Sol-H3 load; a running Sol process keeps the code it loaded",
              file=sys.stderr)
    return 0 if args.action != "status" or result != "unknown" else 1


if __name__ == "__main__":
    sys.exit(main())
