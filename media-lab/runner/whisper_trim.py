#!/usr/bin/env python3
"""Trim a TTS wav to exactly the requested line using word timestamps.

Chatterbox cloned-voice generations run away past the line into re-phrasings
(the alignment force-EOS is bypassed for cloned conds). Silence-gap trimming
misses it — the repeats flow without pauses. The production fix: transcribe
with faster-whisper, find where the TARGET text ends, cut there.

Usage: whisper_trim.py <in.wav> <line.txt> <out.wav>
Exit 0 with out.wav written on success (even if no trim was needed);
non-zero = caller keeps the original.
"""
import re
import subprocess
import sys

wav, line_file, out = sys.argv[1:4]
target = open(line_file).read()

def norm(s):
    return re.findall(r"[a-z0-9']+", s.lower())

tw = norm(target)
if not tw:
    sys.exit(1)

from faster_whisper import WhisperModel

model = WhisperModel("small", device="cpu", compute_type="int8")
segs, _ = model.transcribe(wav, word_timestamps=True, language="en")
words = [(norm(w.word)[0], w.end) for s in segs for w in s.words if norm(w.word)]
if not words:
    sys.exit(1)

# walk the transcript matching the target sequence (tolerant: allow skips in
# the transcript, since whisper sometimes splits/merges tokens)
ti, end_t = 0, None
for w, t in words:
    if ti < len(tw) and (w == tw[ti] or (ti + 1 < len(tw) and w == tw[ti + 1])):
        ti += 2 if (w != tw[ti] and ti + 1 < len(tw) and w == tw[ti + 1]) else 1
        end_t = t
        if ti >= len(tw):
            break

matched = ti / len(tw)
total = words[-1][1]
if end_t is None or matched < 0.6:
    sys.exit(1)                       # couldn't align — don't guess
if total - end_t < 0.8:
    # nothing meaningful after the line — keep as-is
    subprocess.run(["cp", wav, out], check=True)
    print(f"no-trim matched={matched:.2f} end={end_t:.2f} total={total:.2f}")
    sys.exit(0)

cut = end_t + 0.30
subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-t", f"{cut:.3f}",
                "-i", wav, "-c:a", "pcm_s16le", out], check=True)
print(f"trimmed at {cut:.2f}s (was {total:.2f}s, matched={matched:.2f})")
