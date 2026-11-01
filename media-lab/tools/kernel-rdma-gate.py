#!/usr/bin/env python3
"""Machine-readable kernel/RDMA qualification preflight."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_lab_core.kernel_compat import evaluate_kernel_rdma


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("single-node-local", "multi-node-roce"), required=True)
    parser.add_argument("--kernel")
    parser.add_argument("--rdma-present", choices=("auto", "yes", "no"), default="auto")
    args = parser.parse_args()
    kernel = args.kernel or subprocess.run(
        ["uname", "-r"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if args.rdma_present == "auto":
        try:
            check = subprocess.run(["rdma", "link", "show"], capture_output=True, text=True)
            rdma_present = check.returncode == 0 and bool(check.stdout.strip())
        except OSError:
            rdma_present = False
    else:
        rdma_present = args.rdma_present == "yes"
    rdma_devices = ["configured"] if rdma_present else []
    try:
        result = evaluate_kernel_rdma(kernel=kernel, rdma_devices=rdma_devices, mode=args.mode)
    except (ValueError, OSError) as exc:
        print(json.dumps({"allowed": False, "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["allowed"] else 3


if __name__ == "__main__":
    sys.exit(main())
