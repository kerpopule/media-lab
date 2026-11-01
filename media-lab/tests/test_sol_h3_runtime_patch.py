"""Sol-H3 stage-1 geometry patch: installer safety, seam logic, and (on the
studio host) the CPU probe that reproduces the third-prompt stop and shows the
patched regions stop growing."""
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent
TOOL = SRC / "tools" / "sol-h3-runtime-patch.py"
PROBE = SRC / "tools" / "sol_h3_recompile_probe.py"
GEOMETRY = SRC / "patches" / "sol-h3-spark" / "geometry.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load(TOOL, "sol_h3_runtime_patch")


def upstream_fixture():
    """A stand-in regional.py containing every upstream anchor exactly once."""
    return "# header\n" + "".join(old + "    # between\n" for old, _new in tool.EDITS)


def fake_pkg(tmp_path, text):
    pkg = tmp_path / "Sol-H3-Spark"
    (pkg / "runtime" / "stage1_ops").mkdir(parents=True)
    (pkg / tool.REGIONAL).write_text(text)
    return pkg


def pins(text):
    return {"upstream_sha": tool.sha256(text.encode()),
            "patched_sha": tool.sha256(tool.patched_text(text).encode())}


def test_edits_round_trip_and_touch_only_the_body_op_seam():
    text = upstream_fixture()
    patched = tool.patched_text(text)
    assert tool.upstream_text(patched) == text
    assert "geometry.install(native_vsa, bodies)" in patched
    assert "attn_metadata.current_timestep" not in patched
    assert "prefix_tiles: int" not in patched


def test_apply_is_pinned_idempotent_and_reversible(tmp_path):
    text = upstream_fixture()
    pkg = fake_pkg(tmp_path, text)
    kw = pins(text)
    assert tool.state(pkg, **kw) == "upstream"
    assert tool.apply(pkg, edits=tool.EDITS, **kw) == "patched"
    assert tool.state(pkg, **kw) == "patched"
    assert (pkg / tool.GEOMETRY).read_bytes() == GEOMETRY.read_bytes()
    assert tool.apply(pkg, edits=tool.EDITS, **kw) == "already patched"
    assert tool.revert(pkg, edits=tool.EDITS, **kw) == "upstream"
    assert (pkg / tool.REGIONAL).read_text() == text
    assert not (pkg / tool.GEOMETRY).exists()


def test_unknown_regional_is_never_touched(tmp_path):
    text = upstream_fixture()
    kw = pins(text)
    pkg = fake_pkg(tmp_path, text + "# a local hand edit\n")
    with pytest.raises(tool.PatchError):
        tool.apply(pkg, edits=tool.EDITS, **kw)
    with pytest.raises(tool.PatchError):
        tool.revert(pkg, edits=tool.EDITS, **kw)
    assert (pkg / tool.REGIONAL).read_text().endswith("# a local hand edit\n")
    assert not (pkg / tool.GEOMETRY).exists()


def test_pinned_shas_are_the_real_upstream_and_patched_files():
    # the real pins describe different files and the patched pin is derived, not typed
    assert tool.UPSTREAM_REGIONAL_SHA != tool.PATCHED_REGIONAL_SHA
    assert len(tool.PATCHED_REGIONAL_SHA) == 64 and "@" not in tool.PATCHED_REGIONAL_SHA


# --- the seam itself, with a tiny stand-in for torch (CI has no torch) --------

class FakeTensor:
    def __init__(self, shape=(), values=None, device="cpu"):
        self.shape = tuple(shape)
        self.values = values
        self.device = types.SimpleNamespace(type=device)

    def tolist(self):
        return list(self.values)

    def numel(self):
        n = 1
        for d in self.shape:
            n *= d
        return n

    def new_zeros(self, shape):
        return FakeTensor(shape)


fake_torch = types.SimpleNamespace(int64="int64",
                                   tensor=lambda values, dtype, device: FakeTensor((len(values),), values, device))


def metadata(step, prefix, video, geometry_id, tiles):
    return types.SimpleNamespace(tile_elems=64, current_timestep=step, num_prefix_tiles=prefix,
                                 num_video_tiles=video, untile_combined_index=geometry_id,
                                 variable_block_sizes=FakeTensor((tiles,)), tile_buf_holder=None)


