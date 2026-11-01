"""One aggregated, side-effect-free health answer for the studio.

``GET /api/health`` (behind the family door: the local token or a signed-in pass)
and ``media-lab status --json`` both return this. It exists because the truth
used to be spread over ``/api/residency``, the pool files, two watchdogs and the
lease database, and nothing said "held for N hours".

Levels:
    ok      nothing to do
    warn    worth a line in the daily brief; the studio still renders
    action  a person has to do something (the reasons say what)

Reading only: files, the lease database in read-only mode, two local HTTP
probes with short timeouts, ``systemctl show``. Nothing here starts, stops,
clears or writes anything.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

HOLD_ACTION_AFTER_S = 20 * 60
STUCK_AFTER_S = 15 * 60
RUNNING_OVER_ETA = 3.0
HANDOFF_STALE_S = 10 * 60
DISK_WARN_PCT = 15.0
DISK_ACTION_PCT = 8.0
SNAPSHOT_STALE_S = 3 * 3600
HARMLESS_HOLD = ("durable-lease-recovery:owner-exited", "durable-lease-recovery:controller-restarted")
# A guard pressure trip during an H3 cold load clears itself (hold_autorecover):
# 15 min of settling plus a ~6 min reload. Only after this is it a person's job.
GUARD_TRIP_REASONS = ("memory-psi", "low-memavailable", "swap-growth")
TRIP_ACTION_AFTER_S = 40 * 60


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _stop_reason(sol_root) -> str | None:
    doc = read_json(Path(sol_root) / "safety-stop.json") if sol_root else None
    return doc.get("reason") if isinstance(doc, dict) else None


def lease_row(db: Path) -> dict | None:
    if not Path(db).exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM gpu_lease WHERE singleton=1").fetchone()
        return dict(row) if row is not None else None
    finally:
        con.close()


def newest_mtime(directory: Path, pattern: str = "*") -> float | None:
    try:
        return max((p.stat().st_mtime for p in Path(directory).glob(pattern)), default=None)
    except OSError:
        return None


def disk_free_pct(path: Path) -> float | None:
    try:
        usage = shutil.disk_usage(path)
        return round(100.0 * usage.free / usage.total, 1)
    except OSError:
        return None


def unit_restarts(unit: str) -> int | None:
    try:
        out = subprocess.run(["systemctl", "--user", "show", unit, "-p", "NRestarts", "--value"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        return int(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def boot_cleared(sol_root: Path, boot_id: str) -> bool:
    permit = read_json(Path(sol_root) / "boot-clearance.json")
    return bool(isinstance(permit, dict) and permit.get("approved") is True
                and boot_id and permit.get("boot_id") == boot_id)


def text_models(url: str, timeout: float = 2.0) -> list[str] | None:
    """Model ids the text bridge lists, or None when it does not answer."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url.rstrip("/") + "/v1/models", timeout=timeout) as r:
            data = json.load(r)
        return [str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict)]
    except Exception:
        return None


def _age(now: float, ts: Any) -> float | None:
    try:
        return max(0.0, now - float(ts))
    except (TypeError, ValueError):
        return None


