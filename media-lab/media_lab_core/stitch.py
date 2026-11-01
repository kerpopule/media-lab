"""Director-grade stitching: the editor's half of "director school".

The old storyboard assembler butted every clip together with a hard ``concat``,
padded H3's 1344x768 frames into a 1280x704 canvas (black slivers left and
right), left each generator's dead head and tail frames in, never matched
colour or loudness, never faded audio at a seam (clicks), truncated the song
without a fade, ignored the music's beat, and re-encoded at CRF 23 "fast".
Cut's preview renders (CRF 30 "veryfast") were then handed over as the film.

This module plans and renders one cut from a list of shots:

* **Dead frames**: black frames, frozen or "settling" tails and unrequested
  internal cuts near a clip's head or tail are trimmed. An unrequested cut in
  the middle of a clip is reported (the shot needs a re-render), never hidden.
* **Colour**: shots in the same scene are pulled part of the way towards the
  scene's median colour (per-channel gain/offset, clamped), so one shot that
  came out greener or darker does not jump.
* **Loudness**: each shot is measured (EBU R128) and levelled, then the whole
  mix gets a two-pass linear loudnorm to -16 LUFS / -1.5 dBTP.
* **Seams**: every picture cut has an audio crossfade (equal power), J and L
  cuts move the audio edit ahead of or behind the picture, dissolves and fades
  through black only where the plan asks for them.
* **Music**: beat-tracked; hard cuts are nudged onto the nearest beat when the
  shots have the frames to allow it; the song fades out instead of stopping.
* **One generation**: sources are decoded once and encoded once, at a
  delivery quality (CRF 18 by default), constant frame rate and size.

Every decision lands in a receipt so a reviewer can see exactly what was
trimmed, recoloured, levelled and moved, and why.

numpy is used for analysis when it is installed (it is on every studio host
and in CI). Without it the cut still renders, and the receipt says which
analysis steps were skipped.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any

try:  # analysis only; rendering never needs it
    import numpy as np
except Exception:  # pragma: no cover - exercised only on hosts without numpy
    np = None

SCHEMA = "media_lab.director_cut.v1"
RECEIPT_SCHEMA = "media_lab.director_cut_receipt.v1"

QUALITY = {
    # name: (x264 preset, crf) — "preview" exists for drafts and is labelled
    # as such in the receipt; nothing below it is ever a delivery.
    "preview": ("veryfast", 26),
    "high": ("medium", 18),
    "master": ("slow", 16),
}
DELIVERY_QUALITIES = {"high", "master"}

# Transitions a director can ask for on the way INTO a shot.
HARD_CUTS = {"cut", "cut_on_action", "match_cut", "smash_cut", "j_cut", "l_cut"}
SOFT_CUTS = {"dissolve", "fade_through_black"}
TRANSITIONS = HARD_CUTS | SOFT_CUTS
DEFAULT_TRANSITION_FRAMES = {"dissolve": 12, "fade_through_black": 16}
DEFAULT_AUDIO_OFFSET_FRAMES = {"j_cut": -8, "l_cut": 8}
# Audio crossfade width at a hard cut, in seconds. Two to three frames of
# overlap is the ordinary editor's default and is what kills seam clicks.
AUDIO_XFADE_S = {"cut": 0.10, "cut_on_action": 0.08, "match_cut": 0.10,
                 "smash_cut": 0.02, "j_cut": 0.12, "l_cut": 0.12}
DECLICK_S = 0.012

MIN_SHOT_S = 1.0
HEAD_WINDOW_S = 1.5
TAIL_WINDOW_S = 1.5
HOLD_ALLOWANCE_S = 0.30
MAX_AUTO_TRIM_FRACTION = 0.45
ASPECT_CROP_TOLERANCE = 0.12

TARGET_LUFS = -16.0
TARGET_TP = -1.5
TARGET_LRA = 11.0
SHOT_TARGET_DIALOGUE_LUFS = -20.0
SHOT_TARGET_AMBIENCE_LUFS = -26.0
SHOT_GAIN_LIMIT_DB = 12.0

COLOR_STRENGTH = 0.6
COLOR_GAIN_RANGE = (0.82, 1.22)
COLOR_OFFSET_LIMIT = 20.0

Runner = Callable[..., subprocess.CompletedProcess]


class StitchError(RuntimeError):
    """A plan or render problem the caller must show, never swallow."""


# ----------------------------------------------------------------- utilities

def _run(cmd: Sequence[str], *, timeout: int = 600, input_bytes: bytes | None = None,
         text: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, timeout=timeout,
                          input=input_bytes, text=text, check=False)


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _r6(x: float) -> float:
    return round(float(x), 6)


def _frames(seconds: float, fps: int) -> int:
    return int(round(float(seconds) * fps))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path: str | Path) -> dict[str, Any]:
    """Duration, size, fps and audio facts for one media file."""
    result = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                   "-show_format", str(path)], timeout=60, text=True)
    if result.returncode != 0:
        raise StitchError(f"ffprobe could not read {Path(path).name}: {result.stderr.strip()[:200]}")
    data = json.loads(result.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}

    def _dur(stream):
        try:
            return float((stream or {}).get("duration") or 0)
        except (TypeError, ValueError):
            return 0.0

    fps = 0.0
    if video:
        num, _, den = str(video.get("r_frame_rate") or "0/1").partition("/")
        try:
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    duration = _dur(video) or float(fmt.get("duration") or 0)
    return {
        "path": str(path),
        "duration": duration,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "fps": fps,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "audio_duration": _dur(audio) if audio else 0.0,
        "sample_rate": int((audio or {}).get("sample_rate") or 0),
        "video_bitrate": int((video or {}).get("bit_rate") or 0),
    }


# ------------------------------------------------------------ frame analysis

def gray_frames(path: str | Path, *, width: int = 64, height: int = 36,
                start: float | None = None, end: float | None = None):
    """Every frame as a small grayscale array (numpy), or None without numpy."""
    if np is None:
        return None
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    if end is not None:
        cmd += ["-to", f"{end:.6f}"]
    cmd += ["-i", str(path), "-an", "-vf", f"scale={width}:{height}:flags=area,format=gray",
            "-f", "rawvideo", "-"]
    result = _run(cmd, timeout=600)
    if result.returncode != 0 or not result.stdout:
        return None
    frame_bytes = width * height
    count = len(result.stdout) // frame_bytes
    return np.frombuffer(result.stdout[:count * frame_bytes], np.uint8).reshape(
        count, height, width).astype(np.float32)


def rgb_samples(path: str | Path, times: Iterable[float], *, width: int = 96, height: int = 54):
    """Small RGB frames at the given times (numpy array list)."""
    if np is None:
        return []
    out = []
    for t in times:
        result = _run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.0, t):.6f}", "-i", str(path),
                       "-frames:v", "1", "-an", "-vf", f"scale={width}:{height}:flags=area",
                       "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], timeout=60)
        if result.returncode == 0 and len(result.stdout) >= width * height * 3:
            out.append(np.frombuffer(result.stdout[:width * height * 3], np.uint8)
                       .reshape(height, width, 3).astype(np.float32))
    return out


def audio_envelope(path: str | Path, *, rate: int = 8000, hop_s: float = 0.02,
                   start: float | None = None, end: float | None = None):
    """(times, rms_db) envelope of the audio (mono), or (None, None)."""
    if np is None:
        return None, None
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    if end is not None:
        cmd += ["-to", f"{end:.6f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    result = _run(cmd, timeout=600)
    if result.returncode != 0 or not result.stdout:
        return None, None
    samples = np.frombuffer(result.stdout[: len(result.stdout) // 2 * 2], np.int16).astype(np.float32) / 32768.0
    hop = max(1, int(rate * hop_s))
    n = len(samples) // hop
    if n == 0:
        return None, None
    blocks = samples[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1) + 1e-12)
    return (np.arange(n) * hop_s + hop_s / 2), 20 * np.log10(rms + 1e-9)


def pcm_mono(path: str | Path, *, rate: int = 22050, start: float | None = None,
             end: float | None = None):
    if np is None:
        return None
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    if end is not None:
        cmd += ["-to", f"{end:.6f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    result = _run(cmd, timeout=600)
    if result.returncode != 0 or not result.stdout:
        return None
    return np.frombuffer(result.stdout[: len(result.stdout) // 2 * 2], np.int16).astype(np.float32) / 32768.0


def _motion_series(frames):
    """Mean absolute difference between consecutive frames."""
    if frames is None or len(frames) < 2:
        return None
    return np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))


def find_internal_cuts(diffs, fps: float, *, ratio: float = 6.0, floor: float = 10.0) -> list[dict]:
    """Frames where the picture jumps: a hard cut the director never asked for.

    A generated clip moves smoothly; a cut is one frame whose difference is a
    spike far above the clip's own median motion (measured: the diner's
    bf8247d36fee jumps 29.2 at frame 27 against a 1.7 median).
    """
    if diffs is None or len(diffs) < 3:
        return []
    med = float(np.median(diffs)) or 0.01
    cuts = []
    for i, value in enumerate(diffs):
        value = float(value)
        if value < max(floor, ratio * med):
            continue
        lo = max(0, i - 3)
        hi = min(len(diffs), i + 4)
        neighbours = [float(diffs[j]) for j in range(lo, hi) if j != i]
        local = (sum(neighbours) / len(neighbours)) if neighbours else med
        if value >= 3.0 * max(local, 0.01):
            cuts.append({"frame": i + 1, "time": _r6((i + 1) / fps), "score": round(value / med, 1)})
    return cuts


def _runs(mask) -> list[tuple[int, int]]:
    runs, start = [], None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def analyze_clip(path: str | Path, *, fps_hint: float = 24.0, has_dialogue: bool = False) -> dict[str, Any]:
    """Everything the editor needs to know about one shot before cutting it."""
    info = probe(path)
    fps = info["fps"] or fps_hint
    report: dict[str, Any] = {"probe": info, "fps": fps, "skipped": []}
    if np is None:
        report["skipped"].append("numpy missing: no dead-frame, colour or speech analysis")
        report.update({"head_trim": 0.0, "tail_trim": 0.0, "reasons": [], "internal_cuts": [],
                       "mid_cuts": [], "speech_end": None, "luma": None})
        return report
    frames = gray_frames(path)
    diffs = _motion_series(frames)
    duration = info["duration"] or (len(frames) / fps if frames is not None else 0.0)
    reasons: list[str] = []
    head, tail = 0.0, 0.0
    cuts = find_internal_cuts(diffs, fps)
    report["internal_cuts"] = cuts
    mid_cuts = []
    for cut in cuts:
        t = cut["time"]
        if t <= HEAD_WINDOW_S:
            head = max(head, t)
            reasons.append(f"head: unrequested cut at {t:.2f}s (a different shot flashes first); trimmed past it")
        elif duration - t <= TAIL_WINDOW_S:
            tail = max(tail, duration - t)
            reasons.append(f"tail: unrequested cut at {t:.2f}s; trimmed before it")
        else:
            mid_cuts.append(cut)
    report["mid_cuts"] = mid_cuts
    luma = None
    if frames is not None and len(frames):
        luma = frames.mean(axis=(1, 2))
        report["luma"] = round(float(luma.mean()), 2)
        black = luma < 0.07 * 255
        for s, e in _runs(black):
            if s == 0:
                head = max(head, e / fps)
                reasons.append(f"head: {e} black frame(s) trimmed")
            elif e == len(black):
                tail = max(tail, (len(black) - s) / fps)
                reasons.append(f"tail: {len(black) - s} black frame(s) trimmed")
    speech_end = speech_start = None
    times, env = audio_envelope(path) if info["has_audio"] else (None, None)
    if env is not None and len(env):
        loud = float(np.percentile(env, 95))
        active = np.where(env > max(loud - 14.0, -45.0))[0]
        if len(active):
            speech_end = _r6(float(times[active[-1]]) + 0.06)
            speech_start = _r6(max(0.0, float(times[active[0]]) - 0.06))
    report["speech_end"] = speech_end
    report["speech_start"] = speech_start
    if diffs is not None and len(diffs) > 12:
        med = float(np.median(diffs)) or 0.01
        still = diffs < min(0.30 * med, 0.8)
        # a frozen or "settling" tail: the model holding an end pose
        run_start = len(still)
        while run_start > 0 and still[run_start - 1]:
            run_start -= 1
        frozen_tail = (len(still) - run_start) / fps
        if frozen_tail > HOLD_ALLOWANCE_S and run_start > 0:
            cut_at = (run_start + 1) / fps + HOLD_ALLOWANCE_S
            if has_dialogue and speech_end is not None:
                cut_at = max(cut_at, speech_end + 0.12)
            candidate = max(0.0, duration - cut_at)
            if candidate > 0.04:
                tail = max(tail, candidate)
                reasons.append(f"tail: {frozen_tail:.2f}s frozen/settling hold; kept {HOLD_ALLOWANCE_S:.2f}s")
        run_end = 0
        while run_end < len(still) and still[run_end]:
            run_end += 1
        frozen_head = run_end / fps
        if frozen_head > 0.25 and run_end < len(still):
            candidate = max(0.0, frozen_head - 2 / fps)
            head = max(head, candidate)
            reasons.append(f"head: {frozen_head:.2f}s frozen start trimmed")
    # never let auto-trim eat the shot
    limit = duration * MAX_AUTO_TRIM_FRACTION
    if head + tail > limit:
        scale = limit / (head + tail)
        head, tail = head * scale, tail * scale
        reasons.append(f"auto-trim capped at {int(MAX_AUTO_TRIM_FRACTION * 100)}% of the shot")
    if duration - head - tail < MIN_SHOT_S:
        head, tail = 0.0, 0.0
        reasons.append("auto-trim skipped: the shot would fall under the minimum length")
    report.update({"head_trim": _r6(head), "tail_trim": _r6(tail), "reasons": reasons})
    return report


# ------------------------------------------------------------------- colour

def color_signature(path: str | Path, start: float, end: float, samples: int = 6):
    """Per-channel mean and std (0-255) over a few frames of the kept range."""
    if np is None or end <= start:
        return None
    span = end - start
    times = [start + span * (i + 0.5) / samples for i in range(samples)]
    frames = rgb_samples(path, times)
    if not frames:
        return None
    stack = np.stack(frames).reshape(-1, 3)
    return {"mean": [round(float(v), 3) for v in stack.mean(axis=0)],
            "std": [round(float(v), 3) for v in stack.std(axis=0)]}


def color_corrections(signatures: Sequence[Mapping | None], groups: Sequence[str], *,
                      strength: float = COLOR_STRENGTH) -> list[dict | None]:
    """Gain/offset per shot pulling it towards its scene group's median look."""
    out: list[dict | None] = [None] * len(signatures)
    by_group: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    for members in by_group.values():
        valid = [i for i in members if signatures[i]]
        if len(valid) < 2:
            continue
        ref_mean = [median(signatures[i]["mean"][c] for i in valid) for c in range(3)]
        ref_std = [median(signatures[i]["std"][c] for i in valid) for c in range(3)]
        for i in valid:
            sig = signatures[i]
            if max(abs(sig["mean"][c] - ref_mean[c]) for c in range(3)) > 60:
                # far too different to be drift: a deliberate look (another
                # place, a flashback). Leave it and let the critic say so.
                out[i] = {"skipped": "differs from its scene by more than drift; left alone"}
                continue
            gains, offsets = [], []
            for c in range(3):
                std = max(sig["std"][c], 1.0)
                gain = 1.0 + strength * (ref_std[c] / std - 1.0)
                gain = min(max(gain, COLOR_GAIN_RANGE[0]), COLOR_GAIN_RANGE[1])
                target_mean = sig["mean"][c] + strength * (ref_mean[c] - sig["mean"][c])
                offset = target_mean - sig["mean"][c] * gain
                offset = min(max(offset, -COLOR_OFFSET_LIMIT), COLOR_OFFSET_LIMIT)
                gains.append(round(gain, 4))
                offsets.append(round(offset, 3))
            if all(abs(g - 1) < 0.02 for g in gains) and all(abs(o) < 2.0 for o in offsets):
                continue
            out[i] = {"gain": gains, "offset": offsets,
                      "towards": {"mean": [round(v, 2) for v in ref_mean], "std": [round(v, 2) for v in ref_std]}}
    return out


