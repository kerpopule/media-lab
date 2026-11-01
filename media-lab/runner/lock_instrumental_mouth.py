#!/usr/bin/env python3
"""Lock a singer's mouth closed during a known instrumental opening.

This is deliberately scoped to the approved 1120x640 close-up. It warps the
closed-mouth first source frame with the live global face/camera affine, then
feathers only the lower-face ellipse. It fails on any other composition.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import cv2
import numpy as np

SIZE = (1120, 640)
CENTER = (620, 286)
RADII = (88, 46)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--closed-source", required=True)
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--until", type=float, required=True)
    ap.add_argument("--fade-start", type=float, required=True)
    args = ap.parse_args()
    if not (0 < args.fade_start < args.until <= 2.0):
        raise SystemExit("invalid mouth-lock interval")

    cap = cv2.VideoCapture(args.video)
    src = cv2.VideoCapture(args.closed_source)
    ok, ref = src.read(); src.release()
    if not ok:
        raise SystemExit("closed-mouth source frame is unreadable")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if (width, height) != SIZE or ref.shape[1::-1] != SIZE or fps <= 0:
        raise SystemExit(f"mouth lock requires 1120x640 video, got {width}x{height} at {fps} fps")

    outdir = Path(args.frames_dir)
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)

    ref_gray = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    base_mask = np.zeros((height, width), np.float32)
    cv2.ellipse(base_mask, CENTER, RADII, 0, 0, 360, 1.0, -1)
    base_mask = cv2.GaussianBlur(base_mask, (0, 0), 10)

    count = locked = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = count / fps
        result = frame
        if t < args.until:
            cur_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
            warp = np.eye(2, 3, dtype=np.float32)
            try:
                cv2.findTransformECC(ref_gray, cur_gray, warp, cv2.MOTION_AFFINE,
                                     (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-6),
                                     None, 3)
            except cv2.error:
                pass
            closed = cv2.warpAffine(ref, warp, SIZE, flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_REFLECT)
            mask = cv2.warpAffine(base_mask, warp, SIZE, flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_CONSTANT)
            temporal = 1.0 if t <= args.fade_start else (args.until - t) / (args.until - args.fade_start)
            alpha = np.clip(mask * temporal, 0, 1)[..., None]
            result = np.clip(frame.astype(np.float32) * (1 - alpha) +
                             closed.astype(np.float32) * alpha, 0, 255).astype(np.uint8)
            locked += 1
        if not cv2.imwrite(str(outdir / f"frame-{count:06d}.png"), result):
            raise SystemExit("failed to write mouth-lock frame")
        count += 1
    cap.release()
    if count < 2:
        raise SystemExit("mouth-lock source had too few frames")
    print(f"MOUTH_LOCK_DONE frames={count} locked={locked} until={args.until:.3f} fade_start={args.fade_start:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