def assess(h: dict, now: float | None = None) -> tuple[str, list[str]]:
    """(level, reasons) for one health document. Pure."""
    now = time.time() if now is None else now
    action: list[str] = []
    warn: list[str] = []
    gpu, queue, sol = h.get("gpu") or {}, h.get("queue") or {}, h.get("sol") or {}
    hold = gpu.get("hold") or {}
    auto = gpu.get("autorecover") or {}
    guard_trip = False
    if hold.get("exists"):
        age = hold.get("age_s") or 0
        reason = hold.get("reason") or "unknown"
        guard_trip = (bool(gpu.get("safety_stop")) and str(reason).startswith("operation-uncertain:")
                      and gpu.get("safety_stop_reason") in GUARD_TRIP_REASONS)
        text = f"GPU recovery hold for {age / 60:.0f} min ({reason})"
        if guard_trip:
            text += f" after a guard {gpu.get('safety_stop_reason')} trip"
        if auto.get("gaveup"):
            action.append(text + "; auto-recover gave up")
        elif guard_trip and age < TRIP_ACTION_AFTER_S:
            warn.append(text + "; auto-recover may clear it once memory has settled")
        elif age >= HOLD_ACTION_AFTER_S or reason not in HARMLESS_HOLD:
            action.append(text)
        else:
            warn.append(text + "; auto-recover may clear it")
    elif auto.get("gaveup"):
        action.append("auto-recover gave up earlier (remove pool/autorecover-gaveup.json once looked at)")
    if gpu.get("safety_stop") and not guard_trip:
        action.append("H3 safety stop is set")
    if gpu.get("latch") and not guard_trip:
        action.append("memwatch latch is set (H3 was stopped for low memory)")
    if queue.get("stuck"):
        action.append(f"queue stuck: {queue.get('queued')} queued, none running for "
                      f"{(queue.get('oldest_queued_age_s') or 0) / 60:.0f} min")
    if queue.get("running_over_eta"):
        action.append("a running job is past 3x its estimate")
    if (h.get("watchdog") or {}).get("restart_capped"):
        action.append("queue watchdog hit its restart cap")
    if sol.get("configured") and sol.get("boot_cleared") is False:
        action.append("host rebooted: H3 needs boot clearance")
    disk = h.get("disk_free_pct")
    if disk is not None and disk < DISK_ACTION_PCT:
        action.append(f"disk {disk}% free")
    elif disk is not None and disk < DISK_WARN_PCT:
        warn.append(f"disk {disk}% free")
    text = h.get("text") or {}
    if text.get("listed") is False:
        warn.append("text bridge does not list media-lab-text"
                    if text.get("answering") else "text bridge is not answering")
    if sol.get("configured") and not sol.get("loaded") and not hold.get("exists") \
            and not queue.get("running"):
        warn.append("H3 is cold")
    guard = gpu.get("guard") or {}
    if sol.get("configured") and guard.get("state") not in ("ready", "monitoring"):
        warn.append(f"control-plane guard is {guard.get('state') or 'absent'}")
    handoff_age = gpu.get("handoff_age_s")
    if handoff_age is not None and handoff_age > HANDOFF_STALE_S:
        warn.append("a planned-restart handoff record was left behind")
    snap = h.get("statesnap_age_s")
    if snap is not None and snap > SNAPSHOT_STALE_S:
        warn.append(f"hourly state snapshot is {snap / 3600:.0f} h old")
    level = "action" if action else "warn" if warn else "ok"
    return level, action + warn


