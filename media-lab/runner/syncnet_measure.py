#!/usr/bin/env python3
"""Objective lip-sync measurement for one talking clip (SyncNet, CPU).

    syncnet_measure.py VIDEO --root LATENTSYNC_ROOT [--work DIR]

Prints one JSON object: {"av_offset_frames", "confidence", "min_distance",
"face_track": bool}. Offset is how many frames the sound is shifted against
the mouth (0 is perfect; the private lip-sync gate accepts |offset| <= 1 with
confidence >= 5). Needs a LatentSync checkout (its SyncNet evaluator, S3FD
face detector and ``checkpoints/auxiliary/syncnet_v2.model``) and that
checkout's interpreter; the critic calls it through MEDIA_LAB_SYNCNET_PYTHON
and MEDIA_LAB_LATENTSYNC_ROOT and says "not measured" when they are unset.
Runs on the CPU and never touches the GPU lease.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--root", required=True)
    ap.add_argument("--work")
    args = ap.parse_args()
    root = Path(args.root)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "eval"))
    from syncnet_detect import SyncNetDetector          # noqa: E402  (LatentSync checkout)
    from eval.syncnet.syncnet_eval import SyncNetEval   # noqa: E402

    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="syncnet-"))
    work.mkdir(parents=True, exist_ok=True)
    detector = SyncNetDetector(device="cpu", detect_results_dir=str(work / "detect"))
    detector(args.video, min_track=30, scale=False)
    track = work / "detect" / "crop" / "00000.mp4"
    if not track.is_file():
        print(json.dumps({"face_track": False, "av_offset_frames": None, "confidence": None}))
        return 0
    model = SyncNetEval(device="cpu")
    model.loadParameters(str(root / "checkpoints" / "auxiliary" / "syncnet_v2.model"))
    offset, distance, confidence = model.evaluate(str(track), temp_dir=str(work / "evaluate"),
                                                  batch_size=20, vshift=15)
    print(json.dumps({"face_track": True, "av_offset_frames": int(offset),
                      "min_distance": float(distance), "confidence": float(confidence)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
