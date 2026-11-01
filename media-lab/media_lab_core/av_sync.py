"""Lip-sync check and trim for talking clips (stdlib only).

The H3 checkpoints put the sound a little after the lips: measured with
SyncNet on the studio host (2026-09-26), Singularity clips were 16-34 ms late
and Sol-H3 clips about 50 ms late, every one in the same direction, while the
frame count, frame rate, audio latent length, decode and mux were all correct.
A fixed offset would be wrong for any given clip (it varies 16-34 ms), so each
clip is measured and its own offset removed at the mux:

  measure()   runs runner/av_sync_measure.py (SyncNet, CPU) in the SyncNet
              environment configured in config/local.env, returns its JSON;
  plan()      decides whether the measurement is trustworthy enough to act on
              and how many milliseconds to move the sound;
  apply()     re-muxes the clip with the sound moved (video stream copied,
              sound re-encoded, length unchanged);
  sync_trim() does all three and returns a receipt for the job record.

A clip without a clear face, with low SyncNet confidence, with windows that
disagree, or with an offset too large to be the model's own lag is left
untouched and says why. Nothing here ever fails a render.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
from pathlib import Path

MEASURE_SCRIPT = Path(__file__).resolve().parents[1] / "runner" / "av_sync_measure.py"

# Decision policy (see plan()).
MIN_CONFIDENCE = 5.0        # SyncNet confidence; clean talking clips score 7-9
MIN_FACE_COVERAGE = 0.8     # share of frames with a detected face
MIN_SHIFT_MS = 12.0         # below this a move is not worth a re-encode
MAX_SHIFT_MS = 120.0        # beyond this it is not the model's lag (seen: 16-54 ms); never guess
WINDOW_MIN_CONF = 3.0       # a window counts as evidence only above this
WINDOW_MIN_DBFS = -35.0     # ... and only while someone is audibly talking
WINDOW_AGREEMENT_MS = 40.0  # windows' median must agree with the whole-clip value


def configured(env) -> dict | None:
    """The SyncNet environment from local.env keys, or None when absent/off."""
    mode = str(env.get("MEDIA_LAB_AV_SYNC") or "auto").strip().lower()
    if mode in ("off", "0", "false", "no"):
        return None
    python = str(env.get("MEDIA_LAB_AV_SYNC_PYTHON") or "").strip()
    root = str(env.get("MEDIA_LAB_AV_SYNC_ROOT") or "").strip()
    if not python or not root:
        return None
    python_path = Path(os.path.expanduser(python))
    root_path = Path(os.path.expanduser(root))
    needed = (root_path / "checkpoints/auxiliary/syncnet_v2.model",
              root_path / "checkpoints/auxiliary/sfd_face.pth",
              root_path / "eval/syncnet/syncnet.py")
    if not python_path.exists() or not all(p.is_file() for p in needed):
        return None
    return {"python": str(python_path), "root": str(root_path),
            "threads": str(env.get("MEDIA_LAB_AV_SYNC_THREADS") or "8")}


def measure(video, tool: dict, fps: float = 24.0, timeout_s: float = 900.0) -> dict:
    """Run the SyncNet measurement; returns its JSON or {"ok": False, ...}."""
    cmd = [tool["python"], str(MEASURE_SCRIPT), str(video), "--syncnet-root", tool["root"],
           "--fps", f"{fps:g}", "--threads", str(tool.get("threads") or 8)]
    env = {**os.environ, "OMP_NUM_THREADS": str(tool.get("threads") or 8),
           "CUDA_VISIBLE_DEVICES": ""}
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "sync measurement timed out"}
    except OSError as exc:
        return {"ok": False, "error": f"sync measurement could not start: {exc}"}
    lines = [ln for ln in (done.stdout or "").splitlines() if ln.startswith("{")]
    if not lines:
        tail = (done.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return {"ok": False, "error": f"sync measurement failed: {tail[0][:200]}"}
    try:
        return json.loads(lines[-1])
    except ValueError:
        return {"ok": False, "error": "sync measurement returned unreadable JSON"}


def plan(result: dict) -> dict:
    """Decide the move. Returns {"apply": bool, "shift_ms": float, "reason": str}.

    shift_ms is how far to move the sound: negative = earlier (the usual case,
    removing a late-sound lag), positive = later."""
    if not result or not result.get("ok"):
        return {"apply": False, "shift_ms": 0.0,
                "reason": (result or {}).get("error") or "no measurement"}
    lag = float(result.get("audio_lag_ms") or 0.0)
    conf = float(result.get("confidence") or 0.0)
    coverage = float((result.get("face") or {}).get("coverage") or 0.0)
    if coverage < MIN_FACE_COVERAGE:
        return {"apply": False, "shift_ms": 0.0,
                "reason": f"a face was found in only {coverage:.0%} of frames"}
    if conf < MIN_CONFIDENCE:
        return {"apply": False, "shift_ms": 0.0,
                "reason": f"SyncNet confidence {conf:.1f} is below {MIN_CONFIDENCE:.1f}"}
    if abs(lag) < MIN_SHIFT_MS:
        return {"apply": False, "shift_ms": 0.0, "reason": f"already in sync ({lag:+.0f} ms)"}
    if abs(lag) > MAX_SHIFT_MS:
        return {"apply": False, "shift_ms": 0.0,
                "reason": f"{lag:+.0f} ms is too large to be the model's own lag; left untouched"}
    good = [w for w in (result.get("windows") or [])
            if float(w.get("conf") or 0) >= WINDOW_MIN_CONF
            and float(w.get("rms_dbfs") or -120) >= WINDOW_MIN_DBFS]
    if len(good) >= 2:
        median = statistics.median(float(w["audio_lag_ms"]) for w in good)
        if abs(median - lag) > WINDOW_AGREEMENT_MS:
            return {"apply": False, "shift_ms": 0.0,
                    "reason": f"windows disagree (median {median:+.0f} ms vs {lag:+.0f} ms)"}
    return {"apply": True, "shift_ms": round(-lag, 1),
            "reason": f"sound was {abs(lag):.0f} ms {'late' if lag > 0 else 'early'}"}


def shift_command(src, dst, shift_ms: float) -> list[str]:
    """ffmpeg argv that moves the sound by shift_ms, keeping the video stream
    bit-exact and the clip's length unchanged (padding with silence)."""
    shift_s = abs(float(shift_ms)) / 1000.0
    if shift_ms < 0:   # sound earlier: drop its first |shift| ms
        audio_in = ["-ss", f"{shift_s:.4f}", "-i", str(src)]
        audio_filter = "apad"
    else:              # sound later: prepend |shift| ms of silence
        audio_in = ["-i", str(src)]
        audio_filter = f"adelay={int(round(shift_s * 1000))}:all=1,apad"
    return ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), *audio_in,
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-af", audio_filter,
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(dst)]


