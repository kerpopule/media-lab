#!/usr/bin/env python3
"""Reconcile one exact Media Lab durable GPU recovery hold.

Run only after operator approval. The one standing approval is
runner/hold_autorecover.py, and only for the harmless same-boot restart hold
(owner-exited / controller-restarted) under the limits it documents. The tool stops admission, adopts the existing
recovery fence (or proves that only one exact orphaned job flag remains), reclaims
every managed GPU companion, requires exact process and memory proof, reconciles
the protocol, removes only the matching hold, restores the legacy idle pool, and
restarts Media Lab in a finally block.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "media-lab-simple.service"
JOB_OUTPUT_FIELDS = (
    "url", "poster", "sha256", "song_url", "video_url", "video_poster",
    "final_url", "output", "outputs",
)


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def internal_residency_marker_matches(
    lease, durable_row, job, marker, job_id: str,
) -> bool:
    """Bind a jobless internal hold to one exact durable recovery lease."""
    if not (
        lease is not None and lease.state == "recovery"
        and lease.job_id == job_id and job is None
        and (lease.job_id.startswith("internal-")
             or lease.job_id.startswith("idle-restore-"))
        and marker is not None
    ):
        return False
    if marker.get("job_id") == job_id:
        return True
    if marker.get("job_id") is not None:
        return False
    lease_reason = str((durable_row or {}).get("reason") or "unknown")
    return str(marker.get("reason") or "") in {
        lease_reason,
        f"durable-lease-{lease.state}:{lease_reason}",
    }


def configured_home() -> Path:
    """Resolve the same per-host data root app.py will use, without importing it."""
    from media_lab_core import local_config

    return local_config.home()


def persisted_job_snapshot(jobs_file: Path, job_id: str):
    """Read the pre-import row; importing app converts running jobs to queued."""
    if not jobs_file.exists():
        return None
    state = json.loads(jobs_file.read_text())
    jobs = state.get("jobs") if isinstance(state, dict) else None
    return jobs.get(job_id) if isinstance(jobs, dict) else None


def interrupted_running_boot_recovery_matches(
    lease, durable_row, persisted_job, live_job, marker, job_id: str,
) -> bool:
    """Recognize only the exact output-free load interrupted by a real reboot."""
    if not (
        lease is not None and lease.state == "recovery" and lease.phase == "load"
        and lease.job_id == job_id
        and persisted_job is not None and persisted_job.get("status") == "running"
        and persisted_job.get("id") == job_id
        and live_job is not None and live_job.get("status") == "queued"
        and marker is not None and marker.get("job_id") is None
        and marker.get("reason") == "durable-lease-recovery:boot-changed"
        and (durable_row or {}).get("reason") == "boot-changed"
    ):
        return False
    if any(persisted_job.get(field) for field in JOB_OUTPUT_FIELDS):
        return False
    exact_fields = ("fence", "owner", "pid", "boot_id", "job_id", "engine", "task", "phase", "state")
    return all((durable_row or {}).get(field) == getattr(lease, field) for field in exact_fields)


def interrupted_boot_transition_matches(
    lease, durable_row, persisted_job, live_job, marker, job_id: str,
) -> bool:
    """Resume only this tool's durable terminal-disposition checkpoint."""
    if not (
        persisted_job is not None and live_job is not None
        and persisted_job.get("id") == job_id
        and persisted_job.get("status") == "queued"
        and persisted_job.get("recovery_required") is True
        and persisted_job.get("recovery_reason") == "interrupted-running-boot-recovery"
        and persisted_job.get("recovery_disposition") == "terminal-error-no-auto-retry"
        and live_job.get("recovery_required") is True
        and live_job.get("recovery_disposition") == "terminal-error-no-auto-retry"
    ):
        return False
    if any(persisted_job.get(field) for field in JOB_OUTPUT_FIELDS):
        return False
    if lease is None:
        return marker is None
    marker_matches = marker is None or (
        marker.get("job_id") is None
        and marker.get("reason") == "durable-lease-recovery:boot-changed"
    )
    if not (
        marker_matches and lease.state == "recovery" and lease.phase == "load"
        and lease.job_id == job_id and (durable_row or {}).get("reason") == "boot-changed"
    ):
        return False
    exact_fields = ("fence", "owner", "pid", "boot_id", "job_id", "engine", "task", "phase", "state")
    return all((durable_row or {}).get(field) == getattr(lease, field) for field in exact_fields)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ["MEDIA_LAB_DISABLE_BACKGROUND_WORKERS"] = "1"

    systemctl("stop", SERVICE)
    try:
        jobs_file = configured_home() / "jobs.json"
        persisted_job = persisted_job_snapshot(jobs_file, args.job_id)
        # app.py logs door codes at import for the interactive service. Recovery
        # receipts must never expose those secrets to operator logs.
        with contextlib.redirect_stdout(io.StringIO()):
            import app
        if Path(app.JOBS_FILE).resolve() != jobs_file.resolve():
            raise RuntimeError("configured jobs path changed while recovery admission was stopped")

        protocol = app.gpu_protocol()
        lease = app._gpu_active_lease or protocol.recover_startup()
        app._gpu_active_lease = lease
        durable_row = protocol.snapshot().get("lease") or {}
        orphan_recovery_job = lease is None
        marker = None
        if app.GPU_RECOVERY_HOLD.exists():
            marker = json.loads(app.GPU_RECOVERY_HOLD.read_text())

        job = app.jobs.get(args.job_id)
        internal_residency_recovery = internal_residency_marker_matches(
            lease, durable_row, job, marker, args.job_id
        )
        interrupted_running_boot_recovery = interrupted_running_boot_recovery_matches(
            lease, durable_row, persisted_job, job, marker, args.job_id
        )
        interrupted_boot_transition = interrupted_boot_transition_matches(
            lease, durable_row, persisted_job, job, marker, args.job_id
        )
        interrupted_boot_recovery = (
            interrupted_running_boot_recovery or interrupted_boot_transition
        )
        terminal_parked_recovery = bool(
            lease is not None and lease.state == "recovery" and lease.phase == "parked"
            and lease.job_id == args.job_id and job
            and job.get("status") in ("done", "error", "cancelled")
            and marker is not None and marker.get("job_id") is None
            and str(marker.get("reason") or "").startswith("durable-lease-recovery:")
        )
        if ((not job and not internal_residency_recovery) or
                (job and job.get("recovery_required") is not True
                 and not terminal_parked_recovery
                 and not interrupted_boot_recovery)):
            raise RuntimeError(
                "matching target is neither recovery_required, an exact internal residency lease, "
                "an exact terminal parked restart, nor an exact interrupted boot recovery"
            )

        if lease is None:
            # A previous exact reconciliation can leave only the persisted job
            # flag behind if the controller crashes between protocol/marker
            # cleanup and jobs.json persistence. A remaining marker is not that
            # case: without its matching durable fence it is ambiguous and must
            # stay blocked for an operator.
            if marker is not None:
                raise RuntimeError("recovery marker exists without a durable lease")
        else:
            if lease.state != "recovery" or lease.job_id != args.job_id:
                raise RuntimeError(
                    f"exact recovery lease not found for {args.job_id}: "
                    f"{(lease.job_id, lease.state)}"
                )
            if ((marker is None and not interrupted_boot_transition) or
                    (marker is not None and marker.get("job_id") != args.job_id
                     and not terminal_parked_recovery
                     and not interrupted_boot_recovery
                     and not internal_residency_recovery)):
                raise RuntimeError(
                    f"recovery marker belongs to {None if marker is None else marker.get('job_id')!r}"
                )
        if (interrupted_boot_recovery and lease is not None
                and getattr(lease, "_fd", None) is None):
            raise RuntimeError("durable recovery exclusion lock is still owned by an active process")

        target_engine = str(
            getattr(lease, "engine", "") or (job or {}).get("engine") or ""
        )
        if target_engine == "maestro":
            reaped = app.reap_orphan_maestro_runners()
            if reaped.get("status") not in ("clean", "reaped"):
                raise RuntimeError(
                    f"Maestro runner absence is unproven: {reaped.get('detail') or reaped}"
                )
        proof = app._gpu_reclaim_all(job)
        if proof.get("processes_gone") is not True:
            raise RuntimeError(f"managed GPU processes survived: {proof.get('survivors')}")
        if proof.get("memory_recovered") is not True:
            raise RuntimeError(f"memory did not recover: {proof.get('available_gib')} GiB")

        if interrupted_boot_recovery:
            assert job is not None
            if interrupted_running_boot_recovery:
                job.update(
                    recovery_required=True,
                    recovery_reason="interrupted-running-boot-recovery",
                    recovery_disposition="terminal-error-no-auto-retry",
                )
                app.save_state()
            # Once the durable job checkpoint exists, it is the admission hold.
            # Remove the marker before deleting the lease so every crash point is
            # resumable as either an exact fenced transition or an orphan flag.
            if marker is not None:
                app.GPU_RECOVERY_HOLD.unlink()
                marker = None
        if lease is not None:
            app.gpu_protocol().reconcile(lease, proof=proof)
        app._gpu_active_lease = None
        if job is not None:
            if interrupted_boot_recovery:
                # A reboot makes the engine outcome unknowable. Do not silently
                # reissue the request: park this exact attempt as a terminal error
                # so a user-initiated Retry creates the next take without a
                # duplicate output from the interrupted attempt.
                job.update(
                    status="error", stage="error", retryable=False,
                    message="This take was interrupted by a host reboot and was not retried. Use Retry to start a new take.",
                )
            for key in ("recovery_required", "recovery_reason"):
                job.pop(key, None)
            app.save_state()
            if interrupted_boot_recovery:
                # Keep the terminal row retained by save_state's queued-job rule,
                # then remove it only in memory. The restarted app reloads the
                # terminal status and filters the stale queue entry itself.
                for pending in (getattr(app, "queue", []), getattr(app, "online_queue", [])):
                    while args.job_id in pending:
                        pending.remove(args.job_id)
        if marker is not None:
            app.GPU_RECOVERY_HOLD.unlink()
        if app.pool_cmd("acquire") != "OK":
            raise RuntimeError("legacy idle pool did not reacquire canonical GPU lock")

        print(json.dumps({
            "reconciled_job": args.job_id,
            "orphan_recovery_job": orphan_recovery_job,
            "internal_residency_recovery": internal_residency_recovery,
            "terminal_parked_recovery": terminal_parked_recovery,
            "interrupted_running_boot_recovery": interrupted_running_boot_recovery,
            "interrupted_boot_transition": interrupted_boot_transition,
            "job_disposition": "terminal-error-no-auto-retry" if interrupted_boot_recovery else None,
            "proof": proof,
            "lease": app.gpu_protocol().snapshot().get("lease"),
            "hold_exists": app.GPU_RECOVERY_HOLD.exists(),
            "pool_restored": True,
        }, sort_keys=True))
        return 0
    finally:
        started = systemctl("start", SERVICE, check=False)
        if started.returncode != 0:
            print(started.stderr, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
