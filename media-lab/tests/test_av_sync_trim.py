"""Lip-sync trim: the decision policy and the real ffmpeg move of the sound.

The SyncNet measurement itself needs torch and the SyncNet weights, which only
the studio host has; here it is replaced by its JSON contract, while the move
it drives runs through real ffmpeg on a synthetic click track.
"""
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from media_lab_core import av_sync

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _measured(lag, conf=8.0, coverage=1.0, windows=None, ok=True):
    return {"ok": ok, "audio_lag_ms": lag, "confidence": conf, "face": {"coverage": coverage},
            "windows": windows if windows is not None else
            [{"audio_lag_ms": lag + d, "conf": 8.0, "rms_dbfs": -15.0} for d in (-5, 0, 5)]}


def test_policy_moves_a_confident_late_sound_earlier_by_its_own_lag():
    plan = av_sync.plan(_measured(31.7))
    assert plan["apply"] is True and plan["shift_ms"] == -31.7
    assert "31 ms late" in plan["reason"] or "32 ms late" in plan["reason"]
    early = av_sync.plan(_measured(-40.0))
    assert early["apply"] is True and early["shift_ms"] == 40.0


@pytest.mark.parametrize("result,why", [
    ({"ok": False, "error": "no face track"}, "no face track"),
    (_measured(30, coverage=0.5), "only 50%"),
    (_measured(30, conf=3.5), "confidence 3.5"),
    (_measured(8), "already in sync"),
    (_measured(600), "too large"),
    (_measured(30, windows=[{"audio_lag_ms": 150, "conf": 8, "rms_dbfs": -15},
                            {"audio_lag_ms": 160, "conf": 8, "rms_dbfs": -15}]), "windows disagree"),
])
def test_policy_leaves_untrustworthy_measurements_alone(result, why):
    plan = av_sync.plan(result)
    assert plan["apply"] is False and plan["shift_ms"] == 0.0 and why in plan["reason"]


def test_quiet_or_unconfident_windows_are_not_evidence():
    windows = [{"audio_lag_ms": 400, "conf": 1.0, "rms_dbfs": -15},    # no lock
               {"audio_lag_ms": 400, "conf": 9.0, "rms_dbfs": -60},    # a pause
               {"audio_lag_ms": 28, "conf": 9.0, "rms_dbfs": -14},
               {"audio_lag_ms": 33, "conf": 9.0, "rms_dbfs": -14}]
    assert av_sync.plan(_measured(30, windows=windows))["apply"] is True


def test_configuration_requires_the_python_and_every_asset(tmp_path):
    assert av_sync.configured({}) is None
    root = tmp_path / "syncnet"
    (root / "checkpoints/auxiliary").mkdir(parents=True)
    (root / "eval/syncnet").mkdir(parents=True)
    env = {"MEDIA_LAB_AV_SYNC_PYTHON": str(Path(shutil.which("python3") or "/bin/sh")),
           "MEDIA_LAB_AV_SYNC_ROOT": str(root)}
    assert av_sync.configured(env) is None                      # weights missing
    for name in ("checkpoints/auxiliary/syncnet_v2.model", "checkpoints/auxiliary/sfd_face.pth",
                 "eval/syncnet/syncnet.py"):
        (root / name).write_bytes(b"x")
    assert av_sync.configured(env)["root"] == str(root)
    assert av_sync.configured({**env, "MEDIA_LAB_AV_SYNC": "off"}) is None


def test_measure_reports_a_broken_tool_instead_of_raising(tmp_path):
    tool = {"python": "/nonexistent/python", "root": str(tmp_path)}
    result = av_sync.measure(tmp_path / "x.mp4", tool, timeout_s=10)
    assert result["ok"] is False and "could not start" in result["error"]


# ------------------------------------------------------------ the real move
def _ff(*args):
    done = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def _click_clip(path, click_at):
    """2 s, 24 fps video with silence and one 30 ms 1 kHz burst at ``click_at``."""
    _ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
        "-f", "lavfi", "-i",
        f"aevalsrc='if(between(t,{click_at},{click_at + 0.03}),0.8*sin(2*PI*1000*t),0)':s=48000:d=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", str(path))


def _onset(path):
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", "48000", "-f", "s16le", "-"], capture_output=True).stdout
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    first = next(i for i, v in enumerate(samples) if abs(v) > 8000)
    return first / 48000.0


def _video_md5(path):
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy",
                           "-f", "md5", "-"], capture_output=True, text=True).stdout.strip()


def _dur(path, stream):
    return av_sync._duration(path, stream)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg and ffprobe are required")
@pytest.mark.parametrize("click_at,shift_ms,expected", [(1.060, -60.0, 1.000), (1.000, 40.0, 1.040)])
def test_the_move_lands_the_sound_where_asked_and_keeps_the_picture(tmp_path, click_at, shift_ms, expected):
    src = tmp_path / "src.mp4"
    _click_clip(src, click_at)
    dst = tmp_path / "dst.mp4"
    shutil.copyfile(src, dst)
    av_sync.apply(src, dst, shift_ms)
    assert abs(_onset(dst) - expected) < 0.006, _onset(dst)
    assert _video_md5(dst) == _video_md5(src)                  # picture untouched
    assert abs(_dur(dst, "v:0") - _dur(src, "v:0")) < 0.01
    assert abs(_dur(dst, "a:0") - _dur(dst, "v:0")) < 0.05      # length kept


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg and ffprobe are required")
def test_sync_trim_applies_the_plan_and_keeps_a_receipt(tmp_path, monkeypatch):
    clip = tmp_path / "take.mp4"
    _click_clip(clip, 1.050)
    monkeypatch.setattr(av_sync, "configured", lambda env: {"python": "p", "root": "r"})
    monkeypatch.setattr(av_sync, "measure", lambda video, tool, fps=24.0: _measured(50.0))
    receipt = av_sync.sync_trim(clip, {})
    assert receipt["applied"] is True and receipt["shift_ms"] == -50.0
    assert receipt["measured_lag_ms"] == 50.0 and receipt["confidence"] == 8.0
    assert abs(_onset(clip) - 1.000) < 0.006
    assert not list(tmp_path.glob("*.before-sync.mp4"))


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg and ffprobe are required")
def test_a_failed_move_restores_the_untouched_take(tmp_path, monkeypatch):
    clip = tmp_path / "take.mp4"
    _click_clip(clip, 1.0)
    before = clip.read_bytes()
    monkeypatch.setattr(av_sync, "configured", lambda env: {"python": "p", "root": "r"})
    monkeypatch.setattr(av_sync, "measure", lambda video, tool, fps=24.0: _measured(50.0))

    def broken(src, dst, shift):
        Path(dst).write_bytes(b"half written")
        raise RuntimeError("disk full")
    monkeypatch.setattr(av_sync, "apply", broken)
    receipt = av_sync.sync_trim(clip, {})
    assert receipt["applied"] is False and "disk full" in receipt["reason"]
    assert clip.read_bytes() == before


def test_unconfigured_hosts_skip_the_check_quietly(tmp_path):
    receipt = av_sync.sync_trim(tmp_path / "x.mp4", {})
    assert receipt == {"checked": False, "applied": False, "reason": "lip-sync check not configured"}


def test_measure_script_has_no_download_path():
    text = (Path(av_sync.MEASURE_SCRIPT)).read_text()
    assert "check_model_and_download = lambda" in text
    assert "huggingface" not in text.lower() and "urlopen" not in text
