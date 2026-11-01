#!/usr/bin/env python3
"""CPU-only sung-lyric word timestamps for Screenshots to Song.

Uses an already-cached CTranslate2 Whisper model and explicitly enables offline
mode. JSON is written to stdout for app.py; diagnostics go to stderr.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from faster_whisper import WhisperModel


def cached_model(name: str) -> str:
    root = Path.home() / ".cache/huggingface/hub" / f"models--Systran--faster-whisper-{name}" / "snapshots"
    snapshots = sorted((p for p in root.glob("*") if p.is_dir()), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    if not snapshots:
        raise FileNotFoundError(f"cached faster-whisper-{name} model is unavailable")
    return str(snapshots[0])


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: screenshot_song_align.py AUDIO", file=sys.stderr)
        return 2
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    model_name = os.getenv("MEDIA_LAB_SCREENSHOT_WHISPER", "small")
    model = WhisperModel(cached_model(model_name), device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        sys.argv[1], beam_size=5, word_timestamps=True, vad_filter=False,
        condition_on_previous_text=False,
    )
    words = []
    text = []
    for segment in segments:
        if segment.text.strip():
            text.append(segment.text.strip())
        for word in segment.words or []:
            token = word.word.strip()
            if token:
                words.append({"word": token, "start": round(word.start, 3),
                              "end": round(word.end, 3),
                              "probability": round(float(word.probability), 4)})
    print(json.dumps({"duration": round(float(info.duration), 3),
                      "language": info.language, "text": " ".join(text), "words": words},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
