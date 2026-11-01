#!/usr/bin/env python3
"""Crop the largest face (with generous margin) out of a character reference
image. A multi-pose contact sheet fed raw as an i2v start image makes the video
OPEN ON THE GRID and zoom into one cell — the start image must be one clean
portrait. cv2 haar cascade, CPU, no extra deps.

The margin is CLAMPED against every other detected face: a generous box around
one pose used to swallow the top of the pose below it, and that neighbour rode
into the video as a second body at the bottom of frame.

Usage: face_crop.py <in-image> <out-png>   (exit non-zero if no face found)"""
import sys

import cv2

src, dst = sys.argv[1:3]
img = cv2.imread(src)
if img is None:
    sys.exit(1)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(60, 60))
if len(faces) == 0:
    sys.exit(2)
faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
x, y, w, h = faces[0]
H, W = img.shape[:2]
# margin: half a face each side, a full face above, one and a bit below
mx, my = int(w * 0.6), int(h * 0.9)
x0, y0 = max(0, x - mx), max(0, y - my)
x1, y1 = min(W, x + w + mx), min(H, y + h + int(h * 1.1))

# stop the box short of any OTHER face — halfway to it, so no part of a
# neighbouring pose (their hair, shoulders, chin) survives in the crop
cx, cy = x + w / 2.0, y + h / 2.0
for (ox, oy, ow, oh) in faces[1:]:
    ocx, ocy = ox + ow / 2.0, oy + oh / 2.0
    if abs(ocx - cx) > abs(ocy - cy):                  # neighbour is to a side
        if ocx > cx:
            x1 = min(x1, int((x + w + ox) / 2))
        else:
            x0 = max(x0, int((cx + ox + ow) / 2))
    else:                                              # neighbour is above/below
        if ocy > cy:
            y1 = min(y1, int((y + h + oy) / 2))
        else:
            y0 = max(y0, int((cy + oy + oh) / 2))

if x1 - x0 < w or y1 - y0 < h:                         # clamped into nothing
    x0, y0 = max(0, x - int(w * 0.25)), max(0, y - int(h * 0.4))
    x1, y1 = min(W, x + w + int(w * 0.25)), min(H, y + h + int(h * 0.5))
crop = img[y0:y1, x0:x1]
if crop.size == 0:
    sys.exit(3)
cv2.imwrite(dst, crop)
print(f"face crop {x1-x0}x{y1-y0} from {W}x{H} ({len(faces)} faces seen)")
