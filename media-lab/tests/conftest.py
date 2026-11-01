"""Shared fixtures for the Cut test-suite: tiny synthetic media made by ffmpeg."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The suite exercises every engine integration, including the ones whose model
# licence is personal / non-commercial and that a fresh install therefore keeps
# OFF (media_lab_core/engine_licences.py). Opt the test process in, as a host
# owner would in config/local.env; test_engine_licences.py covers the public
# defaults with this switched off.
os.environ.setdefault("MEDIA_LAB_PERSONAL_ENGINES", "all")
# Tests that read the Spark host's private trees carry the `spark` marker or
# import from those paths; on any other checkout they skip instead of failing on
# a FileNotFoundError. Each skip reason names the capability that is *not* being
# covered, so a green run cannot be mistaken for coverage (docs/
# HOST-DEPENDENT-TESTS.md lists the same inventory for humans).
_SPARK_ONLY_TESTS = {
    "test_pplx_ltx_co_residency.py": "the Spark host's private image-svc/ tree",
}
# Module → reason for individual `@pytest.mark.spark` cases outside that map.
_SPARK_MARKER_REASONS = {
    "test_yue2_music.py": "a live YuE2 engine on the studio host (YUE2_PORT)",
    "test_sol_h3_runtime_patch.py": "the Sol-H3 stage-1 environment and package on the studio host",
}
_SPARK_TREE_REASON = "the Spark host's private productions/ or image-svc/ tree"


def _host_capability(item):
    """What a host-only case needs, or None when this checkout can run it."""
    name = Path(str(item.fspath)).name
    if name in _SPARK_ONLY_TESTS:
        return _SPARK_ONLY_TESTS[name]
    if item.get_closest_marker("spark"):
        return _SPARK_MARKER_REASONS.get(name, _SPARK_TREE_REASON)
    return None


def pytest_collection_modifyitems(config, items):
    if (ROOT / "productions").is_dir():
        return
    for item in items:
        needs = _host_capability(item)
        if needs:
            item.add_marker(pytest.mark.skip(reason="needs " + needs))

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _ff(*args):
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-fflags", "+bitexact", *args],
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="session")
def cut_media(tmp_path_factory):
    """A 3 s video with a tone, a 2 s video, a square PNG and a 5 s mp3 — no real media."""
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe are required for Cut media tests")
    base = tmp_path_factory.mktemp("cut-media")
    _ff("-f", "lavfi", "-i", "testsrc2=s=640x360:r=24:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:d=3",
        "-c:v", "libx264", "-threads", "1", "-flags:v", "+bitexact", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-flags:a", "+bitexact", "-map_metadata", "-1", "-shortest", str(base / "a.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=0x3355aa:s=640x360:r=24:d=2",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:d=2",
        "-c:v", "libx264", "-threads", "1", "-flags:v", "+bitexact", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-flags:a", "+bitexact", "-map_metadata", "-1", "-shortest", str(base / "b.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=0xaa5533:s=480x480:d=1", "-frames:v", "1", str(base / "c.png"))
    _ff("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100:d=5", "-c:a", "libmp3lame", "-q:a", "4",
        "-map_metadata", "-1", str(base / "m.mp3"))
    return {"dir": base, "a": base / "a.mp4", "b": base / "b.mp4", "c": base / "c.png", "m": base / "m.mp3"}
