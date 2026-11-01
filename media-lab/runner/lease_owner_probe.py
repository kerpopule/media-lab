#!/usr/bin/env python3
"""Is the canonical GPU lock held by our own live controller lease?

Exit 0  -> yes. The canonical flock is held by the live ``media-lab-simple``
           controller process and the controller has not declared an unresolved
           GPU outcome.
Exit 1  -> anything else: a free lock, a foreign holder, a dead or stale
           holder, an unresolved-outcome (recovery) hold, or evidence this probe
           could not read. Exit 1 is always the fail-closed answer.

WHY THIS EXISTS (2026-09-21). ``runner/pool_lock.sh`` is the legacy residency
handshake: "is the GPU claimed by us, and may I boot/render on it?". The
controller now owns ``/run/user/1000/spark-gpu.lock`` directly for the duration
of every fenced local GPU operation (``media_lab_core/durable_gpu_protocol.py``,
``docs/gpu-queue-rollout.md``), which means the legacy question and the legacy
holder no longer agree:

* ``pool_lock.sh acquire`` found the lock held, matched no managing unit and no
  stale ``flock ... sleep infinity`` holder, and correctly answered ``BUSY``;
* the "external holder" it reported was the controller itself;
* every image job died with ``HTTP 503 {"error": "gpu reserved elsewhere",
  "detail": "pool_lock reports an external holder"}`` while the box sat idle
  (observed live on the media Spark), and a cold image boot died the same way inside
  ``_ensure_engine_under_lease``.

This probe is the missing adapter, not a new lock: it names the holder the
legacy shim is already allowed to recognize as its own. It is strictly
read-only — it never takes, releases, starts, stops or kills anything — and it
says nothing about memory, capacity or admission, which stay with the durable
protocol and the controller.

Ownership proof is layered so a coincidence cannot open admission:

1. the canonical lock file exists and is not free;
2. exactly one process holds an exclusive POSIX flock on it (/proc/locks);
3. that process is alive and is in the ``media-lab-simple.service`` cgroup;
4. when its command line is readable, it looks like the controller;
5. ``pool/gpu-recovery-hold.json`` does not exist (skipped with
   ``--ignore-hold``/``consult_hold=False``, which answers only "does our own
   live controller hold this lock?" — the question `pool_lock.sh release` asks).

(5) is deliberate: while the controller has declared an unresolved outcome,
residency must stay refused, exactly as it is today.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

CONTROLLER_UNIT = "media-lab-simple.service"
CONTROLLER_CMD_MARKERS = ("uvicorn", "app:app", "media-lab-simple")
DEFAULT_HOME_ROOT = Path(os.environ.get("HOME") or "~").expanduser() / "media-lab-simple"


def default_lock_path() -> Path:
    """The canonical flock, resolved the same way gpu_lease_runtime.py does."""
    configured = os.environ.get("MEDIA_LAB_GPU_LOCK")
    if configured:
        return Path(configured).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    uid = os.getuid()
    if runtime:
        return Path(runtime) / "spark-gpu.lock"
    linux_runtime = Path(f"/run/user/{uid}")
    if linux_runtime.is_dir():
        return linux_runtime / "spark-gpu.lock"
    return Path("/run/user") / str(uid) / "spark-gpu.lock"


def default_root() -> Path:
    return Path(os.environ.get("MEDIA_LAB_ROOT") or DEFAULT_HOME_ROOT)


def default_marker_path(lock_path: Path | None = None) -> Path:
    """The controller's unresolved-outcome marker beside the deployed tree."""
    db = os.environ.get("MEDIA_LAB_GPU_LEASE_DB")
    if db:
        return Path(db).expanduser().parent / "gpu-recovery-hold.json"
    return default_root() / "pool" / "gpu-recovery-hold.json"


# --------------------------------------------------------------------------- probes
def lock_is_free(lock_path: Path) -> bool:
    """True when nothing holds the canonical flock (probe only; never keeps it)."""
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o664)
    except OSError:
        return False
    try:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _lock_device_inode(lock_path: Path) -> tuple[int, int, int] | None:
    try:
        st = os.stat(lock_path)
    except OSError:
        return None
    return os.major(st.st_dev), os.minor(st.st_dev), st.st_ino


