#!/usr/bin/env python3
"""Print the largest face's height as a fraction of the image height (0 if none).

The lip-sync gate: LTX animates the mouth from pixels, so a start frame whose
face is small produces mush. Approved takes measured 0.40; the take judged
called "horrible" measured 0.12.

Usage: face_frac.py <image>
"""
import sys

import cv2

img = cv2.imread(sys.argv[1])
if img is None:
    print("0")
    sys.exit(0)
H = img.shape[0]
faces = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml").detectMultiScale(
        cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 6, minSize=(30, 30))
print(f"{(max(f[3] for f in faces) / H) if len(faces) else 0:.3f}")
