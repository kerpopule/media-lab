"""Deterministic MuseTalk preprocessing for stable close-up source videos.

This avoids MuseTalk's heavyweight whole-body pose stack. It detects the largest
frontal face with OpenCV, rejects smaller beach-background false positives, and
smooths the crop coordinates to prevent talking-bubble jitter.
"""
from __future__ import annotations

import cv2
import numpy as np
from tqdm import tqdm

coord_placeholder = (0.0, 0.0, 0.0, 0.0)


def read_imgs(img_list):
    frames = []
    print("reading images...")
    for img_path in tqdm(img_list):
        frame = cv2.imread(img_path)
        if frame is None:
            raise RuntimeError(f"could not read frame: {img_path}")
        frames.append(frame)
    return frames


def _largest_face(cascade, frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    detections = cascade.detectMultiScale(
        gray,
        scaleFactor=1.08,
        minNeighbors=5,
        minSize=(80, 80),
    )
    if len(detections) == 0:
        return None
    x, y, w, h = max(detections, key=lambda b: int(b[2]) * int(b[3]))
    return np.asarray([x, y, x + w, y + h], dtype=np.float64)


def _expanded_box(box, width, height, bbox_shift):
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    # Include the jaw/chin while keeping the stable forehead and cheek geometry.
    x1 -= 0.035 * w
    x2 += 0.035 * w
    y1 -= 0.045 * h
    y2 += 0.14 * h + float(bbox_shift)
    return np.asarray([
        max(0.0, x1), max(0.0, y1), min(float(width), x2), min(float(height), y2)
    ])


def _smooth(raw):
    if not raw:
        return []
    # Centered five-frame median removes one-frame detector wobble.
    med = []
    for i in range(len(raw)):
        lo, hi = max(0, i - 2), min(len(raw), i + 3)
        med.append(np.median(np.stack(raw[lo:hi]), axis=0))
    # Gentle temporal smoothing preserves the accepted walking motion.
    out = [med[0]]
    alpha = 0.42
    for box in med[1:]:
        out.append(alpha * box + (1.0 - alpha) * out[-1])
    # Backward pass removes lag without changing the deterministic result.
    back = [out[-1]]
    for box in reversed(out[:-1]):
        back.append(alpha * box + (1.0 - alpha) * back[-1])
    back.reverse()
    return [(int(round(b[0])), int(round(b[1])), int(round(b[2])), int(round(b[3]))) for b in back]


def get_landmark_and_bbox(img_list, upperbondrange=0):
    frames = read_imgs(img_list)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        raise RuntimeError("OpenCV frontal-face cascade is unavailable")
    raw = []
    previous = None
    missed = 0
    for frame in tqdm(frames, desc="detecting stable face boxes"):
        found = _largest_face(cascade, frame)
        if found is None:
            missed += 1
            if previous is None:
                height, width = frame.shape[:2]
                size = min(width, height) * 0.46
                found = np.asarray([
                    width * 0.5 - size * 0.5,
                    height * 0.37 - size * 0.5,
                    width * 0.5 + size * 0.5,
                    height * 0.37 + size * 0.5,
                ])
            else:
                found = previous.copy()
        height, width = frame.shape[:2]
        expanded = _expanded_box(found, width, height, upperbondrange)
        raw.append(expanded)
        previous = found
    coords = _smooth(raw)
    print(f"OpenCV close-up boxes: frames={len(coords)} detector_misses={missed} bbox_shift={upperbondrange}")
    return coords, frames


def get_bbox_range(img_list, upperbondrange=0):
    coords, _ = get_landmark_and_bbox(img_list, upperbondrange)
    heights = [y2 - y1 for _, y1, _, y2 in coords]
    return f"Total frames: {len(coords)}; median crop height: {int(np.median(heights))}; bbox_shift: {upperbondrange}"
