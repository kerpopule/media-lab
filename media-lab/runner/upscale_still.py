#!/usr/bin/env python3
"""Sharpen a small likeness crop before it is used as a start frame.

Why: the vision picker takes ONE panel out of a contact sheet, and a busy sheet
gives a small panel — one came out 241x352 from a 1536x1024 sheet. Blown
up to fill a 704x1280 frame that is very soft, and a soft face gives both the
edit model (which redraws the person smaller) and the video model (which has no
mouth detail to animate) nothing to hold on to.

Real-ESRGAN 2x, then GFPGAN on the face at a gentle blend — the same models the
finishing suite uses, so no new weights. Runs in the image ComfyUI venv.

Usage: upscale_still.py <in> <out> [--min-long-side 640]
"""
import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
import torch

MODELS = Path(__file__).resolve().parent / "models"

ap = argparse.ArgumentParser()
ap.add_argument("src")
ap.add_argument("dst")
ap.add_argument("--min-long-side", type=int, default=640)
ap.add_argument("--face-blend", type=float, default=0.6)
args = ap.parse_args()

img = cv2.imread(args.src)
if img is None:
    sys.exit(1)
h, w = img.shape[:2]
if max(h, w) >= args.min_long_side:
    print(f"already {w}x{h} — no upscale needed")
    Path(args.dst).write_bytes(Path(args.src).read_bytes())
    sys.exit(0)

# The GPU is usually busy with a 40 GB video engine; this is a small image, so
# falling back to CPU costs seconds and never blocks a render.
import os
DEV = os.environ.get("UPSCALE_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
from spandrel import ModelLoader


def make_loader():
    return ModelLoader(device=DEV)


def to_tensor(bgr):
    rgb = np.ascontiguousarray(bgr[:, :, ::-1], dtype=np.float32) / 255.0
    return torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).to(DEV)


def to_bgr(t):
    arr = t.squeeze(0).permute(1, 2, 0).clamp(0, 1).mul(255).round().byte().cpu().numpy()
    return np.ascontiguousarray(arr[:, :, ::-1])


def run_upscale():
    loader = make_loader()
    up = loader.load_from_file(str(MODELS / "RealESRGAN_x2plus.pth"))
    model = up.model.eval().to(DEV)
    o, n = img, 0
    while max(o.shape[:2]) < args.min_long_side and n < 3:
        with torch.no_grad():
            o = to_bgr(model(to_tensor(o)))
        n += 1
    return o, n, loader


try:
    out, passes, loader = run_upscale()
except Exception as e:
    print(f"{DEV} upscale failed ({str(e)[:80]}) — retrying on CPU", file=sys.stderr)
    DEV = "cpu"
    out, passes, loader = run_upscale()

# gentle face restore on the result — the eyes and mouth are what matter
try:
    face_model = loader.load_from_file(str(MODELS / "GFPGANv1.4.pth")).model.eval().to(DEV)
    det = cv2.FaceDetectorYN_create(str(MODELS / "face_detection_yunet_2023mar.onnx"), "",
                                    (out.shape[1], out.shape[0]), score_threshold=0.6)
    det.setInputSize((out.shape[1], out.shape[0]))
    _, faces = det.detect(out)
    if faces is not None and len(faces):
        f = max(faces, key=lambda r: r[2] * r[3])
        x, y, fw, fh = [int(v) for v in f[:4]]
        cx, cy, s = x + fw / 2, y + fh / 2, int(max(fw, fh) * 1.8)
        x0, y0 = max(0, int(cx - s / 2)), max(0, int(cy - s / 2))
        x1, y1 = min(out.shape[1], x0 + s), min(out.shape[0], y0 + s)
        crop = out[y0:y1, x0:x1]
        if crop.size:
            ch, cw = crop.shape[:2]
            with torch.no_grad():
                res = face_model(to_tensor(cv2.resize(crop, (512, 512))))
            if isinstance(res, (tuple, list)):
                res = res[0]
            rest = cv2.resize(to_bgr(res), (cw, ch))
            mask = np.zeros((ch, cw), np.float32)
            cv2.ellipse(mask, (cw // 2, ch // 2), (int(cw * .42), int(ch * .42)), 0, 0, 360, 1., -1)
            k = max(3, (min(ch, cw) // 8) | 1)
            mask = cv2.GaussianBlur(mask, (k, k), 0)[:, :, None] * args.face_blend
            out[y0:y1, x0:x1] = (rest * mask + crop * (1 - mask)).astype(np.uint8)
except Exception as e:
    print(f"face restore skipped: {e}", file=sys.stderr)

cv2.imwrite(args.dst, out)
print(f"upscaled {w}x{h} -> {out.shape[1]}x{out.shape[0]} ({passes} pass(es))")
