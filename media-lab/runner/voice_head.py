#!/usr/bin/env python3
"""Trim the phantom syllable TTS puts in front of a line, and give the take a
short silent lead-in.

Measured on a real Voicebox take: the file opened with ~100 ms of speech-loud
noise (-22 dB, as loud as the line itself), then a 60 ms gap, then the actual
first word at 120 ms. A fixed "cut the first N ms" is either too timid (the
artifact survives) or eats the first consonant — so find the GAP instead.

The lead-in matters twice over: it stops the line starting abruptly, and it
gives the lip-sync model a beat of closed mouth before the first phoneme.

Usage: voice_head.py <in.wav> <out.wav>
"""
import array
import math
import subprocess
import sys

SR = 16000
FRAME = 0.01          # 10 ms analysis frames
LEAD_IN = 0.18        # silence to prepend
PRE_ROLL = 0.03       # keep this much before the first word (natural attack)
SEARCH = 0.30        # the artifact always sits in the first 300 ms


def frames(a, n):
    return [20 * math.log10((sum(v * v for v in a[i * n:(i + 1) * n]) / n) ** 0.5 / 32768 + 1e-9)
            for i in range(len(a) // n)]


def main():
    src, dst = sys.argv[1:3]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", str(SR),
                          "-f", "s16le", "-"], capture_output=True).stdout
    a = array.array("h")
    a.frombytes(raw[:len(raw) // 2 * 2])
    n = int(SR * FRAME)
    if len(a) < n * 20:
        sys.exit(1)
    env = frames(a, n)
    speech = sorted(env)[int(len(env) * 0.75)]        # typical loud-frame level
    quiet = speech - 15
    limit = min(len(env), int(SEARCH / FRAME))

    # The artifact has a very specific shape: audible energy in the FIRST few
    # frames, a short burst, then a dip — all inside the first 300 ms. Anything
    # else (a normal quiet start, a line that simply begins loud) is left alone.
    # Deliberately conservative: eating a first consonant is far worse than
    # leaving a faint blip.
    cut = 0
    # the burst may be preceded by a frame or two of near-silence
    start = next((i for i in range(min(5, len(env))) if env[i] >= speech - 12), None)
    if start is not None:
        burst = start
        while burst < limit and env[burst] >= quiet:
            burst += 1
        dip = burst
        while dip < limit and env[dip] < quiet:
            dip += 1
        if (burst - start) <= 15 and (dip - burst) >= 3 and dip < limit:
            cut = dip

    body = a[cut * n:]
    if len(body) < SR * 0.3:                          # never hand back a stub
        body = a
        cut = 0
    out = array.array("h", [0] * int(SR * LEAD_IN)) + body
    # 15 ms fade so the splice cannot click
    f = int(SR * 0.015)
    base = int(SR * LEAD_IN)
    for i in range(f):
        out[base + i] = int(out[base + i] * (i / f))
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
                        "-ac", "1", "-i", "-", "-c:a", "pcm_s16le", dst],
                       input=out.tobytes(), capture_output=True)
    if p.returncode != 0:
        sys.exit(2)
    print(f"head: cut {cut * 10} ms of artifact, {int(LEAD_IN * 1000)} ms lead-in "
          f"(speech level {speech:.0f} dB)")


if __name__ == "__main__":
    main()