def _lutrgb(correction: Mapping | None) -> str:
    if not correction or "gain" not in correction:
        return ""
    exprs = []
    for channel, gain, offset in zip("rgb", correction["gain"], correction["offset"]):
        exprs.append(f"{channel}='clip(val*{gain:.4f}{offset:+.3f},0,255)'")
    return "format=rgb24,lutrgb=" + ":".join(exprs) + ","


# ------------------------------------------------------------------ loudness

_EBU_I = re.compile(r"I:\s*(-?[\d.]+|-inf)\s*LUFS")


def measure_loudness(path: str | Path, start: float, end: float) -> float | None:
    """Integrated loudness (LUFS) of a range, or None when silent/too short."""
    if end - start < 0.45:
        return None
    result = _run(["ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{start:.6f}", "-to", f"{end:.6f}",
                   "-i", str(path), "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                  timeout=300, text=True)
    matches = _EBU_I.findall(result.stderr or "")
    if not matches or matches[-1] == "-inf":
        return None
    value = float(matches[-1])
    return None if value < -60 else value


def shot_gain_db(lufs: float | None, *, dialogue: bool) -> float:
    if lufs is None:
        return 0.0
    target = SHOT_TARGET_DIALOGUE_LUFS if dialogue else SHOT_TARGET_AMBIENCE_LUFS
    if not dialogue and -30.0 <= lufs <= -22.0:
        return 0.0          # ambience already sits in its bed
    return round(max(-SHOT_GAIN_LIMIT_DB, min(SHOT_GAIN_LIMIT_DB, target - lufs)), 2)


# ---------------------------------------------------------------- beat track

def beat_times(path: str | Path, *, start: float = 0.0, duration: float | None = None) -> dict[str, Any]:
    """Tempo and beat positions (seconds from ``start``) of a song.

    Spectral-flux onset envelope, autocorrelation tempo with a prior near
    120 BPM, then Ellis-style dynamic-programming beat tracking.
    """
    if np is None:
        return {"bpm": None, "beats": [], "skipped": "numpy missing"}
    rate, n_fft, hop = 22050, 1024, 512
    end = None if duration is None else start + duration
    y = pcm_mono(path, rate=rate, start=start, end=end)
    if y is None or len(y) < n_fft * 8:
        return {"bpm": None, "beats": [], "skipped": "audio too short"}
    window = np.hanning(n_fft).astype(np.float32)
    count = 1 + (len(y) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(count)[:, None]
    spec = np.abs(np.fft.rfft(y[idx] * window, axis=1))
    logspec = np.log1p(10.0 * spec)
    flux = np.maximum(0.0, np.diff(logspec, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    flux = flux - np.convolve(flux, np.ones(16) / 16, mode="same")
    onset = np.maximum(flux, 0.0)
    if onset.std() > 0:
        onset = onset / onset.std()
    fps = rate / hop
    min_lag, max_lag = int(fps * 60 / 180), int(fps * 60 / 60)
    ac = np.correlate(onset, onset, mode="full")[len(onset) - 1:]
    lags = np.arange(min_lag, min(max_lag, len(ac) - 1) + 1)
    if len(lags) == 0:
        return {"bpm": None, "beats": [], "skipped": "song too short for tempo"}
    bpms = 60.0 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)
    period = int(lags[int(np.argmax(ac[lags] * prior))])
    bpm = 60.0 * fps / period
    # dynamic programming beat tracker
    alpha = 100.0
    score = onset.copy()
    back = np.full(len(onset), -1)
    for t in range(len(onset)):
        lo, hi = t - 2 * period, t - period // 2
        if hi <= 0:
            continue
        lo = max(lo, 0)
        prev = np.arange(lo, hi)
        penalty = -alpha * (np.log((t - prev) / period)) ** 2
        cand = score[prev] + penalty
        k = int(np.argmax(cand))
        score[t] = onset[t] + cand[k]
        back[t] = prev[k]
    tail = len(onset) - 2 * period
    t = int(np.argmax(score[max(tail, 0):]) + max(tail, 0))
    beats = []
    while t >= 0:
        beats.append(t)
        t = int(back[t])
    beats = sorted(beats)
    return {"bpm": round(float(bpm), 2), "beats": [_r6(b / fps) for b in beats]}


# ------------------------------------------------------------------- planning

FADE_IN_WORDS = {"fade_in", "fade_from_black", "fade_up"}
_ALIASES = {"crossfade": "dissolve", "cross_dissolve": "dissolve", "mix": "dissolve",
            "fade": "fade_through_black", "fade_black": "fade_through_black",
            "dip_to_black": "fade_through_black", "hard_cut": "cut", "straight_cut": "cut"}


def normalize_transition(value: Any) -> str:
    """A model- or person-written transition name as one the editor knows;
    anything else (``fade_in``, ``wipe``...) becomes a straight cut."""
    if isinstance(value, Mapping):
        value = value.get("kind")
    kind = str(value or "cut").strip().lower().replace("-", "_").replace(" ", "_")
    kind = _ALIASES.get(kind, kind)
    return kind if kind in TRANSITIONS else "cut"


def _transition(shot: Mapping, index: int, fps: int) -> dict[str, Any]:
    raw = shot.get("transition_in") or {}
    if isinstance(raw, str):
        raw = {"kind": raw}
    kind = str(raw.get("kind") or "cut").lower().replace("-", "_").replace(" ", "_")
    if kind in {"crossfade", "cross_dissolve", "mix"}:
        kind = "dissolve"
    if kind in {"fade", "fade_black", "dip_to_black"}:
        kind = "fade_through_black"
    if kind not in TRANSITIONS:
        raise StitchError(f"shot {index + 1}: unknown transition {kind!r}")
    frames = int(raw.get("frames") or DEFAULT_TRANSITION_FRAMES.get(kind, 0))
    offset = raw.get("audio_offset_frames")
    if offset is None:
        offset = DEFAULT_AUDIO_OFFSET_FRAMES.get(kind, 0)
    return {"kind": kind, "frames": frames if kind in SOFT_CUTS else 0,
            "audio_offset_frames": int(offset), "why": str(raw.get("why") or "")[:300]}


def plan_cut(plan: Mapping[str, Any], *, analyses: Sequence[Mapping] | None = None,
             analyze: bool = True) -> dict[str, Any]:
    """Resolve a director's cut plan into exact frame/sample decisions.

    ``plan`` = {"shots": [{"path", "trim_in"?, "trim_out"?, "auto_trim"?,
    "scene"?, "dialogue"?, "transition_in"?}], "width", "height", "fps",
    "quality", "music"?: {"path", "gain_db", "beat_align"}, "clip_audio"?,
    "color_match"?, "fade_in_frames"?, "fade_out_frames"?}
    """
    shots_in = list(plan.get("shots") or [])
    if not shots_in:
        raise StitchError("the cut plan has no shots")
    fps = int(plan.get("fps") or 24)
    width = int(plan.get("width") or 0)
    height = int(plan.get("height") or 0)
    quality = str(plan.get("quality") or "high")
    if quality not in QUALITY:
        raise StitchError(f"unknown quality {quality!r}")
    notes: list[str] = []
    if np is None:
        notes.append("numpy missing: dead-frame trims, colour match, speech guard and beat alignment were skipped")
    shots: list[dict[str, Any]] = []
    for i, raw in enumerate(shots_in):
        path = Path(str(raw.get("path") or ""))
        if not path.is_file():
            raise StitchError(f"shot {i + 1}: source {path.name or '?'} is missing")
        dialogue = bool(raw.get("dialogue"))
        if analyses is not None:
            analysis = dict(analyses[i])
        elif analyze:
            analysis = analyze_clip(path, fps_hint=fps, has_dialogue=dialogue)
        else:
            analysis = {"probe": probe(path), "head_trim": 0.0, "tail_trim": 0.0, "reasons": [],
                        "internal_cuts": [], "mid_cuts": [], "speech_end": None}
        info = analysis["probe"]
        duration = float(info["duration"])
        explicit = raw.get("trim_in") is not None or raw.get("trim_out") is not None
        auto = bool(raw.get("auto_trim", True)) and not explicit
        vin = float(raw.get("trim_in") or 0.0)
        vout = duration if raw.get("trim_out") in (None, "") else float(raw["trim_out"])
        if vin < 0 or vout <= vin or vout > duration + 0.05:
            raise StitchError(f"shot {i + 1}: trim {vin:.3f}-{vout:.3f}s does not fit its {duration:.3f}s source")
        vout = min(vout, duration)
        reasons = []
        if auto:
            vin = max(vin, float(analysis.get("head_trim") or 0.0))
            vout = min(vout, duration - float(analysis.get("tail_trim") or 0.0))
            reasons = list(analysis.get("reasons") or [])
        # snap to whole frames so picture and sound agree on every seam
        fin = int(round(vin * fps))
        fout = min(int(round(vout * fps)), math.floor(duration * fps + 1e-6))
        if fout - fin < max(1, _frames(0.4, fps)):
            raise StitchError(f"shot {i + 1}: less than 0.4 s left after trimming")
        shots.append({
            "index": i, "id": str(raw.get("id") or f"shot{i + 1}"), "path": str(path),
            "scene": str(raw.get("scene") or plan.get("scene") or "main"),
            "dialogue": dialogue, "in_frame": fin, "out_frame": fout,
            "source_duration": duration, "has_audio": bool(info.get("has_audio")),
            "audio_duration": float(info.get("audio_duration") or 0.0),
            "width": info.get("width"), "height": info.get("height"),
            "trim_reasons": reasons, "explicit_trim": explicit, "auto_trim": auto,
            "mid_cuts": analysis.get("mid_cuts") or [],
            "speech_end": analysis.get("speech_end"),
            "speech_start": analysis.get("speech_start"),
            "transition": _transition(raw, i, fps) if i else {"kind": "cut", "frames": 0, "audio_offset_frames": 0, "why": ""},
        })
        target = raw.get("target_seconds")
        if auto and target:
            _trim_to_target(shots[-1], float(target), fps)
        if analysis.get("mid_cuts"):
            notes.append(f"shot {i + 1} contains an unrequested cut at "
                         + ", ".join(f"{c['time']:.2f}s" for c in analysis["mid_cuts"])
                         + " — re-render it; the editor cannot hide a cut inside a take")
    if not width or not height:
        width, height = int(shots[0]["width"] or 1280), int(shots[0]["height"] or 720)
    width -= width % 2
    height -= height % 2
    # scene groups: a dissolve or fade into a shot starts a new scene unless the plan names them
    if not any(raw.get("scene") for raw in shots_in):
        group = 0
        for s in shots:
            if s["index"] and s["transition"]["kind"] in SOFT_CUTS:
                group += 1
            s["scene"] = f"scene{group + 1}"
    # colour
    color_cfg = plan.get("color_match") if isinstance(plan.get("color_match"), Mapping) else {}
    color_on = plan.get("color_match") is not False and color_cfg.get("enabled", True) and np is not None
    if color_on:
        sigs = [color_signature(s["path"], s["in_frame"] / fps, s["out_frame"] / fps) for s in shots]
        corr = color_corrections(sigs, [s["scene"] for s in shots],
                                 strength=float(color_cfg.get("strength", COLOR_STRENGTH)))
        for s, sig, c in zip(shots, sigs, corr):
            s["color_signature"] = sig
            s["color"] = c
    # loudness (clip audio)
    clip_audio = bool(plan.get("clip_audio", True))
    for s in shots:
        s["lufs"] = measure_loudness(s["path"], s["in_frame"] / fps, s["out_frame"] / fps) \
            if (clip_audio and s["has_audio"]) else None
        s["gain_db"] = shot_gain_db(s["lufs"], dialogue=s["dialogue"])
    # beat alignment of hard cuts when a song drives the cut
    music = dict(plan.get("music") or {}) if plan.get("music") else None
    if clip_audio:
        _make_room_for_audio_edits(shots, fps)
    beat_report = None
    if music and music.get("beat_align", True) and np is not None:
        beat_report = _align_to_beats(shots, music, fps)
    timeline = _timeline(shots, fps)
    _audio_edits(shots, timeline, fps)
    return {
        "schema": SCHEMA, "fps": fps, "width": width, "height": height, "quality": quality,
        "delivery": quality in DELIVERY_QUALITIES,
        "shots": shots, "timeline": timeline, "music": music, "clip_audio": clip_audio,
        "fade_in_frames": int(plan.get("fade_in_frames") or 0),
        "fade_out_frames": int(plan.get("fade_out_frames") if plan.get("fade_out_frames") is not None else 12),
        "loudness": {"target_lufs": float((plan.get("loudness") or {}).get("target_lufs", TARGET_LUFS)),
                     "true_peak": float((plan.get("loudness") or {}).get("true_peak", TARGET_TP))},
        "beats": beat_report, "notes": notes,
    }


def _trim_to_target(shot: dict, target: float, fps: int) -> None:
    """Pace a take to the length the director planned: a generated shot is
    always ~5 s, a planned insert may be 3. Cut in late (40 % of the excess from
    the head) and get out early (60 % from the tail), never through a line."""
    kept = (shot["out_frame"] - shot["in_frame"]) / fps
    target = max(target, MIN_SHOT_S)
    if kept <= target + 0.1:
        return
    excess = kept - target
    head, tail = 0.4 * excess, 0.6 * excess
    if shot["dialogue"] and shot.get("speech_end") is not None:
        latest_out = shot["speech_end"] + 0.3
        max_tail = max(0.0, shot["out_frame"] / fps - latest_out)
        if tail > max_tail:
            head += tail - max_tail
            tail = max_tail
    if shot["dialogue"] and shot.get("speech_start") is not None:
        max_head = max(0.0, shot["speech_start"] - 0.15 - shot["in_frame"] / fps)
        head = min(head, max_head)
    head_f, tail_f = int(round(head * fps)), int(round(tail * fps))
    if head_f + tail_f <= 0:
        return
    shot["in_frame"] += head_f
    shot["out_frame"] -= tail_f
    shot["trim_reasons"].append(f"paced to the planned {target:.1f}s: {head_f} frame(s) off the head, "
                                f"{tail_f} off the tail")


def _make_room_for_audio_edits(shots: list[dict], fps: int) -> None:
    """Give every seam the audio frames its edit needs.

    Generated clips have no handles: audio ends where picture ends. A J-cut
    (the next shot's sound arrives first) is made the way an editor makes it —
    the incoming picture's head is trimmed while its audio still starts at the
    top — and an L-cut trims the outgoing picture's tail while its sound runs
    on. A plain cut borrows at most two frames each side for a real crossfade.
    Explicit trims are never touched.
    """
    min_frames = _frames(MIN_SHOT_S, fps)
    for k in range(1, len(shots)):
        a, b = shots[k - 1], shots[k]
        trans = b["transition"]
        if trans["kind"] in SOFT_CUTS or not (a["has_audio"] and b["has_audio"]):
            continue
        a_audio_frames = math.floor((a["audio_duration"] or a["source_duration"]) * fps + 1e-6)
        offset = trans["audio_offset_frames"]
        half = max(1, _frames(AUDIO_XFADE_S.get(trans["kind"], 0.10) / 2, fps))
        need_b = max(0, -offset) + half          # frames of B audio before its picture
        need_a = max(0, offset) + half           # frames of A audio after its picture
        if b.get("auto_trim") and b["in_frame"] < need_b:
            add = need_b - b["in_frame"] if offset < 0 else min(2, need_b - b["in_frame"])
            if b["out_frame"] - b["in_frame"] - add >= min_frames:
                b["in_frame"] += add
                b["trim_reasons"].append(
                    f"head: {add} picture frame(s) trimmed so the sound can lead"
                    + (" (J-cut)" if offset < 0 else " (audio crossfade)"))
        tail_room = a_audio_frames - a["out_frame"]
        if a.get("auto_trim") and tail_room < need_a:
            cut = need_a - tail_room if offset > 0 else min(2, need_a - tail_room)
            if a["out_frame"] - cut - a["in_frame"] >= min_frames:
                a["out_frame"] -= cut
                a["trim_reasons"].append(
                    f"tail: {cut} picture frame(s) trimmed so the sound can run on"
                    + (" (L-cut)" if offset > 0 else " (audio crossfade)"))


def _timeline(shots: list[dict], fps: int) -> dict[str, Any]:
    """Frame-exact timeline positions: shot k starts where the previous shot's
    picture ends, minus the overlap of a dissolve or fade into shot k."""
    start = 0
    for k, s in enumerate(shots):
        length = s["out_frame"] - s["in_frame"]
        if k:
            overlap = s["transition"]["frames"]
            prev_len = shots[k - 1]["out_frame"] - shots[k - 1]["in_frame"]
            overlap = min(overlap, prev_len // 2, length // 2)
            s["transition"]["frames"] = overlap
            start = shots[k - 1]["start_frame"] + prev_len - overlap
        s["start_frame"] = start
        s["length_frames"] = length
    total = shots[-1]["start_frame"] + shots[-1]["length_frames"]
    return {"total_frames": total, "total_seconds": _r6(total / fps),
            "cuts": [{"into_shot": k + 1, "frame": s["start_frame"], "seconds": _r6(s["start_frame"] / fps),
                      "kind": s["transition"]["kind"], "frames": s["transition"]["frames"]}
                     for k, s in enumerate(shots) if k]}


def _align_to_beats(shots: list[dict], music: Mapping, fps: int) -> dict[str, Any]:
    path = Path(str(music.get("path") or ""))
    if not path.is_file():
        return {"skipped": "music file missing"}
    total = sum(s["out_frame"] - s["in_frame"] for s in shots) / fps
    info = beat_times(path, start=float(music.get("offset") or 0.0), duration=total + 2.0)
    beats = info.get("beats") or []
    if len(beats) < 4 or not info.get("bpm"):
        return {"bpm": info.get("bpm"), "aligned": 0, "skipped": info.get("skipped") or "no clear beat"}
    period = 60.0 / info["bpm"]
    window = min(0.45, period * 0.5)
    before = after = 0
    moved = []
    t = 0  # running end of the picture, in frames
    for k, s in enumerate(shots):
        if k == 0:
            t += s["out_frame"] - s["in_frame"]
            continue
        overlap = s["transition"]["frames"] if s["transition"]["kind"] in SOFT_CUTS else 0
        t -= overlap
        cut = t / fps
        nearest = min(beats, key=lambda b: abs(b - cut))
        if abs(nearest - cut) <= 1.5 / fps:
            before += 1
        if s["transition"]["kind"] in HARD_CUTS:
            prev = shots[k - 1]
            best = None
            for b in sorted(beats, key=lambda b: abs(b - cut)):
                if abs(b - cut) > window:
                    break
                delta = _frames(b - cut, fps)
                new_len = prev["out_frame"] + delta - prev["in_frame"]
                src_frames = math.floor(prev["source_duration"] * fps + 1e-6)
                if new_len >= _frames(MIN_SHOT_S, fps) and prev["out_frame"] + delta <= src_frames:
                    if prev["dialogue"] and prev.get("speech_end") and delta < 0 \
                            and (prev["out_frame"] + delta) / fps < prev["speech_end"]:
                        continue
                    best = delta
                    break
            if best:
                prev["out_frame"] += best
                t += best
                moved.append({"into_shot": k + 1, "moved_frames": best})
            cut = t / fps
        if min(abs(b - cut) for b in beats) <= 1.5 / fps:
            after += 1
        t += s["out_frame"] - s["in_frame"]
    return {"bpm": info["bpm"], "beats_found": len(beats), "cuts": len(shots) - 1,
            "cuts_on_beat_before": before, "cuts_on_beat_after": after, "moved": moved}


def _audio_edits(shots: list[dict], timeline: Mapping, fps: int) -> None:
    """Place each shot's audio window, honouring J/L offsets, crossfades and the
    frames each source actually has before its in-point and after its out-point."""
    total = timeline["total_frames"] / fps
    for k, s in enumerate(shots):
        s["audio"] = {"start": _r6(s["start_frame"] / fps),
                      "end": _r6((s["start_frame"] + s["length_frames"]) / fps),
                      "fade_in": DECLICK_S, "fade_out": DECLICK_S}
    shots[0]["audio"]["start"] = 0.0
    shots[-1]["audio"]["end"] = _r6(total)
    for k in range(1, len(shots)):
        a, b = shots[k - 1], shots[k]
        trans = b["transition"]
        cut = b["start_frame"] / fps
        a_src_audio = a["audio_duration"] or a["source_duration"]
        # material available: B before its in-point, A after its picture end
        head_b = b["in_frame"] / fps
        a_pic_end = (a["start_frame"] + a["length_frames"]) / fps
        tail_a = max(0.0, a_src_audio - a["out_frame"] / fps)
        if trans["kind"] in SOFT_CUTS:
            overlap = trans["frames"] / fps
            edit, width = cut + overlap / 2, overlap
        else:
            edit = cut + trans["audio_offset_frames"] / fps
            width = AUDIO_XFADE_S.get(trans["kind"], 0.10)
        lo = cut - head_b                      # earliest B audio can start
        hi = a_pic_end + tail_a                # latest A audio can run
        if hi - lo < width:
            width = max(0.0, hi - lo)
        edit = min(max(edit, lo + width / 2), hi - width / 2) if hi - lo >= width else (lo + hi) / 2
        if width < 0.02:
            width = 0.0
            edit = min(max(cut, lo), hi)
        a["audio"]["end"] = _r6(edit + width / 2)
        b["audio"]["start"] = _r6(edit - width / 2)
        a["audio"]["fade_out"] = _r6(max(width, DECLICK_S))
        b["audio"]["fade_in"] = _r6(max(width, DECLICK_S))
        b["audio"]["edit_point"] = _r6(edit)
        b["audio"]["crossfade"] = _r6(width)
        b["audio"]["offset_vs_picture"] = _r6(edit - (cut if trans["kind"] not in SOFT_CUTS else cut + trans["frames"] / fps / 2))
    for s in shots:
        src_start = s["in_frame"] / fps + (s["audio"]["start"] - s["start_frame"] / fps)
        src_end = s["in_frame"] / fps + (s["audio"]["end"] - s["start_frame"] / fps)
        s["audio"]["src_start"] = _r6(max(0.0, src_start))
        s["audio"]["src_end"] = _r6(max(src_start + 0.001, src_end))


# ------------------------------------------------------------------ rendering

def build_command(resolved: Mapping[str, Any], output: str | Path, *,
                  loudnorm_measured: Mapping[str, Any] | None = None,
                  audio_only: bool = False) -> list[str]:
    """The single ffmpeg command that renders the resolved cut."""
    fps, width, height = resolved["fps"], resolved["width"], resolved["height"]
    shots = resolved["shots"]
    total = resolved["timeline"]["total_frames"] / fps
    # the measuring pass must log at info level: loudnorm prints its JSON there
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info" if audio_only else "error", "-y"]
    for s in shots:
        cmd += ["-i", s["path"]]
    music = resolved.get("music")
    music_index = None
    if music:
        music_index = len(shots)
        cmd += ["-i", str(music["path"])]
    filters: list[str] = []
    if not audio_only:
        for i, s in enumerate(shots):
            src_aspect = (s["width"] or width) / max(1, (s["height"] or height))
            aspect = width / height
            if abs(src_aspect - aspect) / aspect <= ASPECT_CROP_TOLERANCE:
                fit = (f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                       f"crop={width}:{height}")
            else:
                fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
            filters.append(
                f"[{i}:v]trim=start={s['in_frame'] / fps:.6f}:end={s['out_frame'] / fps:.6f},"
                f"setpts=PTS-STARTPTS,fps={fps},{fit},setsar=1,{_lutrgb(s.get('color'))}"
                f"format=yuv420p,tpad=stop_mode=clone:stop=3,trim=end_frame={s['length_frames']},"
                f"settb=1/{fps},setpts=N/({fps}*TB)[v{i}]")
        current = "v0"
        for k in range(1, len(shots)):
            s = shots[k]
            out = f"j{k}"
            kind = s["transition"]["kind"]
            if kind in SOFT_CUTS and s["transition"]["frames"] > 0:
                mode = "fadeblack" if kind == "fade_through_black" else "fade"
                filters.append(f"[{current}][v{k}]xfade=transition={mode}:"
                               f"duration={s['transition']['frames'] / fps:.6f}:"
                               f"offset={s['start_frame'] / fps:.6f},settb=1/{fps},setpts=N/({fps}*TB)[{out}]")
            else:
                filters.append(f"[{current}][v{k}]concat=n=2:v=1:a=0,settb=1/{fps},setpts=N/({fps}*TB)[{out}]")
            current = out
        tail = [f"trim=end_frame={resolved['timeline']['total_frames']}", f"setpts=N/({fps}*TB)"]
        if resolved.get("fade_in_frames"):
            tail.append(f"fade=t=in:st=0:d={resolved['fade_in_frames'] / fps:.6f}")
        if resolved.get("fade_out_frames"):
            d = resolved["fade_out_frames"] / fps
            tail.append(f"fade=t=out:st={max(0.0, total - d):.6f}:d={d:.6f}")
        filters.append(f"[{current}]" + ",".join(tail) + "[vout]")
    # audio
    buses = []
    if resolved.get("clip_audio", True):
        for i, s in enumerate(shots):
            if not s["has_audio"]:
                continue
            au = s["audio"]
            length = au["end"] - au["start"]
            if length <= 0.001:
                continue
            delay_ms = int(round(au["start"] * 1000))
            fade_out_start = max(0.0, length - au["fade_out"])
            filters.append(
                f"[{i}:a]atrim=start={au['src_start']:.6f}:end={au['src_end']:.6f},asetpts=PTS-STARTPTS,"
                f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={s['gain_db']:.2f}dB,"
                f"afade=t=in:st=0:d={au['fade_in']:.6f}:curve=qsin,"
                f"afade=t=out:st={fade_out_start:.6f}:d={au['fade_out']:.6f}:curve=qsin,"
                f"adelay=delays={delay_ms}:all=1[a{i}]")
            buses.append(f"[a{i}]")
    if music_index is not None:
        fade = min(2.0, max(0.5, total * 0.1))
        filters.append(
            f"[{music_index}:a]atrim=start={float(music.get('offset') or 0):.6f}:"
            f"duration={total:.6f},asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,volume={float(music.get('gain_db') or 0):.2f}dB,"
            f"afade=t=in:st=0:d=0.05,afade=t=out:st={max(0.0, total - fade):.6f}:d={fade:.6f}[mus]")
        buses.append("[mus]")
    has_audio_out = bool(buses)
    if has_audio_out:
        mix = (f"{''.join(buses)}amix=inputs={len(buses)}:normalize=0:dropout_transition=0"
               if len(buses) > 1 else f"{buses[0]}anull")
        loud = resolved["loudness"]
        if loudnorm_measured:
            m = loudnorm_measured
            norm = (f"loudnorm=I={loud['target_lufs']}:TP={loud['true_peak']}:LRA={TARGET_LRA}:"
                    f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
                    f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
        else:
            norm = (f"loudnorm=I={loud['target_lufs']}:TP={loud['true_peak']}:LRA={TARGET_LRA}"
                    f":print_format=json")
        filters.append(f"{mix},apad=whole_dur={total:.6f},atrim=end={total:.6f},{norm},"
                       f"aresample=48000,atrim=end={total:.6f}[aout]")
    cmd += ["-filter_complex", ";".join(filters)]
    if audio_only:
        if not has_audio_out:
            raise StitchError("no audio to measure")
        cmd += ["-map", "[aout]", "-f", "null", "-"]
        return cmd
    preset, crf = QUALITY[resolved["quality"]]
    cmd += ["-map", "[vout]"]
    if has_audio_out:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-r", str(fps), "-g", str(fps * 2), "-movflags", "+faststart",
            "-map_metadata", "-1", str(output)]
    return cmd


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


def render(plan: Mapping[str, Any], output: str | Path, *, resolved: Mapping[str, Any] | None = None,
           timeout: int = 3600) -> dict[str, Any]:
    """Plan (unless given), measure the mix, render once, and return a receipt."""
    if not ffmpeg_available():
        raise StitchError("ffmpeg/ffprobe are not installed")
    resolved = dict(resolved or plan_cut(plan))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    measured = None
    if (resolved.get("clip_audio", True) and any(s["has_audio"] for s in resolved["shots"])) or resolved.get("music"):
        probe_cmd = build_command(resolved, output, audio_only=True)
        result = _run(probe_cmd, timeout=timeout, text=True)
        match = _LOUDNORM_JSON.search(result.stderr or "")
        if result.returncode == 0 and match:
            data = json.loads(match.group(0))
            if all(k in data for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")) \
                    and data["input_i"] not in ("-inf", "inf"):
                measured = data
        if measured is None:
            resolved.setdefault("notes", []).append("loudness measurement failed; single-pass loudnorm used")
    tmp = output.with_name(output.stem + ".partial" + output.suffix)
    cmd = build_command(resolved, tmp, loudnorm_measured=measured)
    result = _run(cmd, timeout=timeout, text=True)
    if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise StitchError("ffmpeg could not render the cut: " + (result.stderr or "").strip()[-400:])
    tmp.replace(output)
    out_info = probe(output)
    expected = resolved["timeline"]["total_seconds"]
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "output": str(output), "bytes": output.stat().st_size, "sha256": sha256_file(output),
        "quality": resolved["quality"], "delivery_quality": resolved["delivery"],
        "encode_generations": 1,
        "expected_seconds": expected, "probe": out_info,
        "duration_ok": abs(out_info["duration"] - expected) <= 2.0 / resolved["fps"] + 0.05,
        "loudnorm_measured": measured,
        "plan": _public_plan(resolved),
        "command": cmd,
    }
    if not receipt["duration_ok"]:
        raise StitchError(f"rendered {out_info['duration']:.3f}s but the plan is {expected:.3f}s")
    return receipt


def _public_plan(resolved: Mapping[str, Any]) -> dict[str, Any]:
    fps = resolved["fps"]
    shots = []
    for s in resolved["shots"]:
        shots.append({
            "id": s["id"], "source": Path(s["path"]).name, "source_path": s["path"], "scene": s["scene"],
            "dialogue": s["dialogue"],
            "in": _r6(s["in_frame"] / fps), "out": _r6(s["out_frame"] / fps),
            "kept_seconds": _r6(s["length_frames"] / fps),
            "source_seconds": _r6(s["source_duration"]),
            "timeline_start": _r6(s["start_frame"] / fps),
            "trim_reasons": s["trim_reasons"], "transition_in": s["transition"],
            "color": s.get("color"), "lufs": s.get("lufs"), "gain_db": s.get("gain_db"),
            "audio": s.get("audio"), "mid_cuts": s.get("mid_cuts"),
        })
    return {"fps": fps, "width": resolved["width"], "height": resolved["height"],
            "timeline": resolved["timeline"], "beats": resolved.get("beats"),
            "notes": resolved.get("notes"), "shots": shots}


def legacy_board_plan(board: Mapping[str, Any], sources: Sequence[tuple], *, width: int, height: int,
                      song: Path | None = None, quality: str = "high") -> dict[str, Any]:
    """A cut plan for a storyboard: the beats' own transitions when the
    director wrote them, a plain cut otherwise; explicit trims are honoured
    exactly and switch auto-trim off for that beat."""
    shots = []
    fade_in = 0
    for index, (beat, clip, start, end, _duration) in enumerate(sources):
        explicit = beat.get("trim_in_seconds") not in (None, "") or beat.get("trim_out_seconds") not in (None, "")
        shot = {"path": str(clip), "id": str(beat.get("title") or "")[:60] or None,
                "dialogue": bool(str(beat.get("speaker") or "").strip()) or '"' in str(beat.get("video_prompt") or ""),
                "transition_in": normalize_transition(beat.get("transition"))}
        if index == 0 and str(beat.get("transition") or "").lower().replace("-", "_") in FADE_IN_WORDS:
            fade_in = 12
        if beat.get("scene"):
            shot["scene"] = str(beat["scene"])
        if explicit:
            shot["trim_in"], shot["trim_out"] = start, end
        shots.append(shot)
    plan = {"schema": SCHEMA, "shots": shots, "width": width, "height": height, "fps": 24,
            "quality": quality, "fade_out_frames": 12, "fade_in_frames": fade_in}
    if song is not None:
        plan["music"] = {"path": str(song), "gain_db": 0.0, "beat_align": True}
        plan["clip_audio"] = False
    return plan
