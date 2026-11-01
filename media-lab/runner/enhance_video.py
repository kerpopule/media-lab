#!/usr/bin/env python3
"""Media Lab finishing suite — face restore (GFPGAN) and 2x upscale (Real-ESRGAN)
for finished videos. Runs inside the image ComfyUI's venv (torch + spandrel + CUDA),
streaming frames through ffmpeg pipes so memory stays flat.

  enhance_video.py --src in.mp4 --dst out.mp4 --ops faces,upscale --models DIR

Models expected in DIR:
  GFPGANv1.4.pth                        (face restoration, 512x512 aligned crops)
  RealESRGAN_x2plus.pth                 (2x whole-frame upscale)
  face_detection_yunet_2023mar.onnx     (OpenCV YuNet face detector)

Face pass: detect faces per frame, restore each on a square crop, feather-blend
back at partial strength — full-strength per-frame restoration flickers; 0.65
keeps the fix while staying temporally calm.
"""
import argparse, json, subprocess, sys
from pathlib import Path

import numpy as np
import torch

p = argparse.ArgumentParser()
p.add_argument("--src", required=True)
p.add_argument("--dst", required=True)
p.add_argument("--ops", default="faces")
p.add_argument("--models", required=True)
p.add_argument("--face-blend", type=float, default=0.65)
args = p.parse_args()

SRC, DST, MODELS = Path(args.src), Path(args.dst), Path(args.models)
OPS = [o for o in args.ops.split(",") if o in ("faces", "upscale")]
if not OPS:
    sys.exit("no valid ops")
DEV = "cuda" if torch.cuda.is_available() else "cpu"

probe = json.loads(subprocess.run(
    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
     "stream=width,height,r_frame_rate", "-of", "json", str(SRC)],
    capture_output=True, text=True).stdout)["streams"][0]
W, H = int(probe["width"]), int(probe["height"])
num, den = probe["r_frame_rate"].split("/")
FPS = float(num) / float(den or 1)

from spandrel import ModelLoader
loader = ModelLoader(device=DEV)

face_model = face_det = None
if "faces" in OPS:
    import cv2
    face_model = loader.load_from_file(str(MODELS / "GFPGANv1.4.pth")).model.eval().to(DEV)
    face_det = cv2.FaceDetectorYN_create(str(MODELS / "face_detection_yunet_2023mar.onnx"),
                                         "", (W, H), score_threshold=0.6)
up_model = None
if "upscale" in OPS:
    up = loader.load_from_file(str(MODELS / "RealESRGAN_x2plus.pth"))
    up_model = up.model.eval().to(DEV)
    SCALE = up.scale
else:
    SCALE = 1

OW, OH = W * SCALE, H * SCALE

def to_tensor(bgr):
    rgb = np.ascontiguousarray(bgr[:, :, ::-1], dtype=np.float32) / 255.0
    return torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).to(DEV)

def to_bgr(t):
    arr = t.squeeze(0).permute(1, 2, 0).clamp(0, 1).mul(255).round().byte().cpu().numpy()
    return np.ascontiguousarray(arr[:, :, ::-1])

def fix_faces(frame):
    import cv2
    face_det.setInputSize((frame.shape[1], frame.shape[0]))
    _, faces = face_det.detect(frame)
    if faces is None:
        return frame
    out = frame.copy()
    for f in faces[:6]:
        x, y, w, h = [int(v) for v in f[:4]]
        if w < 16 or h < 16:
            continue
        # square crop with headroom around the detection box
        cx, cy, s = x + w / 2, y + h / 2, int(max(w, h) * 1.8)
        x0, y0 = max(0, int(cx - s / 2)), max(0, int(cy - s / 2))
        x1, y1 = min(frame.shape[1], x0 + s), min(frame.shape[0], y0 + s)
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        ch, cw = crop.shape[:2]
        inp = cv2.resize(crop, (512, 512), interpolation=cv2.INTER_LINEAR)
        with torch.no_grad():
            res = face_model(to_tensor(inp))
        if isinstance(res, (tuple, list)):   # GFPGAN returns (image, intermediates)
            res = res[0]
        restored = to_bgr(res)
        restored = cv2.resize(restored, (cw, ch), interpolation=cv2.INTER_LINEAR)
        # feathered elliptical mask, partial blend — calm over crisp
        mask = np.zeros((ch, cw), np.float32)
        cv2.ellipse(mask, (cw // 2, ch // 2), (int(cw * 0.42), int(ch * 0.42)), 0, 0, 360, 1.0, -1)
        k = max(3, (min(ch, cw) // 8) | 1)
        mask = cv2.GaussianBlur(mask, (k, k), 0)[:, :, None] * args.face_blend
        out[y0:y1, x0:x1] = (restored * mask + crop * (1 - mask)).astype(np.uint8)
    return out

def upscale(frame):
    with torch.no_grad():
        return to_bgr(up_model(to_tensor(frame)))

dec = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(SRC),
                        "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                       stdout=subprocess.PIPE, bufsize=W * H * 3 * 4)
tmp = DST.with_suffix(".video.mp4")
enc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-y",
                        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OW}x{OH}",
                        "-r", f"{FPS:.6f}", "-i", "-",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)],
                       stdin=subprocess.PIPE)
n = 0
fsize = W * H * 3
while True:
    buf = dec.stdout.read(fsize)
    if len(buf) < fsize:
        break
    frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
    if "faces" in OPS:
        frame = fix_faces(frame)
    if "upscale" in OPS:
        frame = upscale(frame)
    enc.stdin.write(frame.tobytes())
    n += 1
    if n % 48 == 0:
        print(f"FRAME {n}", flush=True)
enc.stdin.close()
dec.wait()
if enc.wait() != 0 or not tmp.exists():
    sys.exit("encode failed")

# mux the original audio back in untouched
mux = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(tmp), "-i", str(SRC),
                      "-map", "0:v", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                      "-movflags", "+faststart", str(DST)])
tmp.unlink(missing_ok=True)
if mux.returncode != 0 or not DST.exists():
    sys.exit("mux failed")
print(f"DONE frames={n} out={DST}", flush=True)
