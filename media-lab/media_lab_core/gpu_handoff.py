"""Planned-restart handoff of an idle, parked GPU residency.

Why this exists
---------------
A warm engine (Sol-H3 above all) runs in its own unit and survives a restart of
the studio. The durable lease that owns it does not: the next controller finds
the row owned by a dead PID, quarantines it as ``owner-exited`` and holds the
whole GPU until an operator reconciles it (a ~5 minute cold reload of H3). The
existing restart adoption path only accepts a *queued job* as proof, which an
idle parked residency never has, so every deploy, watchdog repair or config
restart while H3 was warm ended in a recovery hold.

The handoff
-----------
On a *graceful* stop (the ASGI lifespan shutdown) the controller writes one
small record, ``pool/gpu-handoff.json``, but only when all of this is true at
that moment:

* no fenced GPU operation is running in this process (the caller holds the
  controller's protocol mutex and keeps it until exit, so none can start);
* the durable lease is ``active`` + ``parked`` and owned by this process;
* the lease names an exact runtime process, and that process is alive with the
  same boot-scoped identity;
* the engine answers healthy and not busy, with the exact resident config;
* no job is running and no recovery hold exists.

On the next start, :func:`validate` accepts the record only if every field still
matches the quarantined row exactly (fence, owner, boot, job, engine, task,
runtime pid and identity), the owner really exited, the record is fresh, the
engine is still healthy/idle with the same resident config, and nothing is
running or held. Anything else is refused and the controller holds exactly as
before. A crash, a kill or a hang never reaches the shutdown hook, so it leaves
no record and still holds. The record is consumed (deleted) on every start,
used or not, so a stale record can never be replayed.

This module is stdlib-only and side-effect free apart from the record file, so
the whole decision is testable without the studio.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

VERSION = 1
MAX_AGE_S = 600.0
FLAG = "MEDIA_LAB_GRACEFUL_HANDOFF"


class HandoffRefused(ValueError):
    """The record does not prove an exact, idle, same-boot residency."""


def _identity_tuple(identity: Any) -> tuple[int, str] | None:
    if not identity:
        return None
    try:
        pid, ident = identity
        return int(pid), str(ident)
    except (TypeError, ValueError):
        return None


def plan(row: Mapping[str, Any] | None, *, owner: str, boot_id: str,
         live_identity: Any, warm: Mapping[str, Any], resident_config: Any,
         running_jobs: int, hold_exists: bool, now: float | None = None) -> dict:
    """Return the handoff record for a graceful stop, or raise HandoffRefused.

    ``warm`` is the controller's exact warm proof ({"healthy": bool, "busy": bool}).
    ``resident_config`` is what the engine reports it has loaded (None when the
    engine has no such notion); it is replayed verbatim on the next start.
    """
    if hold_exists:
        raise HandoffRefused("a recovery hold exists")
    if running_jobs:
        raise HandoffRefused(f"{running_jobs} job(s) running")
    if row is None:
        raise HandoffRefused("no durable lease")
    if row.get("state") != "active" or row.get("phase") != "parked":
        raise HandoffRefused(f"lease is {row.get('state')}/{row.get('phase')}, not active/parked")
    if row.get("owner") != owner:
        raise HandoffRefused("lease is owned by another controller")
    if row.get("boot_id") != boot_id:
        raise HandoffRefused("lease is from another boot")
    runtime = _identity_tuple(live_identity)
    if runtime is None:
        raise HandoffRefused("engine process is not running")
    if (row.get("runtime_pid") is None or int(row["runtime_pid"]) != runtime[0]
            or not row.get("runtime_identity") or row["runtime_identity"] != runtime[1]):
        raise HandoffRefused("lease does not name the live engine process")
    if warm.get("healthy") is not True or warm.get("busy") is not False:
        raise HandoffRefused("engine is not proven healthy and idle")
    return {
        "version": VERSION,
        "created": float(time.time() if now is None else now),
        "boot_id": boot_id,
        "fence": int(row["fence"]),
        "owner": str(row["owner"]),
        "owner_pid": int(row["pid"]),
        "job_id": str(row["job_id"]),
        "engine": str(row["engine"]),
        "task": str(row["task"]),
        "runtime_pid": runtime[0],
        "runtime_identity": runtime[1],
        "resident_config": resident_config,
    }


def write(path: Path, record: Mapping[str, Any]) -> None:
    """Atomically replace ``path`` with ``record`` (tmp + fsync + rename + dir fsync)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(dict(record), fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def consume(path: Path) -> dict | None:
    """Read and delete the record. A malformed record is deleted and ignored."""
    path = Path(path)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def validate(record: Mapping[str, Any] | None, row: Mapping[str, Any] | None, *,
             boot_id: str, live_identity: Any, warm: Mapping[str, Any],
             resident_config: Any, running_jobs: int, hold_exists: bool,
             now: float | None = None, max_age_s: float = MAX_AGE_S) -> dict:
    """Return the ``adopt_recovered`` proof for an exact handoff, or raise.

    ``row`` is the durable lease row *after* ``recover_startup`` quarantined it.
    """
    if not record:
        raise HandoffRefused("no handoff record (crash, kill or first start)")
    if record.get("version") != VERSION:
        raise HandoffRefused("unknown handoff record version")
    if hold_exists:
        raise HandoffRefused("a recovery hold exists")
    if running_jobs:
        raise HandoffRefused(f"{running_jobs} job(s) running")
    current = float(time.time() if now is None else now)
    try:
        age = current - float(record.get("created"))
    except (TypeError, ValueError):
        raise HandoffRefused("handoff record has no time") from None
    if age < 0 or age > max_age_s:
        raise HandoffRefused(f"handoff record is {age:.0f}s old")
    if row is None:
        raise HandoffRefused("no durable lease to adopt")
    if row.get("state") != "recovery" or row.get("reason") != "owner-exited":
        raise HandoffRefused(
            f"lease is {row.get('state')}:{row.get('reason')}, not recovery:owner-exited")
    if row.get("phase") != "parked":
        raise HandoffRefused(f"lease phase is {row.get('phase')}, not parked")
    if record.get("boot_id") != boot_id or row.get("boot_id") != boot_id:
        raise HandoffRefused("boot changed since the handoff")
    exact = {
        "fence": "fence", "owner": "owner", "owner_pid": "pid", "job_id": "job_id",
        "engine": "engine", "task": "task", "runtime_pid": "runtime_pid",
        "runtime_identity": "runtime_identity",
    }
    for key, column in exact.items():
        if record.get(key) is None or record.get(key) != row.get(column):
            raise HandoffRefused(f"handoff {key} does not match the durable lease")
    runtime = _identity_tuple(live_identity)
    if runtime is None or runtime != (int(record["runtime_pid"]), str(record["runtime_identity"])):
        raise HandoffRefused("engine process changed or exited since the handoff")
    if warm.get("healthy") is not True or warm.get("busy") is not False:
        raise HandoffRefused("engine is not proven healthy and idle")
    if resident_config != record.get("resident_config"):
        raise HandoffRefused("engine resident config changed since the handoff")
    return {
        "boot_id": record["boot_id"], "job_id": record["job_id"],
        "engine": record["engine"], "task": record["task"],
        "pid": runtime[0], "process_identity": runtime[1],
        "healthy": True, "busy": False,
    }


def adopt(protocol, recovered, *, owner: str, proof: Mapping[str, Any],
          park_proof: Mapping[str, Any]):
    """Adopt the quarantined lease and park it again, exactly as it was.

    ``adopt_recovered`` re-fences the lease into ``render``; a jobless residency
    must go straight back to ``parked`` so the next job retargets it normally.
    If the re-park fails the lease is marked recovery again (fail closed).
    """
    lease = protocol.adopt_recovered(recovered, owner=owner, proof=proof)
    try:
        protocol.park(lease, proof=park_proof)
    except Exception:
        try:
            protocol.mark_recovery(lease, "handoff-repark-failed")
        except Exception:
            pass
        raise
    return lease