def test_adopt_shares_one_buffer_and_zeroes_it_only_for_a_new_geometry():
    geometry = load(GEOMETRY, "sol_h3_geometry_seam")
    holder = types.SimpleNamespace(buffer=None, untile_geometry=None)
    first, second = object(), object()
    with mock.patch.dict(sys.modules, {"torch": fake_torch}):
        md = geometry.adopt(metadata(0, 5, 40, first, 45), holder)
        assert md.tile_buf_holder is holder
        assert geometry.scalars(getattr(md, geometry.SCALARS)) == (0, 5, 40)
        holder.buffer = FakeTensor((4, 45 * 64, 2, 8))          # made by the first region call
        kept = holder.buffer
        geometry.adopt(metadata(3, 5, 40, first, 45), holder)   # same geometry, next step
        assert holder.buffer is kept
        geometry.adopt(metadata(0, 7, 40, second, 47), holder)  # a new prompt
        assert holder.buffer is not kept and holder.buffer.shape == (4, 47 * 64, 2, 8)
        with pytest.raises(RuntimeError):
            geometry.adopt(types.SimpleNamespace(**{**vars(metadata(0, 1, 1, first, 2)), "tile_elems": 256}),
                           holder)
    with pytest.raises(RuntimeError):
        geometry.scalars(FakeTensor((3,), [0, 1, 2], device="cuda"))


def test_preprocess_has_no_python_int_comparison_in_the_traced_path():
    source = GEOMETRY.read_text()
    body = source[source.index("def preprocess_qkv"):source.index("def install")]
    assert "total_seq_length" not in body and "current_timestep" not in body
    assert "buffer[:, attn_metadata.untile_combined_index] = qkv" in body


# --- studio host: the real FastVideo code, CPU only ---------------------------

def _stage1_python():
    root = Path(os.path.expanduser(os.environ.get("SOL_ROOT", "~/.local/share/sol-h3-spark")))
    python = root / "envs" / "stage1" / "bin" / "python"
    pkg = os.environ.get("SOL_PKG") or ""
    if not pkg:
        env = SRC / "config" / "local.env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("SOL_PKG="):
                    pkg = line.split("=", 1)[1].strip().strip('"')
    pkg = Path(os.path.expanduser(pkg)) if pkg else None
    return python, pkg


@pytest.mark.spark
@pytest.mark.parametrize("mode", ["stock", "patched"])
def test_probe_reproduces_the_stop_and_the_patch_plateaus(mode):
    python, pkg = _stage1_python()
    if not python.exists() or pkg is None or not pkg.exists():
        pytest.skip("needs the Sol-H3 stage-1 environment on the studio host")
    run = subprocess.run([str(python), str(PROBE), mode, "--sol-pkg", str(pkg)],
                         capture_output=True, text=True, timeout=900, check=False)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]


def test_outdated_geometry_is_upgraded_in_place_and_revertible(tmp_path):
    text = upstream_fixture()
    pkg = fake_pkg(tmp_path, text)
    kw = pins(text)
    assert tool.apply(pkg, edits=tool.EDITS, **kw) == "patched"
    old = b"# an earlier release of geometry.py\n"
    (pkg / tool.GEOMETRY).write_bytes(old)
    assert tool.state(pkg, **kw) == "unknown"          # not a known release: never touched
    assert tool.state(pkg, previous_geometry={tool.sha256(old)}, **kw) == "outdated"
    with mock.patch.object(tool, "PREVIOUS_GEOMETRY_SHAS", frozenset({tool.sha256(old)})):
        assert tool.state(pkg, previous_geometry=tool.PREVIOUS_GEOMETRY_SHAS, **kw) == "outdated"
    assert tool.sha256(GEOMETRY.read_bytes()) not in tool.PREVIOUS_GEOMETRY_SHAS


def test_request_buffer_is_released_when_its_builder_goes_away():
    geometry = load(GEOMETRY, "sol_h3_geometry_lifetime")

    class Builder:
        def build(self, **kw):
            return metadata(kw["step"], 5, 40, kw["geometry"], 45)

    native = types.SimpleNamespace(_MiniMaxH3VSATileBufferHolder=lambda: types.SimpleNamespace(
        buffer=None, untile_geometry=None), MiniMaxH3VSAMetadataBuilder=Builder)
    with mock.patch.dict(sys.modules, {"torch": fake_torch}):
        geometry.install(native, [])
        first, g = Builder(), object()
        md = first.build(step=0, geometry=g)
        holder = md.tile_buf_holder
        holder.buffer = FakeTensor((4, 45 * 64, 2, 8))
        second = Builder()
        md2 = second.build(step=0, geometry=g)          # a new request starts clean
        assert md2.tile_buf_holder is holder and holder.buffer is None
        holder.buffer = FakeTensor((4, 45 * 64, 2, 8))
        del first, md                                    # a late finalizer of the old request...
        import gc
        gc.collect()
        assert holder.buffer is not None                 # ...never clears the new one
        del second, md2
        gc.collect()
        assert holder.buffer is None                     # the request's own builder going away does
