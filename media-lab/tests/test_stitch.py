"""The director-grade stitcher on real (synthetic) media: dead frames, J/L
cuts, dissolves, colour match, loudness, the beat, one encode."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from media_lab_core import stitch

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="needs ffmpeg")


def _ff(*args):
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def clip(path: Path, *, seconds=3.0, freq=440, volume=0.3, flash=0.0, freeze=0.0, brightness=0.0,
         size="320x180"):
    """A moving test pattern with a tone; optionally a different 'shot' flashing
    at the head, a frozen tail, or a brightness shift."""
    body = seconds - flash - freeze
    chain = f"[0:v]eq=brightness={brightness}[m]"
    inputs = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r=24:d={body}"]
    parts = ["[m]"]
    if flash:
        inputs += ["-f", "lavfi", "-i", f"color=c=0x2040ff:s={size}:r=24:d={flash}"]
        parts = ["[1:v]", "[m]"]
    video = "".join(parts) + f"concat=n={len(parts)}:v=1:a=0[c]"
    if freeze:
        video += f";[c]tpad=stop_mode=clone:stop_duration={freeze}[v]"
    else:
        video += ";[c]null[v]"
    inputs += ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}"]
    audio_index = len([x for x in inputs if x == "-i"]) - 1
    _ff(*inputs, "-filter_complex", f"{chain};{video};[{audio_index}:a]volume={volume}[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path))
    return path


def test_dead_head_and_frozen_tail_are_trimmed(tmp_path):
    src = clip(tmp_path / "a.mp4", seconds=4.0, flash=0.5, freeze=1.2)
    report = stitch.analyze_clip(src)
    assert report["head_trim"] >= 0.45, report
    assert report["tail_trim"] >= 0.7, report
    assert any("unrequested cut" in r for r in report["reasons"])
    assert any("frozen" in r for r in report["reasons"])


def test_mid_clip_cut_is_reported_not_hidden(tmp_path):
    a = tmp_path / "a.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=2", "-f", "lavfi", "-i",
        "color=c=red:s=320x180:r=24:d=2", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(a))
    report = stitch.analyze_clip(a)
    assert [round(c["time"], 2) for c in report["mid_cuts"]] == [2.0]


def test_cut_renders_once_with_crossfades_levels_and_exact_length(tmp_path):
    a = clip(tmp_path / "a.mp4", seconds=3.0, freq=330, volume=6.0)
    b = clip(tmp_path / "b.mp4", seconds=3.0, freq=550, volume=0.05, brightness=0.25)
    c = clip(tmp_path / "c.mp4", seconds=3.0, freq=660, volume=0.3)
    plan = {"shots": [{"path": str(a)},
                      {"path": str(b), "dialogue": True, "transition_in": "j_cut"},
                      {"path": str(c), "transition_in": {"kind": "dissolve", "frames": 12}}],
            "width": 320, "height": 180, "quality": "high", "fade_out_frames": 6}
    receipt = stitch.render(plan, tmp_path / "out.mp4")
    shots = receipt["plan"]["shots"]
    total = receipt["plan"]["timeline"]["total_frames"]
    lengths = [round(s["kept_seconds"] * 24) for s in shots]
    assert total == sum(lengths) - 12                       # the dissolve overlaps 12 frames
    assert receipt["duration_ok"] and receipt["encode_generations"] == 1
    assert receipt["probe"]["has_audio"] and receipt["probe"]["sample_rate"] == 48000
    j = shots[1]["audio"]
    assert j["offset_vs_picture"] < -0.2                    # the J-cut: sound arrives early
    assert any("J-cut" in r for r in shots[1]["trim_reasons"])
    assert shots[1]["gain_db"] > 10 and shots[0]["gain_db"] < 0   # quiet shot lifted, loud shot lowered
    assert shots[1]["color"] and shots[1]["color"]["gain"]  # the brighter shot is pulled back
    lufs = stitch.measure_loudness(tmp_path / "out.mp4", 0, receipt["probe"]["duration"])
    assert lufs is not None and abs(lufs - stitch.TARGET_LUFS) < 2.0
    assert "-crf" in receipt["command"] and receipt["command"][receipt["command"].index("-crf") + 1] == "18"


def test_explicit_trims_are_exact_and_never_auto_trimmed(tmp_path):
    a = clip(tmp_path / "a.mp4", seconds=3.0, flash=0.5)
    plan = {"shots": [{"path": str(a), "trim_in": 0.25, "trim_out": 2.25}], "width": 320, "height": 180,
            "fade_out_frames": 0}
    resolved = stitch.plan_cut(plan)
    shot = resolved["shots"][0]
    assert (shot["in_frame"], shot["out_frame"]) == (6, 54)
    assert shot["trim_reasons"] == []


def test_aspect_close_to_canvas_is_cropped_not_letterboxed(tmp_path):
    a = clip(tmp_path / "a.mp4", seconds=1.5, size="336x192")   # 1.75, like H3's 1344x768
    plan = {"shots": [{"path": str(a)}], "width": 320, "height": 176, "fade_out_frames": 0}
    cmd = " ".join(stitch.build_command(stitch.plan_cut(plan), tmp_path / "o.mp4"))
    assert "crop=320:176" in cmd and "pad=320" not in cmd


def test_unknown_transition_is_refused(tmp_path):
    a = clip(tmp_path / "a.mp4", seconds=1.5)
    with pytest.raises(stitch.StitchError):
        stitch.plan_cut({"shots": [{"path": str(a)}, {"path": str(a), "transition_in": "star_wipe"}]})


def test_beat_tracking_and_cuts_on_the_beat(tmp_path):
    song = tmp_path / "song.wav"
    # 120 BPM: a short decaying click every 0.5 s
    _ff("-f", "lavfi", "-i",
        "aevalsrc='0.8*sin(2*PI*1000*t)*exp(-60*mod(t\\,0.5))':s=22050:d=12", str(song))
    info = stitch.beat_times(song)
    assert info["bpm"] and abs(info["bpm"] - 120) < 3 or abs(info["bpm"] - 60) < 2
    shots = [clip(tmp_path / f"s{i}.mp4", seconds=3.0) for i in range(3)]
    plan = {"shots": [{"path": str(shots[0]), "trim_out": 2.3}, {"path": str(shots[1]), "trim_out": 2.1},
                      {"path": str(shots[2])}],
            "width": 320, "height": 180, "music": {"path": str(song), "beat_align": True}, "clip_audio": False}
    resolved = stitch.plan_cut(plan)
    beats = resolved["beats"]
    assert beats["cuts_on_beat_after"] >= beats["cuts_on_beat_before"]
    assert beats["cuts_on_beat_after"] == 2, beats
    receipt = stitch.render(plan, tmp_path / "mv.mp4", resolved=resolved)
    assert receipt["probe"]["has_audio"]


def test_color_corrections_are_clamped_and_skip_deliberate_looks():
    sigs = [{"mean": [100, 100, 100], "std": [50, 50, 50]},
            {"mean": [110, 100, 90], "std": [55, 50, 45]},
            {"mean": [100, 102, 98], "std": [50, 51, 49]},
            {"mean": [240, 30, 30], "std": [10, 10, 10]}]
    out = stitch.color_corrections(sigs, ["s"] * 4)
    assert out[1] and all(stitch.COLOR_GAIN_RANGE[0] <= g <= stitch.COLOR_GAIN_RANGE[1] for g in out[1]["gain"])
    assert out[3] == {"skipped": "differs from its scene by more than drift; left alone"}


def test_receipt_is_json_serialisable(tmp_path):
    a = clip(tmp_path / "a.mp4", seconds=2.0)
    receipt = stitch.render({"shots": [{"path": str(a)}], "width": 320, "height": 180}, tmp_path / "o.mp4")
    json.dumps(receipt)


def test_takes_are_paced_to_the_planned_length(tmp_path):
    a = clip(tmp_path / "a.mp4", seconds=5.0)
    resolved = stitch.plan_cut({"shots": [{"path": str(a), "target_seconds": 3}], "width": 320, "height": 180})
    shot = resolved["shots"][0]
    assert shot["length_frames"] == 72
    assert shot["in_frame"] > 0 and any("paced" in r for r in shot["trim_reasons"])


def test_model_written_transitions_never_break_assembly(tmp_path):
    a = clip(tmp_path / "a.mp4", seconds=2.0)
    sources = [({"transition": "fade_in"}, a, 0, 2, 2), ({"transition": "Cross-Dissolve"}, a, 0, 2, 2),
               ({"transition": "wipe"}, a, 0, 2, 2)]
    plan = stitch.legacy_board_plan({}, sources, width=320, height=180)
    assert [s["transition_in"] for s in plan["shots"]] == ["cut", "dissolve", "cut"]
    assert plan["fade_in_frames"] == 12
    stitch.plan_cut(plan)
