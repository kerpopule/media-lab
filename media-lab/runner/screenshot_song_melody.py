#!/usr/bin/env python3
"""Measure whether a rendered screenshot-song has a genuinely melodic pitch range.

The worker calls this in the existing LTX utility environment, where NumPy,
PyTorch, and torchaudio are already installed. FFmpeg performs decoding so the
script does not need TorchCodec, librosa, or any network download.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
import torch
import torchaudio


SAMPLE_RATE = 16_000


def decode_mono(path: Path) -> np.ndarray:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        capture_output=True,
        check=False,
    )
    if result.returncode or not result.stdout:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(detail or "FFmpeg produced no audio")
    return np.frombuffer(result.stdout, dtype="<i2").astype("float32") / 32768.0


def metrics(path: Path) -> dict:
    samples = decode_mono(path)
    if samples.size < SAMPLE_RATE * 5:
        raise ValueError("audio is shorter than five seconds")
    waveform = torch.from_numpy(samples.copy()).unsqueeze(0)
    frame_samples = 640  # 40 ms
    hop_samples = 320    # 20 ms
    frames = waveform.unfold(1, frame_samples, hop_samples)[0]
    rms = (frames.square().mean(1) + 1e-12).sqrt()
    active = rms > torch.quantile(rms, 0.35)
    pitch = torchaudio.functional.detect_pitch_frequency(
        waveform, SAMPLE_RATE, frame_time=0.02, win_length=5,
        freq_low=75, freq_high=500,
    )[0]
    size = min(len(pitch), len(active))
    active_pitch = pitch[:size][active[:size]]
    active_pitch = active_pitch[torch.isfinite(active_pitch) & (active_pitch > 0)]
    if active_pitch.numel() < 100:
        raise ValueError("not enough voiced pitch evidence")
    semitones = 12.0 * torch.log2(active_pitch / 440.0)
    q10 = torch.quantile(semitones, 0.10)
    q25 = torch.quantile(semitones, 0.25)
    q75 = torch.quantile(semitones, 0.75)
    q90 = torch.quantile(semitones, 0.90)
    return {
        "analyzed_seconds": round(float(samples.size / SAMPLE_RATE), 3),
        "active_pitch_frames": int(active_pitch.numel()),
        "pitch_iqr_semitones": round(float(q75 - q25), 3),
        "pitch_p90_p10_semitones": round(float(q90 - q10), 3),
        "pitch_median_hz": round(float(torch.median(active_pitch)), 3),
        "analysis": "torchaudio-autocorrelation-v1",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    args = parser.parse_args()
    try:
        report = metrics(args.audio)
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:800]}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