def lock_holder_pids(lock_path: Path, locks_text: str | None = None) -> list[int]:
    """PIDs holding an exclusive POSIX flock on ``lock_path``.

    Reads /proc/locks, whose rows look like::

        1: FLOCK  ADVISORY  WRITE 521659 00:2c:460472 0 EOF

    Only FLOCK/WRITE (exclusive) rows count: a shared or nonexistent lock is
    not ownership. An unreadable table yields no holders, i.e. "not ours".
    """
    ids = _lock_device_inode(lock_path)
    if ids is None:
        return []
    major, minor, inode = ids
    wanted = f"{major:02x}:{minor:02x}:{inode}"
    if locks_text is None:
        try:
            locks_text = Path("/proc/locks").read_text()
        except OSError:
            return []
    holders: list[int] = []
    for line in locks_text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[0].startswith("->"):
            continue  # "->" marks a *waiting* request, not ownership
        if parts[1] != "FLOCK" or parts[3] != "WRITE":
            continue
        if not parts[4].isdigit():
            continue
        if parts[5] != wanted:
            continue
        pid = int(parts[4])
        if pid not in holders:
            holders.append(pid)
    return holders


def cgroup_of(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return ""


def cmdline_of(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(part for part in raw.decode("utf-8", "replace").split("\0") if part)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_controller_process(pid: int, *, cgroup: str | None = None,
                          cmdline: str | None = None, alive: bool | None = None) -> bool:
    """cgroup membership is the ownership proof; cmdline is a cheap cross-check."""
    is_alive = pid_alive(pid) if alive is None else alive
    if not is_alive:
        return False
    group = cgroup_of(pid) if cgroup is None else cgroup
    if CONTROLLER_UNIT not in group:
        return False
    line = cmdline_of(pid) if cmdline is None else cmdline
    return not line or any(marker in line for marker in CONTROLLER_CMD_MARKERS)


def controller_owns_lock(
    lock_path: Path | None = None,
    marker_path: Path | None = None,
    *,
    consult_hold: bool = True,
    lock_free=None,
    path_exists=None,
    locks_text: str | None = None,
    cgroup: str | None = None,
    cmdline: str | None = None,
    alive: bool | None = None,
) -> bool:
    """See the module docstring: layer 1..5, all of which must hold.

    ``consult_hold=False`` answers only "does our own live controller hold this
    lock?" — used by `pool_lock.sh release`, whose only question is whether it
    may start an idle reservation without fighting our own held lock.
    """
    lock = Path(lock_path) if lock_path is not None else default_lock_path()
    exists = path_exists or (lambda p: Path(p).exists())
    if not exists(lock):
        return False
    free = lock_is_free(lock) if lock_free is None else lock_free()
    if free:
        return False
    if consult_hold:
        marker = Path(marker_path) if marker_path is not None else default_marker_path(lock)
        if exists(marker):
            return False
    holders = lock_holder_pids(lock, locks_text)
    if len(holders) != 1:
        return False
    return is_controller_process(holders[0], cgroup=cgroup, cmdline=cmdline, alive=alive)


def explain(lock_path: Path, marker_path: Path | None, consult_hold: bool = True) -> str:
    """Human-readable diagnosis for --explain (never changes any state)."""
    lines = [f"lock:   {lock_path}",
             f"marker: {marker_path if consult_hold else '(not consulted)'}"]
    if not lock_path.exists():
        lines.append("lock file does not exist -> not owned")
        return "\n".join(lines)
    lines.append(f"free:   {lock_is_free(lock_path)}")
    if not consult_hold:
        lines.append("marker: ignored (--ignore-hold)")
    else:
        present = bool(marker_path) and marker_path.exists()
        lines.append(f"marker: {'present (fail closed)' if present else 'absent'}")
    holders = lock_holder_pids(lock_path)
    lines.append(f"holders: {holders or 'none'}")
    for pid in holders:
        lines.append(f"  pid {pid} alive={pid_alive(pid)} cmdline={cmdline_of(pid)!r}")
        lines.append(f"  pid {pid} cgroup={cgroup_of(pid).strip()!r}")
    lines.append(f"owned:  {controller_owns_lock(lock_path, marker_path, consult_hold=consult_hold)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exit 0 when the canonical GPU lock is held by our own live controller lease.")
    parser.add_argument("--lock", default=None, help="canonical flock path")
    parser.add_argument("--marker", default=None, help="unresolved-outcome marker path")
    parser.add_argument("--ignore-hold", action="store_true",
                        help="answer only 'does our own live controller hold the lock?'")
    parser.add_argument("--explain", action="store_true", help="print the read-only diagnosis")
    args = parser.parse_args(argv)
    lock = Path(args.lock) if args.lock else default_lock_path()
    consult = not args.ignore_hold
    marker = Path(args.marker) if args.marker else default_marker_path(lock)
    if args.explain:
        print(explain(lock, marker, consult))
    return 0 if controller_owns_lock(lock, marker, consult_hold=consult) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        # Fail closed, never fail the caller's shell: an unreadable probe must
        # leave the legacy BUSY behaviour below completely unchanged.
        print(f"lease_owner_probe: {exc}", file=sys.stderr)
        sys.exit(1)