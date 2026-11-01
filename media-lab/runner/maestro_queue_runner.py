#!/usr/bin/env python3
"""Run one Maestro/WanGP settings JSON and write a structured receipt.

Executed inside the long-lived maestro-gui container by Media Lab's queue worker.
Paths in settings must be container-visible (normally /data/...).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path("/opt/maestro/app/app")


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: maestro_queue_runner.py SETTINGS_JSON RECEIPT_JSON GPU_DELEGATION_JSON", flush=True)
        return 2
    settings_path = Path(sys.argv[1])
    receipt_path = Path(sys.argv[2])
    delegation_path = Path(sys.argv[3])
    try:
        delegated = json.loads(delegation_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise PermissionError("exact Maestro GPU lease delegation is required") from exc
    supplied = {
        "fence": os.environ.get("MEDIA_LAB_GPU_FENCE", ""),
        "job_id": os.environ.get("MEDIA_LAB_GPU_JOB_ID", ""),
        "engine": os.environ.get("MEDIA_LAB_GPU_ENGINE", ""),
        "task": os.environ.get("MEDIA_LAB_GPU_TASK", ""),
    }
    exact = {key: delegated.get(key) for key in supplied}
    try:
        fresh = 0 <= time.time() - float(delegated.get("issued_at")) <= 120
    except (TypeError, ValueError):
        fresh = False
    if (exact != supplied or not fresh or supplied["engine"] != "maestro"
            or supplied["task"] != "generate" or not supplied["fence"].isdigit()
            or not supplied["job_id"]):
        raise PermissionError("exact Maestro GPU lease delegation is required")
    # One invocation consumes one controller-staged delegation. A stale runner
    # cannot replay it after this process has started.
    delegation_path.unlink()
    sys.path.insert(0, str(ROOT))
    from shared.api import init
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict) or not settings.get("model_type"):
        raise ValueError("Maestro settings require model_type")

    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    session = init(
        root=ROOT,
        config_path=Path("/data/config/wgp_config.json"),
        output_dir=Path("/data/outputs"),
        cli_args=["--attention", "sdpa", "--profile", "4"],
        console_output=True,
    )
    print("MAESTRO_JOB_SUBMITTED", json.dumps({
        "model_type": settings.get("model_type"),
        "resolution": settings.get("resolution"),
        "steps": settings.get("num_inference_steps"),
        "frames": settings.get("video_length"),
        "seed": settings.get("seed"),
    }), flush=True)
    job = session.submit_task(settings)
    result = job.result()
    receipt = {
        "success": bool(result.success),
        "generated_files": list(result.generated_files),
        "errors": [
            {"message": str(error.message), "stage": str(error.stage or "")}
            for error in result.errors
        ],
    }
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print("MAESTRO_RESULT", json.dumps(receipt), flush=True)
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