def _duration(path, stream: str) -> float:
    done = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
                           "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True)
    try:
        return float((done.stdout or "").strip().splitlines()[0])
    except (IndexError, ValueError):
        return 0.0


def apply(src, dst, shift_ms: float) -> None:
    tmp = Path(f"{dst}.sync.mp4")
    done = subprocess.run(shift_command(src, tmp, shift_ms), capture_output=True, text=True)
    if done.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise RuntimeError((done.stderr or "ffmpeg failed").strip()[-300:])
    video_s, audio_s = _duration(tmp, "v:0"), _duration(tmp, "a:0")
    if video_s <= 0 or audio_s <= 0 or abs(video_s - _duration(src, "v:0")) > 0.05:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("the re-muxed clip changed length or lost a stream")
    os.replace(tmp, dst)


def sync_trim(video, env, fps: float = 24.0) -> dict:
    """Measure, decide, and (when trustworthy) fix a clip in place.

    Returns a receipt: {"checked": bool, "applied": bool, "measured_lag_ms",
    "confidence", "shift_ms", "reason"}. Never raises."""
    tool = configured(env)
    if tool is None:
        return {"checked": False, "applied": False, "reason": "lip-sync check not configured"}
    result = measure(video, tool, fps=fps)
    decision = plan(result)
    receipt = {"checked": bool(result.get("ok")), "applied": False,
               "measured_lag_ms": result.get("audio_lag_ms"),
               "confidence": result.get("confidence"),
               "shift_ms": 0.0, "reason": decision["reason"]}
    if not decision["apply"]:
        return receipt
    original = Path(f"{video}.before-sync.mp4")
    try:
        shutil.copyfile(video, original)
        apply(original, video, decision["shift_ms"])
        receipt.update(applied=True, shift_ms=decision["shift_ms"])
    except Exception as exc:  # the untouched render is still a good render
        if original.is_file():
            os.replace(original, video)
        receipt["reason"] = f"trim skipped: {exc}"[:300]
    finally:
        original.unlink(missing_ok=True)
    return receipt
