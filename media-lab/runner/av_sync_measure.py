#!/usr/bin/env python3
"""Measure audio-visual (lip) sync of a talking clip with SyncNet.

SyncNet (Chung & Zisserman, 2016) scores how well a 0.2 s window of mouth
pictures matches a 0.2 s window of sound; sliding the sound against the
pictures finds the offset where they match best. This is the standard
objective lip-sync measure (it is what LatentSync and MuseTalk report).

Protocol:
  * video at its own frame rate (``--fps``, default 24: every source frame is
    kept, no 24->25 duplication) with the sound sped up by 25/fps so SyncNet
    sees its trained 25 fps / 16 kHz geometry; ``--fps 0`` resamples to 25 fps
    instead (the syncnet_python protocol; both agree within ~1 ms here);
  * S3FD face detection on every frame, largest face, median-smoothed crop,
    224x224 (the syncnet_python / LatentSync crop);
  * distance curve over +-15 frames, then re-measured with the sound delayed
    by 1/4, 2/4 and 3/4 of a frame for a ~10 ms grid, refined with a parabola.

Sign: ``audio_lag_ms`` > 0 means the sound comes AFTER the lips (late sound);
< 0 means it comes before them. ``--self-test`` delays the sound by -120 and
+120 ms and reports what it measured, so every run carries its own calibration.

Runs on CPU in the SyncNet host environment (torch, torchvision, opencv,
scipy, python_speech_features). Weights and model code come from a SyncNet
checkout given by ``--syncnet-root`` (the LatentSync layout:
``eval/syncnet/syncnet.py``, ``eval/detectors/``,
``checkpoints/auxiliary/{syncnet_v2.model,sfd_face.pth}``). Nothing is
downloaded. Prints one JSON object per video.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

VSHIFT = 15
SYNCNET_FPS = 25.0
ROOT = None  # set by main()


def _load_syncnet_modules(root: Path):
    sys.path.insert(0, str(root))
    # LatentSync's detector imports a downloader; this tool never downloads.
    pkg = types.ModuleType("latentsync"); sub = types.ModuleType("latentsync.utils")
    util = types.ModuleType("latentsync.utils.util")
    util.check_model_and_download = lambda *a, **k: None
    sys.modules.setdefault("latentsync", pkg)
    sys.modules.setdefault("latentsync.utils", sub)
    sys.modules.setdefault("latentsync.utils.util", util)


def sh(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Measure:
    def __init__(self, root: Path, fps: float, threads: int):
        import torch
        torch.set_num_threads(max(1, threads))
        self.root = root
        self.fps = fps  # 0 = resample to 25
        self.step_fps = fps if fps else SYNCNET_FPS
        _load_syncnet_modules(root)
        from eval.syncnet.syncnet import S
        self.torch = torch
        self.net = S(num_layers_in_fc_layers=1024).eval()
        state = torch.load(str(root / "checkpoints/auxiliary/syncnet_v2.model"),
                           map_location="cpu", weights_only=True)
        own = self.net.state_dict()
        for key, value in state.items():
            own[key].copy_(value)
        cwd = os.getcwd()
        os.chdir(root)  # S3FD opens its weight by a relative path
        try:
            from eval.detectors import S3FD
            self.detector = S3FD(device="cpu")
        finally:
            os.chdir(cwd)

    # ---- media
    def frames(self, video: Path, work: Path):
        import cv2
        out = work / "frames.mp4"
        rate = [] if self.fps else ["-r", str(int(SYNCNET_FPS))]
        sh(["ffmpeg", "-y", "-nostdin", "-i", str(video), "-an", *rate, "-qscale:v", "2", str(out)])
        cap = cv2.VideoCapture(str(out)); frames = []
        while True:
            ok, image = cap.read()
            if not ok:
                break
            frames.append(image)
        return frames

    def audio(self, video: Path, work: Path, delay_ms: int):
        from scipy.io import wavfile
        wav = work / f"a{delay_ms:+d}.wav"
        filters = []
        if delay_ms > 0:
            filters = ["-af", f"adelay={delay_ms}:all=1"]
        elif delay_ms < 0:
            filters = ["-af", f"atrim=start={-delay_ms / 1000:.4f},asetpts=PTS-STARTPTS"]
        # native-rate mode: resample so the SAME samples read at 16 kHz play
        # 25/fps faster, matching the frames read as 25 fps
        rate = str(int(round(16000 * self.fps / SYNCNET_FPS))) if self.fps else "16000"
        sh(["ffmpeg", "-y", "-nostdin", "-i", str(video), "-vn", "-ac", "1", "-ar", rate,
            *filters, "-acodec", "pcm_s16le", str(wav)])
        _, samples = wavfile.read(str(wav))
        return samples

    def crops(self, frames):
        import cv2
        import numpy as np
        from scipy import signal
        boxes = []
        for image in frames:
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            scale = 0.25 if max(image.shape[:2]) >= 1000 else 0.5
            found = self.detector.detect_faces(rgb, conf_th=0.9, scales=[scale])
            if len(found):
                areas = (found[:, 2] - found[:, 0]) * (found[:, 3] - found[:, 1])
                boxes.append(found[int(np.argmax(areas))][:4])
            else:
                boxes.append(None)
        have = [i for i, b in enumerate(boxes) if b is not None]
        coverage = len(have) / max(1, len(frames))
        if len(have) < 10:
            return None, {"frames": len(frames), "face_frames": len(have), "coverage": round(coverage, 3)}
        arr = np.array([boxes[i] for i in have])
        full = np.stack([np.interp(np.arange(len(frames)), have, arr[:, j]) for j in range(4)], 1)
        size = signal.medfilt(np.maximum(full[:, 3] - full[:, 1], full[:, 2] - full[:, 0]) / 2, 13)
        cy = signal.medfilt((full[:, 1] + full[:, 3]) / 2, 13)
        cx = signal.medfilt((full[:, 0] + full[:, 2]) / 2, 13)
        out, pad_scale = [], 0.4
        for i, image in enumerate(frames):
            bs = size[i]; bsi = int(bs * (1 + 2 * pad_scale))
            padded = np.pad(image, ((bsi, bsi), (bsi, bsi), (0, 0)), "constant", constant_values=(110, 110))
            my, mx = cy[i] + bsi, cx[i] + bsi
            face = padded[int(my - bs): int(my + bs * (1 + 2 * pad_scale)),
                          int(mx - bs * (1 + pad_scale)): int(mx + bs * (1 + pad_scale))]
            out.append(cv2.resize(face, (224, 224)))
        return out, {"frames": len(frames), "face_frames": len(have), "coverage": round(coverage, 3),
                     "face_px": round(float(np.mean(size) * 2), 1)}

    # ---- features
    def video_features(self, crops):
        import numpy as np
        torch = self.torch
        stack = np.transpose(np.stack(crops, axis=3)[None], (0, 3, 4, 1, 2)).astype(np.float32)
        tensor = torch.from_numpy(stack)
        n = len(crops) - 5; feats = []
        with torch.no_grad():
            for i in range(0, n, 20):
                batch = torch.cat([tensor[:, :, v:v + 5] for v in range(i, min(n, i + 20))], 0)
                feats.append(self.net.forward_lip(batch))
        return torch.cat(feats, 0)

    def audio_features(self, samples, n):
        import numpy as np
        import python_speech_features
        torch = self.torch
        mfcc = np.stack([np.array(c) for c in zip(*python_speech_features.mfcc(samples, 16000))])
        tensor = torch.from_numpy(mfcc[None, None].astype(np.float32))
        n = min(n, tensor.shape[-1] // 4 - 5); feats = []
        with torch.no_grad():
            for i in range(0, n, 20):
                batch = torch.cat([tensor[:, :, :, v * 4: v * 4 + 20] for v in range(i, min(n, i + 20))], 0)
                feats.append(self.net.forward_aud(batch))
        return torch.cat(feats, 0)

    def distances(self, vf, af):
        torch = self.torch
        n = min(len(vf), len(af)); vf, af = vf[:n], af[:n]
        padded = torch.nn.functional.pad(af, (0, 0, VSHIFT, VSHIFT))
        rows = [torch.nn.functional.pairwise_distance(vf[[i]].repeat(2 * VSHIFT + 1, 1),
                                                      padded[i:i + 2 * VSHIFT + 1]) for i in range(n)]
        return torch.stack(rows, 1).numpy()  # [offsets, frames]

    # ---- one clip
    def run(self, video: Path, extra_delay_ms: int = 0, window: int = 25) -> dict:
        import numpy as np
        work = Path(tempfile.mkdtemp(prefix="avsync-"))
        try:
            frames = self.frames(video, work)
            crops, face = self.crops(frames)
            if crops is None:
                return {"video": str(video), "ok": False, "error": "no face track", "face": face}
            vf = self.video_features(crops)
            quarter = int(round(1000 / self.step_fps / 4))
            points, mats, coarse, first_audio = [], {}, None, None
            for pre in (0, quarter, 2 * quarter, 3 * quarter):
                samples = self.audio(video, work, pre + extra_delay_ms)
                if pre == 0:
                    first_audio = samples
                mat = self.distances(vf, self.audio_features(samples, len(crops) - 5))
                mats[pre] = mat
                curve = mat.mean(1)
                if pre == 0:
                    coarse = curve
                for k, d in enumerate(curve):
                    points.append(((k - VSHIFT) * 1000 / self.step_fps - pre, float(d)))
            lag, _ = _refine(points)
            k0 = int(np.argmin(coarse))
            conf = float(np.median(coarse) - coarse[k0])
            windows = []
            nfr = min(m.shape[1] for m in mats.values())
            samples = first_audio.astype(np.float64)
            per_frame = 16000 / SYNCNET_FPS  # samples per SyncNet frame step
            for start in range(0, max(1, nfr - window + 1), max(1, window // 2)):
                wp = []
                for pre, mat in mats.items():
                    for k, d in enumerate(mat[:, start:start + window].mean(1)):
                        wp.append(((k - VSHIFT) * 1000 / self.step_fps - pre, float(d)))
                wlag, wconf = _refine(wp)
                seg = samples[int(start * per_frame): int((start + window) * per_frame)]
                rms = math.sqrt(float(np.mean(seg ** 2))) / 32768 if len(seg) else 0.0
                windows.append({"t0": round(start / self.step_fps, 2),
                                "t1": round((start + window) / self.step_fps, 2),
                                "audio_lag_ms": round(wlag, 1), "conf": round(wconf, 2),
                                "rms_dbfs": round(20 * math.log10(max(rms, 1e-9)), 1)})
            return {"video": str(video), "ok": True, "tool": "syncnet_v2",
                    "mode": f"native-{self.fps:g}fps" if self.fps else "resample-25fps",
                    "audio_lag_ms": round(lag, 1),
                    "syncnet_offset_frames": int(VSHIFT - k0),
                    "confidence": round(conf, 3), "min_dist": round(float(coarse[k0]), 3),
                    "applied_delay_ms": extra_delay_ms, "face": face, "windows": windows}
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _refine(points):
    import numpy as np
    points = sorted(points)
    lags = np.array([p[0] for p in points]); dists = np.array([p[1] for p in points])
    j = int(np.argmin(dists)); best = float(lags[j])
    if 0 < j < len(dists) - 1:
        x = lags[j - 1:j + 2]; y = dists[j - 1:j + 2]
        a, b, _ = np.polyfit(x, y, 2)
        if a > 0:
            best = float(np.clip(-b / (2 * a), x[0], x[-1]))
    return best, float(np.median(dists) - dists[j])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--syncnet-root", required=True)
    ap.add_argument("--fps", type=float, default=24.0, help="source frame rate; 0 resamples to 25")
    ap.add_argument("--window", type=int, default=25, help="per-window report length in frames")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", help="also write all results to this file")
    args = ap.parse_args(argv)
    root = Path(args.syncnet_root).expanduser().resolve()
    tool = Measure(root, args.fps, args.threads)
    results = []
    for video in [Path(v).resolve() for v in args.videos]:
        result = tool.run(video, window=args.window)
        if args.self_test and result.get("ok"):
            result["self_test"] = {}
            for delay in (-120, 120):
                probe = tool.run(video, extra_delay_ms=delay, window=args.window)
                result["self_test"][str(delay)] = {
                    "expected": round(result["audio_lag_ms"] + delay, 1),
                    "measured": probe.get("audio_lag_ms")}
        print(json.dumps(result), flush=True)
        results.append(result)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
    return 0 if all(r.get("ok") for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