def collect(*, root: Path, now: float | None = None, boot_id: str = "",
            jobs: dict | None = None, queue_ids: list | None = None,
            lease_db: Path | None = None, sol_root: Path | None = None,
            sol_configured: bool = False, runtime_dir: Path | None = None,
            sol_state: Callable[[], dict | None] | None = None,
            text_url: str = "http://127.0.0.1:8004", text_model: str = "media-lab-text",
            eta: Callable[[dict], float] | None = None,
            mem_available_gib: Callable[[], float | None] | None = None,
            restarts: Callable[[], int | None] | None = None) -> dict:
    """Assemble the health document from the studio's own files and probes."""
    now = time.time() if now is None else now
    root = Path(root)
    pool = root / "pool"
    jobs = jobs or {}
    running = [j for j in jobs.values() if isinstance(j, dict) and j.get("status") == "running"]
    queued = [jobs[i] for i in (queue_ids or []) if i in jobs
              and jobs[i].get("status") == "queued"]
    oldest = max((_age(now, j.get("added") or j.get("ts")) or 0 for j in queued), default=None)
    hold_doc = read_json(pool / "gpu-recovery-hold.json")
    hold_exists = (pool / "gpu-recovery-hold.json").exists()
    hold = {"exists": hold_exists,
            "reason": (hold_doc or {}).get("reason") if isinstance(hold_doc, dict) else None,
            "job_id": (hold_doc or {}).get("job_id") if isinstance(hold_doc, dict) else None,
            "age_s": _age(now, (hold_doc or {}).get("created")) if isinstance(hold_doc, dict) else None}
    running_age = max((_age(now, j.get("started")) or 0 for j in running), default=None)
    over_eta = False
    if eta is not None:
        for j in running:
            est_min, age = eta(j), _age(now, j.get("started"))
            if est_min and age is not None and age > RUNNING_OVER_ETA * float(est_min) * 60:
                over_eta = True
    watchdog = read_json(root / "watchdog-state.json") or {}
    stuck = bool(queued and not running and not hold_exists
                 and (oldest or 0) >= STUCK_AFTER_S) or bool(watchdog.get("stuck"))
    try:
        lease = lease_row(lease_db or pool / "gpu-lease.sqlite3")
        lease_error = None
    except sqlite3.Error as exc:
        lease, lease_error = None, str(exc)
    runtime = Path(runtime_dir) if runtime_dir else None
    guard_doc = read_json(runtime / "solh3-control-plane-guard.json") if runtime else None
    guard_fresh = bool(isinstance(guard_doc, dict) and guard_doc.get("boot_id") == boot_id
                       and (_age(now, guard_doc.get("written_at")) or 1e9) <= 30)
    handoff = pool / "gpu-handoff.json"
    auto_state = read_json(pool / "autorecover-state.json") or {}
    attempts = auto_state.get("attempts") or []
    sol_doc = (sol_state() if sol_state else None) or {}
    listed = text_models(text_url)
    doc: dict[str, Any] = {
        "ts": now,
        "queue": {"queued": len(queued), "running": len(running),
                  "oldest_queued_age_s": oldest, "running_age_s": running_age,
                  "running_over_eta": over_eta, "stuck": stuck},
        "gpu": {
            "lease": None if lease is None else {
                k: lease.get(k) for k in ("state", "phase", "engine", "task", "job_id", "reason",
                                          "fence")} | {"age_s": _age(now, lease.get("updated"))},
            "lease_error": lease_error,
            "hold": hold,
            "handoff_age_s": _age(now, handoff.stat().st_mtime) if handoff.exists() else None,
            "guard": {"state": guard_doc.get("state") if isinstance(guard_doc, dict) else None,
                      "fresh": guard_fresh},
            "latch": bool(runtime and (runtime / "flashnext-memwatch.latch").exists()),
            "safety_stop": bool(sol_root and (Path(sol_root) / "safety-stop.json").exists()),
            "safety_stop_reason": _stop_reason(sol_root),
            "autorecover": {"gaveup": (pool / "autorecover-gaveup.json").exists(),
                            "last": attempts[-1] if attempts else None},
        },
        "sol": {"configured": bool(sol_configured),
                "loaded": bool(sol_doc.get("loaded")) if sol_doc else False,
                "task": sol_doc.get("task"), "variant": sol_doc.get("variant"),
                "boot_cleared": boot_cleared(sol_root, boot_id) if (sol_configured and sol_root) else None},
        "text": {"answering": listed is not None,
                 "listed": None if listed is None else text_model in listed},
        "memory": {"available_gib": mem_available_gib() if mem_available_gib else None},
        "disk_free_pct": disk_free_pct(root),
        "statesnap_age_s": _age(now, newest_mtime(root / "backups", "*.json.*")),
        "watchdog": {"restart_capped": bool(watchdog.get("restart_capped")),
                     "restarts_last_hour": len([t for t in watchdog.get("restarts", [])
                                                if now - float(t) < 3600])},
        "deployed": {k: (read_json(root / "deployed-source.json") or {}).get(k)
                     for k in ("tag", "commit")},
        "service_restarts": restarts() if restarts else None,
    }
    if doc["text"]["answering"] is False:
        doc["text"]["listed"] = False
    level, reasons = assess(doc, now)
    doc["level"], doc["reasons"] = level, reasons
    return doc
