#!/usr/bin/env python3
"""Decode-backed signal gate for supplied Media Lab audio masters."""
from array import array
import math
from pathlib import Path
import subprocess


def audio_signal_metrics(path, sample_rate=8000, min_seconds=0.10,
                         min_peak=64, min_rms=8.0):
    """Return decoded PCM metrics and whether the input carries usable signal.

    Container presence and duration are not evidence: an out-of-bounds ffmpeg
    extraction can create a correctly formed all-zero WAV. Decode through ffmpeg
    so WAV, FLAC and compressed inputs share the same gate.
    """
    p = Path(path)
    if not p.is_file() or p.stat().st_size <= 0:
        return {"ok": False, "reason": "missing-or-empty"}
    try:
        r = subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-vn",
            "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "pipe:1",
        ], capture_output=True, timeout=180)
    except Exception as e:
        return {"ok": False, "reason": f"decode-error:{type(e).__name__}"}
    if r.returncode != 0:
        return {"ok": False, "reason": "decode-failed"}
    pcm = array("h")
    raw = r.stdout[:len(r.stdout) // 2 * 2]
    pcm.frombytes(raw)
    if len(pcm) < int(sample_rate * min_seconds):
        return {"ok": False, "reason": "too-short", "samples": len(pcm)}
    peak = max(abs(v) for v in pcm)
    rms = math.sqrt(sum(v * v for v in pcm) / len(pcm))
    dbfs = 20 * math.log10(rms / 32768.0) if rms else float("-inf")
    signal = peak >= min_peak and rms >= min_rms
    return {
        "ok": signal,
        "reason": "signal" if signal else "silent-or-near-silent",
        "samples": len(pcm),
        "sample_rate": sample_rate,
        "peak": peak,
        "rms": round(rms, 3),
        "rms_dbfs": round(dbfs, 3) if math.isfinite(dbfs) else "-inf",
    }
