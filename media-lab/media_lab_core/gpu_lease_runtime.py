"""Shared, stdlib-only bridge from controllers and engine shims to the GPU lease."""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .durable_gpu_protocol import DurableGpuProtocol, Lease

ENV_DB = "MEDIA_LAB_GPU_LEASE_DB"
ENV_LOCK = "MEDIA_LAB_GPU_LOCK"
ENV_FENCE = "MEDIA_LAB_GPU_FENCE"
ENV_JOB = "MEDIA_LAB_GPU_JOB_ID"
ENV_ENGINE = "MEDIA_LAB_GPU_ENGINE"
ENV_TASK = "MEDIA_LAB_GPU_TASK"
HEADER_FENCE = "X-Media-Lab-GPU-Fence"
HEADER_JOB = "X-Media-Lab-GPU-Job"
HEADER_ENGINE = "X-Media-Lab-GPU-Engine"
HEADER_TASK = "X-Media-Lab-GPU-Task"


def default_db_path() -> Path:
    return Path(os.environ.get(ENV_DB, str(Path.home() / "media-lab-simple/pool/gpu-lease.sqlite3"))).expanduser()


def default_lock_path() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    linux_runtime = Path(f"/run/user/{os.getuid()}")
    runtime = configured or (str(linux_runtime) if linux_runtime.is_dir() else
                             str(Path(tempfile.gettempdir()) / f"media-lab-{os.getuid()}"))
    return Path(os.environ.get(ENV_LOCK, str(Path(runtime) / "spark-gpu.lock"))).expanduser()


def open_protocol(**overrides) -> DurableGpuProtocol:
    return DurableGpuProtocol(default_db_path(), default_lock_path(), **overrides)


def delegation_headers(lease: Lease) -> dict[str, str]:
    return {HEADER_FENCE: str(lease.fence), HEADER_JOB: lease.job_id,
            HEADER_ENGINE: lease.engine, HEADER_TASK: lease.task}


def delegation_env(lease: Lease) -> dict[str, str]:
    return {ENV_FENCE: str(lease.fence), ENV_JOB: lease.job_id,
            ENV_ENGINE: lease.engine, ENV_TASK: lease.task,
            ENV_DB: str(default_db_path()), ENV_LOCK: str(default_lock_path())}


def _value(values: Mapping[str, object], name: str) -> str:
    wanted = name.lower()
    for key, value in values.items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def authorize_values(protocol: DurableGpuProtocol, values: Mapping[str, object], *,
                     engine: str, task: str, header_mode: bool = True,
                     phases: tuple[str, ...] = ("render",)) -> bool:
    names = ((HEADER_FENCE, HEADER_JOB, HEADER_ENGINE, HEADER_TASK) if header_mode else
             (ENV_FENCE, ENV_JOB, ENV_ENGINE, ENV_TASK))
    fence, job_id, supplied_engine, supplied_task = (_value(values, name) for name in names)
    if supplied_engine != engine or supplied_task != task or not fence.isdigit() or not job_id:
        return False
    return protocol.authorize(fence=int(fence), job_id=job_id, engine=engine, task=task,
                              phases=phases)


def authorize_environment(protocol: DurableGpuProtocol, *, engine: str, task: str,
                          environ: Mapping[str, object] | None = None,
                          phases: tuple[str, ...] = ("render",)) -> bool:
    return authorize_values(protocol, environ or os.environ, engine=engine, task=task,
                            header_mode=False, phases=phases)
