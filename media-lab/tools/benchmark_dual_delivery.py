"""Reproducible local encoder benchmark; never invokes an AI engine or uploads.
Usage: PYTHONPATH=. python tools/benchmark_dual_delivery.py SOURCE NEW_OUTPUT_DIR
"""

import argparse
import contextlib
import io
import json
import platform
import subprocess
import time
from pathlib import Path

from media_lab_core import hdr_delivery, sdr_delivery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source_hash = sdr_delivery.digest(args.source)
    start = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        receipt = hdr_delivery.prepare(
            args.source,
            args.output / "hdr.mp4",
            args.output / "retained",
            sdr_target=args.output / "vibrant-sdr.mp4",
        )
    total = time.perf_counter() - start
    sdr = receipt["sdr_delivery"]
    source_after = sdr_delivery.digest(args.source)
    if source_after != source_hash:
        raise RuntimeError("Source changed during benchmark")
    video = next(s for s in sdr["probe"]["streams"] if s["codec_type"] == "video")
    result = {
        "scope": "One local finishing run, not an AI render or Spark throughput claim",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "ffmpeg": subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.splitlines()[0],
        "source_sha256": source_hash,
        "source_unchanged": True,
        "seconds": float(video["duration"]),
        "width": video["width"],
        "height": video["height"],
        "frames": video.get("nb_frames"),
        "cadence": video["avg_frame_rate"],
        "total_pair_seconds": round(total, 3),
        "additional_sdr_encode_seconds": round(sdr["encode_seconds"], 3),
        "additional_sdr_verify_seconds": round(sdr["verify_seconds"], 3),
        "additional_sdr_total_seconds": round(sdr["encode_seconds"] + sdr["verify_seconds"], 3),
        "hdr_sha256": receipt["sha256"],
        "sdr_sha256": sdr["sha256"],
        "hdr_container": receipt["container_colr"],
        "hdr_vui": receipt["codec_vui"],
        "sdr_container": sdr["container_colr"],
        "sdr_vui": sdr["codec_vui"],
        "full_decode_both_passed": True,
        "limits": "HDR compatible-display review required; inherited managed HDR is AVC High 8-bit, not a native 10-bit master",
    }
    (args.output / "benchmark.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
