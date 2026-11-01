"""Fail-closed kernel/RDMA qualification gate for multi-Spark work.

The NVIDIA 7.0.0-1019 advisory concerns multi-node/RoCE memory registration.
It must not be used to explain or waive separate single-node GPU-memory faults.
"""
from __future__ import annotations

import re
from typing import Sequence


_AFFECTED_1019 = re.compile(r"^7\.0\.0-1019(?:-|$)")
_MODES = {"single-node-local", "multi-node-roce"}


def evaluate_kernel_rdma(*, kernel: str, rdma_devices: Sequence[str], mode: str) -> dict:
    if mode not in _MODES:
        raise ValueError(f"unsupported qualification mode: {mode}")
    affected = bool(_AFFECTED_1019.match(kernel.strip()))
    devices = [str(item).strip() for item in rdma_devices if str(item).strip()]
    if mode == "single-node-local":
        return {
            "allowed": True,
            "advisory_relevant": False,
            "kernel": kernel,
            "rdma_devices": devices,
            "reason": None,
            "note": "RoCE advisory is separate from single-node H3 memory/admission RCA.",
        }
    if affected:
        return {
            "allowed": False,
            "advisory_relevant": True,
            "kernel": kernel,
            "rdma_devices": devices,
            "reason": "kernel-7.0.0-1019-rdma-enomem-advisory",
            "note": "Hold multi-node/RoCE qualification until a reviewed non-1019 boot is proven.",
        }
    if not devices:
        return {
            "allowed": False,
            "advisory_relevant": True,
            "kernel": kernel,
            "rdma_devices": [],
            "reason": "rdma-device-unavailable",
            "note": "No RDMA device is available for a two-node RoCE qualification.",
        }
    return {
        "allowed": True,
        "advisory_relevant": True,
        "kernel": kernel,
        "rdma_devices": devices,
        "reason": None,
        "note": "Kernel and RDMA presence pass the compatibility pre-gate; NCCL/RoCE still needs live qualification.",
    }
