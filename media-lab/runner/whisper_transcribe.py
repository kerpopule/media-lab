#!/usr/bin/env python3
"""Print the transcript of a wav to stdout — reference text for voice cloning.

Runs in the comfy venv (faster-whisper lives there, same as whisper_trim.py):
  ~/runtime/comfy-ltx25/ComfyUI/.venv/bin/python whisper_transcribe.py in.wav
"""
import sys

from faster_whisper import WhisperModel


def main():
    wav = sys.argv[1]
    model = WhisperModel("base.en", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(wav, beam_size=5)
    print(" ".join(s.text.strip() for s in segments).strip())


if __name__ == "__main__":
    main()
