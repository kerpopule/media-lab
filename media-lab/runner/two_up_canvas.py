#!/usr/bin/env python3
"""Place TWO characters on one canvas, side by side, for a two-hander shot.

Same reasoning as scene_canvas.py but for two people: each face is scaled to a
usable fraction of frame height (a small face cannot lip-sync — measured), they
are set left and right with a gap, and the surrounding grey is what the edit
model fills with the scene. The bottom rows are smeared to the frame edge so the
model paints torsos instead of returning a grey block.

REAL PEOPLE ARE NOT THE SAME SIZE. Scaling both faces to an identical fraction
makes a shorter, smaller person read as the same height as a taller one, and the
shot looks wrong even when both faces are perfect. Pass a per-subject scale
(right_rel) so the difference survives into the frame.

Usage: two_up_canvas.py <left.png> <right.png> <out.png> <W> <H> [face_frac] [right_rel]
  right_rel  size of the RIGHT subject relative to the left (default 1.0).
             A person ~5 inches shorter with a smaller frame is about 0.92.
Prints the face height each subject ended up with, as a fraction of the canvas.
"""
import sys

import cv2
import numpy as np

CASC = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"


def face_box(img):
    det = cv2.CascadeClassifier(CASC).detectMultiScale(
        cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 6, minSize=(40, 40))
    if len(det):
        return max(det, key=lambda f: f[2] * f[3])
    h, w = img.shape[:2]                      # no detect: assume a head crop
    fh = int(h * 0.55)
    return (w // 2 - fh // 2, int(h * 0.12), fh, fh)


def main():
    left_p, right_p, dst = sys.argv[1:4]
    W, H = int(sys.argv[4]), int(sys.argv[5])
    face_frac = float(sys.argv[6]) if len(sys.argv) > 6 else 0.26
    right_rel = float(sys.argv[7]) if len(sys.argv) > 7 else 1.0

    canvas = np.full((H, W, 3), 128, dtype=np.uint8)
    report = []
    for idx, path in enumerate((left_p, right_p)):
        img = cv2.imread(path)
        if img is None:
            sys.exit(1)
        fx, fy, fw, fh = face_box(img)
        want = face_frac * (right_rel if idx == 1 else 1.0)
        scale = (want * H) / fh
        nw, nh = max(2, int(img.shape[1] * scale)), max(2, int(img.shape[0] * scale))
        person = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LANCZOS4)
        # face centred on the quarter lines, head near the top
        target_cx = W * (0.28 if idx == 0 else 0.72)
        px = int(target_cx - (fx + fw / 2) * scale)
        # a shorter person's head STARTS LOWER in frame — that is what reads as
        # a height difference; equal head-tops just look like two equal people
        head_top = 0.16 + (1.0 - (right_rel if idx == 1 else 1.0)) * 1.6
        py = int(head_top * H - fy * scale)
        sx0, sy0 = max(0, -px), max(0, -py)
        dx0, dy0 = max(0, px), max(0, py)
        cw = min(nw - sx0, W - dx0)
        ch = min(nh - sy0, H - dy0)
        if cw <= 0 or ch <= 0:
            continue
        canvas[dy0:dy0 + ch, dx0:dx0 + cw] = person[sy0:sy0 + ch, sx0:sx0 + cw]
        bottom = dy0 + ch
        if bottom < H:
            band = canvas[max(dy0, bottom - 24):bottom, dx0:dx0 + cw]
            if band.size:
                canvas[bottom:H, dx0:dx0 + cw] = cv2.resize(
                    band, (cw, H - bottom), interpolation=cv2.INTER_LINEAR)
        report.append(f"{'left' if idx == 0 else 'right'} face {fh * scale:.0f}px "
                      f"({100 * fh * scale / H:.0f}% of frame, head top "
                      f"{100 * head_top:.0f}%)")
    cv2.imwrite(dst, canvas)
    print(f"two-up canvas {W}x{H}: " + ", ".join(report))


if __name__ == "__main__":
    main()
