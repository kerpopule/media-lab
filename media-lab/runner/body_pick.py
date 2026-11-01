#!/usr/bin/env python3
"""Pick ONE full-body panel out of a character's full-body reference sheet.

The close-up picker (sheet_pick.py) crops to a face, which is exactly the wrong
crop for a wide or action shot — it throws away the build, posture and clothing
that make a full-body take look like the same person. This one keeps the WHOLE
figure: ask the vision model for the panel showing the person standing
front-on, full length, head to feet, then verify the crop actually contains a
head-to-toe figure (a face detected in the upper fifth of a tall crop).

Usage: body_pick.py <in-image> <out-png>
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
    "This image is a character reference sheet: several photo panels of the same person, usually "
    "showing front, three-quarter, side and back views.\n"
    "Reply with ONLY a JSON object, no prose:\n"
    '{"panels": <how many separate photo panels>, "box": [x0, y0, x1, y1]}\n'
    "box gives the ONE panel showing the person STANDING FRONT-ON, full length, head to feet, "
    "facing the camera. Not the side view, not the back view. Use fractions of the image "
    "width/height from 0 to 1, top-left origin. The box must lie strictly INSIDE that panel and "
    "must contain the whole figure including the feet. If the image is a single photo, answer "
    '{"panels": 1, "box": [0, 0, 1, 1]}.'
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
    m = re.search(r"\{.*\}", d["choices"][0]["message"]["content"], re.S)
    if not m:
        raise ValueError("no json in reply")
    return json.loads(m.group(0))


def main():
    src, dst = sys.argv[1:3]
    img = cv2.imread(src)
    if img is None:
        sys.exit(1)
    H, W = img.shape[:2]
    ans = ask_vision(src)
    box = [float(v) for v in (ans.get("box") or [])]
    if len(box) != 4:
        sys.exit(2)
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        sys.exit(3)
    dx, dy = (x1 - x0) * 0.01, (y1 - y0) * 0.01
    px0, py0 = int((x0 + dx) * W), int((y0 + dy) * H)
    px1, py1 = int((x1 - dx) * W), int((y1 - dy) * H)
    crop = img[py0:py1, px0:px1]
    if crop.size == 0 or crop.shape[0] < 200:
        sys.exit(4)
    ch, cw = crop.shape[:2]
    if ch < cw:
        print(f"rejected: {cw}x{ch} is not a standing figure", file=sys.stderr)
        sys.exit(5)
    # verify: exactly one face, and it sits high in the frame — that is what a
    # head-to-toe standing shot looks like. A face in the middle means the panel
    # was cropped to a portrait.
    faces = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml").detectMultiScale(
            cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), 1.1, 6, minSize=(30, 30))
    if len(faces) != 1:
        print(f"rejected: {len(faces)} faces in the chosen box", file=sys.stderr)
        sys.exit(6)
    fx, fy, fw, fh = faces[0]
    if (fy + fh) > ch * 0.45:
        print(f"rejected: face sits at {100*(fy+fh)/ch:.0f}% down — not full length",
              file=sys.stderr)
        sys.exit(7)
    cv2.imwrite(dst, crop)
    print(f"body pick {cw}x{ch} from {W}x{H} (model saw {ans.get('panels')} panels, "
          f"head at {100*fy/ch:.0f}%)")


if __name__ == "__main__":
    main()
