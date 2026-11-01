#!/usr/bin/env python3
"""Lay a portrait onto the target frame at a FACE SIZE THAT LIP-SYNCS.

Measured on real takes (2026-08-17):
  face height 40% of frame  -> lip sync judged perfect
  face height 46% of frame  -> "cropped too far in on my head"
  face height 12% of frame  -> "lip sync is horrible"
LTX animates the mouth from pixels; a small face has none to work with. So the
scene placement must be built around the face, not the body: scale the portrait
until the face is ~36% of frame height, centre the face horizontally, and leave
real headroom above it. The surrounding grey is what the edit model fills in.

Usage: scene_canvas.py <portrait> <out.png> <W> <H> [face_frac] [head_top_frac]
Prints the face box it placed, as fractions of the canvas.
"""
import sys

import cv2
import numpy as np

CASC = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"


def main():
    src, dst = sys.argv[1:3]
    W, H = int(sys.argv[3]), int(sys.argv[4])
    face_frac = float(sys.argv[5]) if len(sys.argv) > 5 else 0.36
    head_top = float(sys.argv[6]) if len(sys.argv) > 6 else 0.12

    img = cv2.imread(src)
    if img is None:
        sys.exit(1)
    ph, pw = img.shape[:2]
    faces = cv2.CascadeClassifier(CASC).detectMultiScale(
        cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 6, minSize=(40, 40))
    if len(faces):
        fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
    else:
        # no detect: assume a head-and-shoulders crop, face ~55% of its height
        fh = int(ph * 0.55)
        fw = fh
        fx, fy = (pw - fw) // 2, int(ph * 0.12)

    scale = (face_frac * H) / fh
    nw, nh = max(2, int(pw * scale)), max(2, int(ph * scale))
    person = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LANCZOS4)

    # place: face centred horizontally, top of the head at head_top of the frame
    fx2, fy2, fw2, fh2 = fx * scale, fy * scale, fw * scale, fh * scale
    px = int(W / 2 - (fx2 + fw2 / 2))
    py = int(head_top * H - fy2)

    canvas = np.full((H, W, 3), 128, dtype=np.uint8)
    # intersect the pasted rect with the canvas — the portrait may overhang
    sx0, sy0 = max(0, -px), max(0, -py)
    dx0, dy0 = max(0, px), max(0, py)
    cw = min(nw - sx0, W - dx0)
    ch = min(nh - sy0, H - dy0)
    if cw <= 0 or ch <= 0:
        sys.exit(2)
    canvas[dy0:dy0 + ch, dx0:dx0 + cw] = person[sy0:sy0 + ch, sx0:sx0 + cw]

    # Stretch the last rows of the portrait down to the bottom edge. Left as flat
    # grey, the edit model treats the lower frame as out of scope and hands back a
    # grey block; given smeared clothing it paints a torso instead.
    bottom = dy0 + ch
    if bottom < H:
        band = canvas[max(dy0, bottom - 24):bottom, dx0:dx0 + cw]
        if band.size:
            canvas[bottom:H, dx0:dx0 + cw] = cv2.resize(
                band, (cw, H - bottom), interpolation=cv2.INTER_LINEAR)
    cv2.imwrite(dst, canvas)
    print(f"canvas {W}x{H} face {fw2:.0f}x{fh2:.0f} "
          f"({100 * fh2 / H:.0f}% of frame height) person {nw}x{nh} at ({px},{py})")


if __name__ == "__main__":
    main()
