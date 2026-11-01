#!/usr/bin/env python3
"""Pick ONE clean portrait panel out of a character reference sheet, using the
Spark's vision model to choose the panel and OpenCV to verify the choice.

Why: an uploaded "reference photo" is very often a multi-pose contact sheet.
A face cascade alone picks a face but cannot see panel boundaries, so its
margins used to swallow the top of the pose in the next cell — and that
neighbour rode into the video as a second person at the bottom of frame.

The vision model reads the layout ("6 panels, 3x2") and returns the box of the
best front-facing head-and-shoulders panel; we then verify the crop contains
exactly one face before accepting it. Any doubt -> exit non-zero and let the
caller fall back to face_crop.py.

Usage: sheet_pick.py <in-image> <out-png>
"""
import base64
import json
import os
import re
import sys
import urllib.request

import cv2

CHAT = "http://127.0.0.1:8003/v1/chat/completions"
MODEL = os.getenv("MEDIA_LAB_TEXT_MODEL", "media-lab-text")
ASK = (
    "This image may be a character reference sheet made of several photo panels of the same "
    "person, or it may be a single photo.\n"
    "Reply with ONLY a JSON object, no prose:\n"
    '{"panels": <how many separate photo panels>, '
    '"box": [x0, y0, x1, y1]}\n'
    "box gives the ONE panel that shows the clearest front-facing head-and-shoulders view of the "
    "person (eyes visible, facing the camera, not a full-body shot, not a profile). When several "
    "panels qualify, choose the LARGEST one — the crop is used as a start frame, so resolution "
    "matters. Use fractions "
    "of the image width/height from 0 to 1, top-left origin. The box must lie strictly INSIDE that "
    "panel — it must not touch or include any part of a neighbouring panel. "
    'If the image is a single photo, answer {"panels": 1, "box": [0, 0, 1, 1]}.'
)


def ask_vision(path):
    img = base64.b64encode(open(path, "rb").read()).decode()
    body = {"model": MODEL, "max_tokens": 200, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": ASK},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}}]}]}
    req = urllib.request.Request(CHAT, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=180))
    txt = d["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError(f"no json in reply: {txt[:200]}")
    return json.loads(m.group(0))


def faces_in(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(40, 40))


def main():
    src, dst = sys.argv[1:3]
    img = cv2.imread(src)
    if img is None:
        sys.exit(1)
    H, W = img.shape[:2]
    ans = ask_vision(src)
    box = [float(v) for v in ans.get("box") or []]
    if len(box) != 4:
        sys.exit(2)
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        sys.exit(3)
    # pull in 1% on every side: models tend to land ON the panel seam
    dx, dy = (x1 - x0) * 0.01, (y1 - y0) * 0.01
    px0, py0 = int((x0 + dx) * W), int((y0 + dy) * H)
    px1, py1 = int((x1 - dx) * W), int((y1 - dy) * H)
    crop = img[py0:py1, px0:px1]
    if crop.size == 0 or crop.shape[0] < 120 or crop.shape[1] < 120:
        sys.exit(4)
    # VERIFY: the whole point is one person, so one face — no face means the
    # model picked scenery, several means it straddled a seam
    n = len(faces_in(crop))
    if n != 1:
        print(f"rejected: {n} faces inside the chosen box", file=sys.stderr)
        sys.exit(5)
    cv2.imwrite(dst, crop)
    print(f"sheet pick {crop.shape[1]}x{crop.shape[0]} from {W}x{H} "
          f"(model saw {ans.get('panels')} panels)")


if __name__ == "__main__":
    main()
