#!/usr/bin/env python3
"""Clear the harmless kind of GPU recovery hold, through the sanctioned tool only.

Run by media-lab-hold-autorecover.timer every 5 minutes. Stdlib only.

A controller restart while a warm engine is parked leaves the durable lease in
``recovery`` with reason ``owner-exited`` (or ``controller-restarted``) and a
``gpu-recovery-hold.json`` marker. Nothing is wrong with the GPU: the owner
simply went away. Until 2026-09 every such hold waited for a person (6.5 to
67.5 hours each) and helpers improvised unsafe clears (deleting the lease row
or the marker by hand). This script is the one sanctioned self-heal:

* it acts ONLY on a startup hold (marker reason ``durable-lease-recovery:
  owner-exited`` / ``...:controller-restarted``, no job in the marker) whose
  durable row says the same, on the SAME boot, for a jobless residency lease
  (``idle-restore-*`` / ``internal-*``) or a parked lease whose job is finished;
* only when the hold is at least 10 minutes old (a person gets the first
  chance), no job is running (asked of the studio itself), no operator baton or
  engine-maintenance marker exists, the H3 safety stop and memwatch latch are
  absent, the control-plane guard heartbeat is current and ready/monitoring,
  the old owner process is gone, and MemAvailable is above a floor;
* its only action is ``tools/reconcile-gpu-recovery.py --job-id <lease job>``,
  the same exact-proof tool an operator runs;
* limits: one attempt per hold, at most 3 attempts per 24 h, a backoff of
  10 min / 30 min / 2 h after failures, and after 2 consecutive failures (or a
  hold it already tried) it writes ``pool/autorecover-gaveup.json`` and stops
  acting until a person removes that file. The health check alerts on it.

A second, narrower class (owner-approved, 2026-09-26): a **guard trip during
an H3 cold load**. The control-plane guard stops a cold load when memory
pressure crosses its limit; the studio then records ``operation-uncertain`` for
the load, and the guard leaves ``safety-stop.json`` plus the memwatch latch.
Every such hold since 09-17 waited hours for a person, who then checked memory
and ran the same reconcile. This script does that only when ALL of these hold:

* the safety stop is a guard pressure trip (``memory-psi`` / ``low-memavailable``
  / ``swap-growth``) of ``media-lab-sol-h3`` on THIS boot, its incident receipt
  says the H3 cgroup was fully terminated, and the latch (if any) names the
  same incident;
* the durable lease is H3, still in phase ``load`` (nothing was rendered), its
  reason equals the hold's ``operation-uncertain:*`` reason, and the load
  record in ``pool/h3-load-pressure.jsonl`` says that very load ended
  ``guard-lost`` around the trip time;
* the tripped job (if any) ended ``error``/``cancelled`` with no output, so a
  clear never re-runs it (the family presses Retry, as today);
* the trip is at least 15 min old, H3 is not running, MemAvailable is back to
  at least 90 GiB, PSI full avg60 is at most 2, and the kernel log since the
  trip shows no Xid / hung task / soft lockup / OOM kill;
* at most ONE guard-trip clear per 24 h (a second trip in a day means something
  is really wrong: it gives up and the health watch alerts).

Its action: preserve the evidence (``.backups/autorecover-trip-<time>/``), move
the safety stop and latch into that folder, wait for the guard to report
``ready``, then run the same reconcile tool. If anything fails the markers are
put back, so the box is exactly as held as before.

It never touches other ``operation-uncertain`` holds (a failure mid-render),
safety stops from a render, ``boot-changed`` holds, or anything while a job
runs. Off unless MEDIA_LAB_HOLD_AUTORECOVER=1 (config/local.env).

A third, tiny job (MEDIA_LAB_H3_CACHE_TRIM=1): with no hold at all, when the
idle H3 restore is refused for a hair of memory ("h3 decode requires 117.0 GiB
...; 116.9 GiB would be available", seen 2026-09-26 20:38-20:48 after a clean
recovery), the box is idle and H3 is not running, it writes the page cache out
of the way (sync + drop the CLEAN page cache, `vm.drop_caches=1`) so the
restore's next try fits. At most every 30 min and 12 times a day. It needs
`sudo -n` for exactly `tee /proc/sys/vm/drop_caches`.

A fourth class (owner decision, 2026-09-27: "it's supposed to come back by
itself"): **after any reboot**, planned or not, the studio clears H3 for the
new boot and the carried-over ``boot-changed`` hold itself, with the same
evidence a person saw in ``spark1-clear-h3`` (MEDIA_LAB_BOOT_AUTOCLEAR=1):

* it first preserves the previous boot's evidence (the end of its journal and
  kernel log, pstore, the hold, the lease row, any safety stop and the newest
  guard incident) in ``.backups/autorecover-boot-<time>/``;
* only when: the box has been up 2 min, no memwatch latch and no safety stop
  from THIS boot, the control-plane guard is current and ready, the GPU answers
  ``nvidia-smi``, the kernel log of this boot has no Xid / hung task / soft
  lockup / OOM kill, MemAvailable >= 90 GiB and PSI full avg60 <= 2, no job is
  running, no operator baton, and the hold (if any) is one the reconcile tool
  can clear exactly (jobless, a finished parked take, or a take the reboot
  interrupted, which the tool ends as an error and never re-runs);
* its action: write ``boot-clearance.json`` for this boot (the old one is kept)
  and, when a hold carried over, run the same reconcile tool. A safety stop
  from the PREVIOUS boot is moved into the evidence folder first.
* limits: 3 tries per boot with a 5 / 15 min backoff, at most 3 cleared boots
  per 24 h (a reboot loop gives up), and if it is still not cleared 45 min
  after boot it gives up. Giving up writes ``pool/autorecover-gaveup.json``
  with the reason, and the health watch alerts the owner with that reason; it
  never waits silently. ``spark1-clear-h3`` stays as the manual override.

    runner/hold_autorecover.py            # one pass (the timer)
    runner/hold_autorecover.py --dry-run  # print the decision, change nothing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from media_lab_core import local_config   # noqa: E402  stdlib only
from media_lab_core import local_token    # noqa: E402  stdlib only

FLAG = "MEDIA_LAB_HOLD_AUTORECOVER"
SAFE_REASONS = ("owner-exited", "controller-restarted")
JOBLESS_PREFIXES = ("idle-restore-", "internal-")
TERMINAL = ("done", "error", "cancelled")
MIN_AGE_S = 600
DAILY_CAP = 3
BACKOFF_S = (600, 1800, 7200)
GIVE_UP_AFTER = 2
MIN_AVAILABLE_GIB = 6.0
GUARD_MAX_AGE_S = 30.0
RECONCILE_TIMEOUT_S = 1200
CONTROLLER_MARKERS = ("uvicorn", "app:app")

# Guard trip during a cold load (the second, narrower class).
GUARD_TRIP_REASONS = ("memory-psi", "low-memavailable", "swap-growth")
H3_UNIT = "media-lab-sol-h3.service"
TRIP_MIN_AGE_S = 900
TRIP_DAILY_CAP = 1
TRIP_MIN_AVAILABLE_GIB = 90.0
TRIP_MAX_PSI_FULL_AVG60 = 2.0
TRIP_LOAD_SLACK_S = 60.0
GUARD_READY_WAIT_S = 20.0
# NVRM "NV_ERR_NO_MEMORY" lines are normal during a heavy load (the audit saw
# bursts with no harm), so the check starts after the trip and looks only for
# lines that mean the box itself is unwell.
KERNEL_BAD = ("xid", "hung_task", "blocked for more than", "soft lockup", "hard lockup",
              "invoked oom-killer", "out of memory: killed process", "oom-kill:",
              "fallen off the bus")
KERNEL_CHECK_AFTER_TRIP_S = 60

# Page-cache trim so an idle H3 restore that misses by a hair can fit.
TRIM_FLAG = "MEDIA_LAB_H3_CACHE_TRIM"
TRIM_MAX_SHORTFALL_GIB = 3.0
TRIM_MIN_CACHED_GIB = 1.0
TRIM_EVERY_S = 1800
TRIM_DAILY_CAP = 12
DEGRADED = re.compile(r"idle reconciliation degraded: h3 \S+ requires ([\d.]+) GiB"
                      r".*?; ([\d.]+) GiB would be available")
# After a reboot: clear H3 for the new boot and the carried-over hold (2026-09-27).
BOOT_FLAG = "MEDIA_LAB_BOOT_AUTOCLEAR"
BOOT_MIN_UPTIME_S = 120
BOOT_MIN_AVAILABLE_GIB = 90.0
BOOT_MAX_PSI_FULL_AVG60 = 2.0
BOOT_GIVE_UP_AFTER_S = 2700          # still not cleared 45 min after boot: alert
BOOT_TRIES_PER_BOOT = 3
BOOT_BACKOFF_S = (300, 900)
BOOT_DAILY_CAP = 3                   # cleared boots per 24 h; more is a reboot loop
BOOT_HOLD_REASON = "durable-lease-recovery:boot-changed"
JOB_OUTPUT_FIELDS = ("url", "poster", "sha256", "song_url", "video_url", "video_poster",
                     "final_url", "output", "outputs")


def log(msg: str) -> None:
    print(f"[hold-autorecover] {msg}", flush=True)


class Paths:
    def __init__(self, root: Path | None = None, runtime: Path | None = None,
                 sol_root: Path | None = None):
        self.root = Path(root or local_config.home())
        self.pool = self.root / "pool"
        self.hold = self.pool / "gpu-recovery-hold.json"
        self.db = Path(os.environ.get("MEDIA_LAB_GPU_LEASE_DB") or self.pool / "gpu-lease.sqlite3")
        self.state = self.pool / "autorecover-state.json"
        self.gaveup = self.pool / "autorecover-gaveup.json"
        self.log = self.pool / "autorecover.log"
        self.jobs = self.root / "jobs.json"
        self.baton = self.root / ".operator-baton"
        self.engine_maintenance = self.root / ".engine-maintenance"
        self.runtime = Path(runtime or local_config.runtime_dir())
        self.latch = self.runtime / "flashnext-memwatch.latch"
        self.guard = Path(os.environ.get("SOL_H3_GUARD_HEARTBEAT")
                          or self.runtime / "solh3-control-plane-guard.json")
        self.sol_root = Path(sol_root or os.path.expanduser(
            local_config.get("SOL_ROOT") or "~/.local/share/sol-h3-spark"))
        self.safety_stop = self.sol_root / "safety-stop.json"
        self.boot_clearance = self.sol_root / "boot-clearance.json"
        self.incidents = self.sol_root / "control-plane-incidents"
        self.load_log = self.pool / "h3-load-pressure.jsonl"
        self.evidence_root = self.root / ".backups"
        self.tool = self.root / "tools" / "reconcile-gpu-recovery.py"
        venv = self.root / ".venv" / "bin" / "python"
        self.python = str(venv) if venv.exists() else sys.executable


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _mem_available_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _psi_full_avg60() -> float | None:
    try:
        for line in Path("/proc/pressure/memory").read_text().splitlines():
            if line.startswith("full"):
                return float(dict(f.split("=", 1) for f in line.split()[1:])["avg60"])
    except (OSError, ValueError, KeyError):
        pass
    return None


def _uptime_s() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def boot_cleared(paths: "Paths", boot_id: str) -> bool:
    permit = _read_json(paths.boot_clearance)
    return bool(boot_id) and isinstance(permit, dict) and permit.get("approved") is True \
        and permit.get("boot_id") == boot_id


def gpu_answers() -> bool:
    """True when nvidia-smi names a GPU within 20 s (the driver is alive)."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def kernel_trouble_this_boot() -> int | None:
    """Bad kernel lines (Xid / hung / lockup / OOM) since this boot; None if unreadable."""
    try:
        r = subprocess.run(["journalctl", "-k", "-b", "0", "--no-pager", "-q", "-o", "cat"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return sum(1 for line in r.stdout.splitlines()
               if any(bad in line.lower() for bad in KERNEL_BAD))


def _last_load(path: Path) -> dict | None:
    """The newest H3 cold-load record (pool/h3-load-pressure.jsonl)."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            return row
    return None


def _unit_active(unit: str) -> bool | None:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", unit],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() in ("active", "activating", "deactivating", "reloading")


def kernel_trouble_since(ts: float) -> int | None:
    """Count kernel lines since ``ts`` that mean the box itself is unwell.

    None when the kernel log cannot be read (then nothing is cleared)."""
    try:
        r = subprocess.run(["journalctl", "-k", "--no-pager", "-q", "-o", "cat",
                            "--since", f"@{int(ts)}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return sum(1 for line in r.stdout.splitlines()
               if any(bad in line.lower() for bad in KERNEL_BAD))


def _pid_is_controller(pid: int) -> bool:
    """True when ``pid`` is alive AND looks like a studio controller."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return False
    return any(m in cmd for m in CONTROLLER_MARKERS)


def lease_row(db: Path) -> dict | None:
    """The durable lease, read-only. Raises when the database cannot be read."""
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM gpu_lease WHERE singleton=1").fetchone()
        return dict(row) if row is not None else None
    finally:
        con.close()


def studio_active_jobs(timeout: float = 15.0) -> tuple[int, int] | None:
    """(running, queued) as the studio itself reports them; None if it cannot answer."""
    url = local_config.studio_url() + "/api/queue?hist=0"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(local_token.authorize(urllib.request.Request(url)), timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return None
    active = data.get("active") if isinstance(data, dict) else None
    if not isinstance(active, list):
        return None
    jobs = [j for j in active if isinstance(j, dict)]
    return (sum(1 for j in jobs if j.get("status") == "running"),
            sum(1 for j in jobs if j.get("status") == "queued"))


def studio_running_jobs(timeout: float = 15.0) -> int | None:
    """Running jobs as the studio itself reports them; None if it cannot answer."""
    both = studio_active_jobs(timeout)
    return None if both is None else both[0]


def h3_restore_shortfall_gib(since: str = "-3min") -> float | None:
    """How far the newest idle H3 restore missed its memory floor (GiB), if it did."""
    try:
        r = subprocess.run(["journalctl", "--user", "-u", "media-lab-simple.service", "--since",
                            since, "--no-pager", "-q", "-o", "cat"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in reversed(r.stdout.splitlines()):
        if "reconciled idle profile" in line:
            return None                      # the newest attempt worked
        m = DEGRADED.search(line)
        if m:
            return round(float(m.group(1)) - float(m.group(2)), 2)
    return None


def _cached_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("Cached:"):
                return int(line.split()[1]) / 1048576
    except (OSError, ValueError, IndexError):
        pass
    return None


def decide_trim(facts: dict, state: dict, *, enabled: bool) -> tuple[str, str]:
    """(action, reason); action is trim, or idle/skip/wait."""
    now = float(facts["now"])
    if not enabled:
        return "idle", "cache trim is off"
    short = facts.get("h3_shortfall_gib")
    if short is None or short <= 0:
        return "idle", "no idle H3 restore is short of memory"
    if short > TRIM_MAX_SHORTFALL_GIB:
        return "skip", f"H3 restore is {short} GiB short: more than a page-cache trim can fix"
    for key, what in (("hold_exists", "a recovery hold"), ("safety_stop", "the H3 safety stop"),
                      ("latch", "the memwatch latch"), ("baton", ".operator-baton"),
                      ("engine_maintenance", ".engine-maintenance")):
        if facts.get(key):
            return "skip", f"{what} is present"
    active = facts.get("studio_active")
    if active is None:
        return "wait", "the studio did not answer the queue probe"
    if any(active) or facts.get("persisted_running"):
        return "wait", "jobs are running or queued"
    if facts.get("h3_active") is not False:
        return "skip", "H3 is running (or its state is unknown)"
    cached = facts.get("cached_gib")
    if cached is None or cached < TRIM_MIN_CACHED_GIB:
        return "skip", f"only {cached} GiB of page cache: a trim would not help"
    trims = [t for t in state.get("trims", []) if now - float(t) < 86400]
    if trims and now - max(float(t) for t in trims) < TRIM_EVERY_S:
        return "wait", "trimmed less than 30 min ago"
    if len(trims) >= TRIM_DAILY_CAP:
        return "skip", f"{TRIM_DAILY_CAP} trims in 24 h already"
    return "trim", f"idle H3 restore is {short} GiB short; {cached:.1f} GiB of page cache to drop"


def drop_clean_page_cache() -> tuple[bool, dict]:
    before = _mem_available_gib()
    os.sync()
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/tee", "/proc/sys/vm/drop_caches"],
                           input="1\n", capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, {"error": str(exc)}
    after = _mem_available_gib()
    return r.returncode == 0, {"rc": r.returncode, "available_before_gib": round(before or 0, 2),
                               "available_after_gib": round(after or 0, 2),
                               **({"error": r.stderr.strip()[-200:]} if r.returncode else {})}


def gather(paths: Paths, *, now: float | None = None) -> dict:
    """Every fact the decision needs. Read-only."""
    now = time.time() if now is None else now
    facts: dict = {"now": now, "boot_id": _boot_id()}
    facts["hold"] = _read_json(paths.hold) if paths.hold.exists() else None
    facts["hold_exists"] = paths.hold.exists()
    try:
        facts["lease"] = lease_row(paths.db)
    except sqlite3.Error as exc:
        facts["lease"] = None
        facts["lease_error"] = str(exc)
    lease = facts["lease"] or {}
    jobs = (_read_json(paths.jobs) or {}).get("jobs") if paths.jobs.exists() else {}
    jobs = jobs if isinstance(jobs, dict) else {}
    job = jobs.get(str(lease.get("job_id") or "")) if lease else None
    facts["lease_job_status"] = job.get("status") if isinstance(job, dict) else None
    facts["persisted_running"] = sum(1 for j in jobs.values()
                                     if isinstance(j, dict) and j.get("status") == "running")
    facts["studio_running"] = studio_running_jobs()
    facts["baton"] = paths.baton.exists()
    facts["engine_maintenance"] = paths.engine_maintenance.exists()
    facts["safety_stop"] = paths.safety_stop.exists()
    facts["latch"] = paths.latch.exists()
    guard = _read_json(paths.guard)
    facts["guard_state"] = guard.get("state") if isinstance(guard, dict) else None
    facts["guard_fresh"] = bool(
        isinstance(guard, dict) and guard.get("boot_id") == facts["boot_id"]
        and isinstance(guard.get("written_at"), (int, float))
        and 0 <= now - float(guard["written_at"]) <= GUARD_MAX_AGE_S)
    pid = lease.get("pid") if lease else None
    facts["owner_is_controller"] = bool(pid) and _pid_is_controller(int(pid))
    facts["mem_available_gib"] = _mem_available_gib()
    if not facts["hold_exists"] and local_config.int_value(TRIM_FLAG, 0) == 1:
        facts["h3_shortfall_gib"] = h3_restore_shortfall_gib()
        if facts["h3_shortfall_gib"]:
            facts["studio_active"] = studio_active_jobs()
            facts["h3_active"] = _unit_active(H3_UNIT)
            facts["cached_gib"] = _cached_gib()
    facts["sol_configured"] = local_config.sol_configured()
    facts["boot_cleared"] = boot_cleared(paths, facts["boot_id"])
    lease_boot = (lease or {}).get("reason") == "boot-changed"
    if (facts["sol_configured"] and not facts["boot_cleared"]) or lease_boot:
        # After a reboot (the fourth class): the evidence spark1-clear-h3 shows.
        facts["uptime_s"] = _uptime_s()
        facts["studio_active"] = studio_active_jobs()
        facts["h3_active"] = _unit_active(H3_UNIT)
        facts["psi_full_avg60"] = _psi_full_avg60()
        facts["gpu_ok"] = gpu_answers()
        facts["kernel_trouble_boot"] = kernel_trouble_this_boot()
        stop = _read_json(paths.safety_stop) if facts["safety_stop"] else None
        facts["safety_record"] = stop if isinstance(stop, dict) else None
    hold = facts["hold"] if isinstance(facts["hold"], dict) else {}
    if facts["safety_stop"] and str((lease or {}).get("reason") or "").startswith("operation-uncertain:"):
        # Only the guard-trip class needs these (and they cost a subprocess).
        stop = _read_json(paths.safety_stop)
        facts["safety_record"] = stop if isinstance(stop, dict) else None
        incident_id = str((stop or {}).get("incident_id") or "")
        incident = _read_json(paths.incidents / f"{incident_id}.json") if incident_id else None
        facts["incident_status"] = incident.get("status") if isinstance(incident, dict) else None
        try:
            facts["latch_text"] = paths.latch.read_text().strip() if paths.latch.exists() else None
        except OSError:
            facts["latch_text"] = "unreadable"
        facts["last_load"] = _last_load(paths.load_log)
        trip_job = jobs.get(str(hold.get("job_id") or "")) if hold.get("job_id") else None
        facts["trip_job"] = ({k: trip_job.get(k) for k in ("status", "recovery_required", *JOB_OUTPUT_FIELDS)}
                             if isinstance(trip_job, dict) else None)
        facts["h3_active"] = _unit_active(H3_UNIT)
        facts["psi_full_avg60"] = _psi_full_avg60()
        when = float((stop or {}).get("time") or now)
        facts["kernel_trouble"] = kernel_trouble_since(when + KERNEL_CHECK_AFTER_TRIP_S)
    return facts


def hold_key(facts: dict) -> str | None:
    hold, lease = facts.get("hold") or {}, facts.get("lease") or {}
    if not lease:
        return None
    return f"{lease.get('fence')}:{lease.get('job_id')}:{hold.get('created')}"


def _recent(state: dict, now: float) -> list[dict]:
    return [a for a in state.get("attempts", []) if now - float(a.get("ts", 0)) < 86400]


def decide(facts: dict, state: dict, *, enabled: bool, gaveup: bool,
           min_age_s: float = MIN_AGE_S, min_available_gib: float = MIN_AVAILABLE_GIB,
           boot_enabled: bool = False) -> tuple[str, str]:
    """Return (action, reason). action is one of: idle, wait, skip, giveup, reconcile."""
    now = float(facts["now"])
    hold = facts.get("hold")
    lease = facts.get("lease")
    after_reboot = (
        (facts.get("hold_exists") and isinstance(hold, dict) and hold.get("reason") == BOOT_HOLD_REASON
         and isinstance(lease, dict) and lease.get("reason") == "boot-changed")
        or (not facts.get("hold_exists") and facts.get("sol_configured")
            and facts.get("boot_cleared") is False))
    if not facts.get("hold_exists") and not after_reboot:
        return "idle", "no recovery hold"
    if not enabled:
        return "skip", f"{FLAG} is off"
    if gaveup:
        return "skip", "gave up earlier; a person must look (remove autorecover-gaveup.json after)"
    if after_reboot:
        return decide_boot(facts, state, enabled=boot_enabled)
    if not isinstance(hold, dict):
        return "skip", "hold marker is unreadable"
    if not lease:
        return "skip", "hold without a durable lease (ambiguous)"
    reason = str(lease.get("reason") or "")
    if (lease.get("state") == "recovery" and reason.startswith("operation-uncertain:")
            and facts.get("safety_stop")):
        return decide_trip(facts, state, min_age_s=max(min_age_s, TRIP_MIN_AGE_S))
    if lease.get("state") != "recovery" or reason not in SAFE_REASONS:
        return "skip", f"lease is {lease.get('state')}:{reason or 'none'} (not a harmless restart hold)"
    if hold.get("job_id") is not None or hold.get("reason") != f"durable-lease-recovery:{reason}":
        return "skip", "hold marker is not the startup restart hold"
    if not facts.get("boot_id") or lease.get("boot_id") != facts["boot_id"]:
        return "skip", "lease is from another boot (needs boot clearance, not auto-recovery)"
    job_id = str(lease.get("job_id") or "")
    jobless = job_id.startswith(JOBLESS_PREFIXES)
    finished_parked = lease.get("phase") == "parked" and facts.get("lease_job_status") in TERMINAL
    if not (jobless or finished_parked):
        return "skip", f"lease belongs to job {job_id} that is not finished"
    try:
        age = now - float(hold.get("created"))
    except (TypeError, ValueError):
        return "skip", "hold has no creation time"
    if age < min_age_s:
        return "wait", f"hold is {age / 60:.0f} min old; a person gets the first {min_age_s / 60:.0f} min"
    if facts.get("studio_running") is None:
        return "wait", "the studio did not answer the queue probe"
    if facts.get("studio_running") or facts.get("persisted_running"):
        return "wait", "a job is running"
    for key, what in (("baton", ".operator-baton"), ("engine_maintenance", ".engine-maintenance"),
                      ("safety_stop", "the H3 safety stop"), ("latch", "the memwatch latch")):
        if facts.get(key):
            return "skip", f"{what} is present"
    if not facts.get("guard_fresh") or facts.get("guard_state") not in ("ready", "monitoring"):
        return "skip", f"control-plane guard is not current/ready ({facts.get('guard_state')})"
    if facts.get("owner_is_controller"):
        return "skip", "the old owner process is still a live controller"
    avail = facts.get("mem_available_gib")
    if avail is None or avail < min_available_gib:
        return "wait", f"MemAvailable {avail} GiB is below {min_available_gib} GiB"
    limited = _limits(facts, state, now)
    if limited is not None:
        return limited
    return "reconcile", f"harmless {reason} hold for {job_id}, {age / 60:.0f} min old"


def _limits(facts: dict, state: dict, now: float, *, trip: bool = False) -> tuple[str, str] | None:
    """The shared limits: one try per hold, daily caps, backoff, give-up."""
    key = hold_key(facts)
    if any(a.get("hold") == key for a in state.get("attempts", [])):
        return "giveup", "this hold was already tried once and is still there"
    recent = _recent(state, now)
    if len(recent) >= DAILY_CAP:
        return "giveup", f"{DAILY_CAP} attempts in 24 h already"
    if trip and sum(1 for a in recent if a.get("kind") == "guard-trip") >= TRIP_DAILY_CAP:
        return "giveup", "a second guard trip within 24 h: a person must look"
    failures = int(state.get("consecutive_failures", 0))
    if failures >= GIVE_UP_AFTER:
        return "giveup", f"{failures} consecutive failures"
    if failures:
        wait = BACKOFF_S[min(failures, len(BACKOFF_S)) - 1]
        last = max((float(a.get("ts", 0)) for a in state.get("attempts", [])), default=0.0)
        if now - last < wait:
            return "wait", f"backing off {wait // 60} min after a failure"
    return None


def decide_trip(facts: dict, state: dict, *, min_age_s: float = TRIP_MIN_AGE_S) -> tuple[str, str]:
    """The guard-trip-during-cold-load class. (action, reason); action reconcile-trip."""
    now = float(facts["now"])
    hold, lease = facts.get("hold") or {}, facts.get("lease") or {}
    reason = str(lease.get("reason") or "")
    stop = facts.get("safety_record")
    if not isinstance(stop, dict):
        return "skip", "safety stop is unreadable"
    if hold.get("reason") != reason:
        return "skip", "hold marker and lease disagree about the reason"
    boot = facts.get("boot_id")
    if not boot or lease.get("boot_id") != boot or stop.get("boot_id") != boot:
        return "skip", "trip or lease is from another boot"
    if stop.get("reason") not in GUARD_TRIP_REASONS or stop.get("unit") != H3_UNIT:
        return "skip", f"safety stop is not a guard pressure trip ({stop.get('reason')})"
    if facts.get("incident_status") != "terminated":
        return "skip", f"guard incident receipt is {facts.get('incident_status')!r}, not 'terminated'"
    latch = facts.get("latch_text")
    if facts.get("latch") and latch != stop.get("incident_id"):
        return "skip", "the memwatch latch is not from this guard trip"
    if lease.get("engine") != "h3" or lease.get("phase") != "load":
        return "skip", f"lease is {lease.get('engine')}/{lease.get('phase')}, not an H3 cold load"
    load = facts.get("last_load") or {}
    try:
        trip_at = float(stop.get("time"))
        started = float(load.get("started_at"))
        ended = started + float(load.get("load_s") or 0)
    except (TypeError, ValueError):
        return "skip", "no cold-load record for the trip"
    if (load.get("outcome") != "guard-lost" or load.get("task") != lease.get("task")
            or not (started - TRIP_LOAD_SLACK_S <= trip_at <= ended + TRIP_LOAD_SLACK_S)):
        return "skip", "the trip did not happen during the recorded cold load"
    job_id = str(lease.get("job_id") or "")
    if hold.get("job_id") not in (None, job_id):
        return "skip", "hold marker names another job"
    job = facts.get("trip_job")
    if job is not None:
        if job.get("status") not in ("error", "cancelled"):
            return "skip", f"tripped job is {job.get('status')}; clearing would re-run it"
        if any(job.get(k) for k in JOB_OUTPUT_FIELDS):
            return "skip", "tripped job has output (not a pure cold load)"
    elif not job_id.startswith(JOBLESS_PREFIXES):
        return "skip", f"tripped job {job_id} is unknown"
    try:
        age = now - max(float(hold.get("created")), trip_at)
    except (TypeError, ValueError):
        return "skip", "hold has no creation time"
    if age < min_age_s:
        return "wait", f"guard trip is {age / 60:.0f} min old; waiting {min_age_s / 60:.0f} min"
    if facts.get("studio_running") is None:
        return "wait", "the studio did not answer the queue probe"
    if facts.get("studio_running") or facts.get("persisted_running"):
        return "wait", "a job is running"
    for key, what in (("baton", ".operator-baton"), ("engine_maintenance", ".engine-maintenance")):
        if facts.get(key):
            return "skip", f"{what} is present"
    if not facts.get("guard_fresh") or facts.get("guard_state") != "quarantined":
        return "skip", f"control-plane guard is not current/quarantined ({facts.get('guard_state')})"
    if facts.get("h3_active") is not False:
        return "skip", "H3 is still running (or its state is unknown)"
    if facts.get("owner_is_controller"):
        return "skip", "the old owner process is still a live controller"
    avail, psi60 = facts.get("mem_available_gib"), facts.get("psi_full_avg60")
    if avail is None or avail < TRIP_MIN_AVAILABLE_GIB:
        return "wait", f"memory has not recovered: MemAvailable {avail} GiB < {TRIP_MIN_AVAILABLE_GIB}"
    if psi60 is None or psi60 > TRIP_MAX_PSI_FULL_AVG60:
        return "wait", f"memory pressure has not settled: PSI full avg60 {psi60}"
    trouble = facts.get("kernel_trouble")
    if trouble is None:
        return "skip", "cannot read the kernel log"
    if trouble:
        return "skip", f"{trouble} kernel error line(s) since the trip (Xid/hung/OOM): a person must look"
    limited = _limits(facts, state, now, trip=True)
    if limited is not None:
        return limited
    return "reconcile-trip", (f"guard {stop.get('reason')} trip during the {lease.get('task')} cold load "
                              f"for {job_id}, {age / 60:.0f} min ago; memory recovered "
                              f"({avail:.0f} GiB free, PSI60 {psi60})")


def boot_hold_job(facts: dict) -> tuple[str | None, str | None]:
    """(lease job to reconcile, refusal). (None, None) when no hold carried over."""
    if not facts.get("hold_exists"):
        return None, None
    hold, lease = facts.get("hold") or {}, facts.get("lease") or {}
    if lease.get("state") != "recovery" or lease.get("reason") != "boot-changed":
        return None, f"lease is {lease.get('state')}:{lease.get('reason')}, not a boot-changed recovery"
    if not lease.get("boot_id") or lease.get("boot_id") == facts.get("boot_id"):
        return None, "the boot-changed lease names this boot (inconsistent)"
    job_id = str(lease.get("job_id") or "")
    if hold.get("job_id") not in (None, job_id):
        return None, f"hold marker names job {hold.get('job_id')}, the lease {job_id}"
    status = facts.get("lease_job_status")
    if job_id.startswith(JOBLESS_PREFIXES):
        return job_id, None
    if lease.get("phase") == "parked" and status in TERMINAL:
        return job_id, None                   # a finished take left parked
    if status in ("running", "queued"):
        return job_id, None                   # a take the reboot interrupted: ends as an error
    return None, f"lease job {job_id or '?'} is {status or 'unknown'} in phase {lease.get('phase')}"


def decide_boot(facts: dict, state: dict, *, enabled: bool) -> tuple[str, str]:
    """After a reboot: (action, reason); action boot-clear, wait, skip or giveup."""
    now = float(facts["now"])
    boot = str(facts.get("boot_id") or "")
    if not enabled:
        return "skip", f"{BOOT_FLAG} is off; H3 waits for spark1-clear-h3"
    if not boot:
        return "skip", "cannot read this boot's id"
    job_id, refusal = boot_hold_job(facts)
    if refusal:
        return "giveup", f"the carried-over hold is not one the reconcile tool clears exactly: {refusal}"
    stop = facts.get("safety_record")
    if facts.get("safety_stop"):
        if not isinstance(stop, dict) or not stop.get("boot_id"):
            return "giveup", "an H3 safety stop is set and does not say which boot it is from"
        if stop.get("boot_id") == boot:
            return "giveup", f"the H3 safety stop was set on THIS boot ({stop.get('reason')})"
    if facts.get("latch"):
        return "giveup", "the memwatch latch is set on this boot (memory ran low after the reboot)"
    if facts.get("gpu_ok") is False:
        return "giveup", "the GPU does not answer nvidia-smi"
    trouble = facts.get("kernel_trouble_boot")
    if trouble:
        return "giveup", f"{trouble} kernel error line(s) this boot (Xid/hung task/OOM)"
    mine = [a for a in state.get("attempts", []) if a.get("kind") == "boot" and a.get("boot") == boot]
    if len(mine) >= BOOT_TRIES_PER_BOOT:
        return "giveup", f"{BOOT_TRIES_PER_BOOT} tries on this boot did not clear it"
    cleared = {a.get("boot") for a in _recent(state, now) if a.get("kind") == "boot" and a.get("ok")}
    if len(cleared - {boot}) >= BOOT_DAILY_CAP:
        return "giveup", f"{BOOT_DAILY_CAP} reboots cleared in 24 h already: a reboot loop needs a person"
    waiting = _boot_wait_reason(facts, mine, now)
    if waiting:
        up = facts.get("uptime_s")
        if up is not None and float(up) >= BOOT_GIVE_UP_AFTER_S:
            return "giveup", f"still not cleared {float(up) / 60:.0f} min after the reboot: {waiting}"
        return "wait", waiting
    what = f"the carried-over hold ({job_id})" if job_id else "no hold carried over"
    return "boot-clear", (f"reboot: clear H3 for boot {boot[:8]}; {what}; memory "
                          f"{facts.get('mem_available_gib'):.0f} GiB free, PSI60 {facts.get('psi_full_avg60')}, "
                          f"GPU answers, kernel log clean")


def _boot_wait_reason(facts: dict, mine: list, now: float) -> str | None:
    up = facts.get("uptime_s")
    if up is None or float(up) < BOOT_MIN_UPTIME_S:
        return f"the box has been up {0 if up is None else float(up):.0f} s; waiting {BOOT_MIN_UPTIME_S} s"
    if mine and not mine[-1].get("ok"):
        wait = BOOT_BACKOFF_S[min(len(mine), len(BOOT_BACKOFF_S)) - 1]
        if now - float(mine[-1].get("ts", 0)) < wait:
            return f"backing off {wait // 60} min after a failed try"
    for key, what in (("baton", ".operator-baton"), ("engine_maintenance", ".engine-maintenance")):
        if facts.get(key):
            return f"{what} is present"
    active = facts.get("studio_active")
    if active is None:
        return "the studio did not answer the queue probe"
    if active[0] or facts.get("persisted_running"):
        return "a job is running"
    if facts.get("h3_active"):
        return "the H3 unit is running"
    prior_stop = bool(facts.get("safety_stop"))
    ok_states = ("ready", "monitoring") + (("quarantined",) if prior_stop else ())
    if not facts.get("guard_fresh") or facts.get("guard_state") not in ok_states:
        return f"control-plane guard is not current/ready ({facts.get('guard_state')})"
    if facts.get("gpu_ok") is None:
        return "GPU state unknown"
    if facts.get("kernel_trouble_boot") is None:
        return "cannot read this boot's kernel log"
    avail, psi60 = facts.get("mem_available_gib"), facts.get("psi_full_avg60")
    if avail is None or avail < BOOT_MIN_AVAILABLE_GIB:
        return f"MemAvailable {avail} GiB is below {BOOT_MIN_AVAILABLE_GIB:.0f} GiB"
    if psi60 is None or psi60 > BOOT_MAX_PSI_FULL_AVG60:
        return f"memory pressure has not settled: PSI full avg60 {psi60}"
    return None


def preserve_boot_evidence(paths: Paths, facts: dict) -> Path:
    """Copy what the previous boot left behind before anything is changed."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(float(facts["now"])))
    evidence = paths.evidence_root / f"autorecover-boot-{stamp}"
    evidence.mkdir(parents=True, exist_ok=False)
    for src in (paths.hold, paths.boot_clearance, paths.safety_stop):
        if src.exists():
            shutil.copy2(src, evidence / src.name)
    stop = facts.get("safety_record") or {}
    incident = paths.incidents / f"{stop.get('incident_id')}.json" if stop.get("incident_id") else None
    if incident is None and paths.incidents.is_dir():
        found = sorted(paths.incidents.glob("*.json"), key=lambda p: p.stat().st_mtime)
        incident = found[-1] if found else None
    if incident is not None and incident.exists():
        shutil.copy2(incident, evidence / f"newest-incident-{incident.name}")
    (evidence / "facts.json").write_text(json.dumps(facts, sort_keys=True, default=str))
    for name, cmd in (
            ("previous-boot-journal-tail.txt", ["journalctl", "-b", "-1", "-n", "400", "--no-pager", "-o", "short-iso"]),
            ("previous-boot-kernel-warnings.txt", ["journalctl", "-b", "-1", "-k", "-p", "warning",
                                                   "-n", "300", "--no-pager", "-o", "short-iso"]),
            ("this-boot-kernel-warnings.txt", ["journalctl", "-b", "0", "-k", "-p", "warning",
                                               "--no-pager", "-o", "short-iso"]),
            ("boots.txt", ["journalctl", "--list-boots", "--no-pager"]),
            ("pstore.txt", ["ls", "-la", "/var/lib/systemd/pstore"])):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            (evidence / name).write_text((r.stdout or "")[-2_000_000:] + (r.stderr or "")[-2000:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            (evidence / name).write_text(f"unavailable: {exc}\n")
    return evidence


def write_boot_clearance(paths: Paths, boot_id: str, evidence: Path, why: str) -> None:
    """The same file spark1-clear-h3 writes, for THIS boot only; the old one is kept."""
    paths.sol_root.mkdir(parents=True, exist_ok=True)
    if paths.boot_clearance.exists():
        os.replace(paths.boot_clearance, paths.boot_clearance.with_name(
            f"boot-clearance.pre-{boot_id[:8]}-{int(time.time())}.json"))
    _save(paths.boot_clearance, {
        "approved": True, "boot_id": boot_id, "at": time.time(),
        "approved_by": "auto: runner/hold_autorecover.py (owner decision, 2026-09-27)",
        "evidence": str(evidence), "why": why})


def _save(path: Path, value: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True))
    os.replace(tmp, path)


def _audit(paths: Paths, row: dict) -> None:
    try:
        if paths.log.exists() and paths.log.stat().st_size > 1_000_000:
            os.replace(paths.log, paths.log.with_suffix(".log.1"))
        with paths.log.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


def _guard_state(paths: Paths) -> str | None:
    guard = _read_json(paths.guard)
    if not (isinstance(guard, dict) and guard.get("boot_id") == _boot_id()):
        return None
    try:
        if not 0 <= time.time() - float(guard.get("written_at")) <= GUARD_MAX_AGE_S:
            return None
    except (TypeError, ValueError):
        return None
    return guard.get("state")


def set_aside_trip_markers(paths: Paths, facts: dict, *, sleep=time.sleep,
                           guard_state=_guard_state) -> tuple[Path, list[tuple[Path, Path]]]:
    """Preserve the evidence, then move the safety stop and latch into it.

    Returns (evidence dir, [(moved_to, original)]). Raises (after putting the
    markers back) when the guard does not come back to ``ready``."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(float(facts["now"])))
    evidence = paths.evidence_root / f"autorecover-trip-{stamp}"
    evidence.mkdir(parents=True, exist_ok=False)
    stop = facts.get("safety_record") or {}
    incident = paths.incidents / f"{stop.get('incident_id')}.json"
    for src in (paths.hold, incident):
        if src.exists():
            shutil.copy2(src, evidence / src.name)
    (evidence / "facts.json").write_text(json.dumps(
        {k: v for k, v in facts.items() if k not in ("hold",)}, sort_keys=True, default=str))
    try:
        trip = float(stop.get("time"))
        journal = subprocess.run(
            ["journalctl", "--user", "--no-pager", "-q", "--since", f"@{int(trip) - 600}",
             "--until", f"@{int(trip) + 120}", "-u", "media-lab-simple", "-u", H3_UNIT,
             "-u", "solh3-control-plane-guard"],
            capture_output=True, text=True, timeout=60)
        (evidence / "journal.txt").write_text(journal.stdout[-2_000_000:])
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        pass
    moved: list[tuple[Path, Path]] = []
    try:
        for src in (paths.safety_stop, paths.latch):
            if src.exists():
                dst = evidence / src.name
                shutil.copy2(src, dst)          # latch lives on tmpfs: copy, then remove
                src.unlink()
                moved.append((dst, src))
        deadline = time.time() + GUARD_READY_WAIT_S
        while guard_state(paths) not in ("ready", "monitoring"):
            if time.time() >= deadline:
                raise RuntimeError(f"guard did not report ready after the markers were set aside "
                                   f"({guard_state(paths)})")
            sleep(1.0)
    except Exception:
        restore_trip_markers(moved)
        raise
    return evidence, moved


def restore_trip_markers(moved: list[tuple[Path, Path]]) -> None:
    """Put the safety stop / latch back (never over a newer one)."""
    for saved, original in moved:
        try:
            if not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, original)
        except OSError as exc:
            log(f"could not restore {original}: {exc}")


def run_reconcile(paths: Paths, job_id: str) -> tuple[bool, dict]:
    """The sanctioned tool; it stops and restarts the studio itself."""
    try:
        r = subprocess.run([paths.python, str(paths.tool), "--job-id", job_id],
                           cwd=str(paths.root), capture_output=True, text=True,
                           timeout=RECONCILE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, {"error": "reconcile timed out"}
    receipt: dict = {"rc": r.returncode}
    for line in reversed((r.stdout or "").splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            receipt["result"] = {k: parsed.get(k) for k in (
                "reconciled_job", "internal_residency_recovery", "terminal_parked_recovery",
                "hold_exists", "pool_restored")}
            receipt["proof"] = {k: (parsed.get("proof") or {}).get(k) for k in (
                "processes_gone", "memory_recovered", "available_gib")}
            break
    if r.returncode != 0:
        receipt["error"] = (r.stderr or r.stdout or "").strip()[-400:]
    ok = r.returncode == 0 and (receipt.get("result") or {}).get("hold_exists") is False
    return ok, receipt


def main(argv: list[str] | None = None, *, paths: Paths | None = None,
         reconcile=run_reconcile, facts: dict | None = None,
         set_aside=set_aside_trip_markers, restore=restore_trip_markers,
         trim=None, boot_set_aside=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print the decision, change nothing")
    args = ap.parse_args(argv)
    paths = paths or Paths()
    trim = trim or (lambda _p: drop_clean_page_cache())
    facts = facts if facts is not None else gather(paths)
    state = _read_json(paths.state) or {}
    if state.get("gaveup_at") and not paths.gaveup.exists() and not args.dry_run:
        # A person looked and removed the give-up flag: start counting afresh.
        state["consecutive_failures"] = 0
        state.pop("gaveup_at", None)
        _save(paths.state, state)
    enabled = local_config.int_value(FLAG, 0) == 1
    boot_enabled = local_config.int_value(BOOT_FLAG, 0) == 1
    action, why = decide(facts, state, enabled=enabled, gaveup=paths.gaveup.exists(),
                         boot_enabled=boot_enabled)
    lease = facts.get("lease") or {}
    out = {"ts": facts["now"], "action": action, "why": why, "dry_run": args.dry_run,
           "lease_job": lease.get("job_id"), "lease_reason": lease.get("reason")}
    if action == "idle":
        t_action, t_why = decide_trim(facts, state, enabled=(
            enabled and local_config.int_value(TRIM_FLAG, 0) == 1))
        if t_action != "idle":
            out.update(action=f"cache-{t_action}", why=t_why)
            if t_action == "trim" and not args.dry_run:
                ok, receipt = trim(paths)
                state.setdefault("trims", []).append(facts["now"])
                state["trims"] = [t for t in state["trims"] if facts["now"] - float(t) < 86400]
                _save(paths.state, state)
                out.update(ok=ok, receipt=receipt)
                _audit(paths, out)
                log(f"CACHE TRIM: {t_why}: {receipt}")
                return out
            log(f"cache-{t_action}: {t_why}" + (" (dry run)" if args.dry_run else ""))
            if t_action == "skip" and not args.dry_run and state.get("last_logged") != t_why:
                _audit(paths, out)
                state["last_logged"] = t_why
                _save(paths.state, state)
            return out
    if args.dry_run or action in ("idle",):
        log(f"{action}: {why}" + (" (dry run)" if args.dry_run else ""))
        return out
    if action in ("wait", "skip"):
        log(f"{action}: {why}")
        if action == "skip" or state.get("last_logged") != why:
            _audit(paths, out)
        state["last_logged"] = why
        _save(paths.state, state)
        return out
    if action == "giveup":
        state["gaveup_at"] = facts["now"]
        _save(paths.state, state)
        _save(paths.gaveup, {"ts": facts["now"], "why": why, "lease_job": lease.get("job_id"),
                             "lease_reason": lease.get("reason"),
                             "next_step": "Look at the hold, clear it with "
                                          "tools/reconcile-gpu-recovery.py --job-id <job> "
                                          "(after a reboot: spark1-clear-h3 from VibeX), "
                                          "then remove pool/autorecover-gaveup.json"})
        log(f"GIVING UP: {why}")
        _audit(paths, out)
        return out
    if action == "boot-clear":
        return _boot_clear(paths, facts, state, out, why, reconcile=reconcile,
                           set_aside=boot_set_aside or set_aside_boot_stop, restore=restore)
    log(f"RECONCILING: {why}")
    started = time.time()
    trip = action == "reconcile-trip"
    moved: list = []
    if trip:
        try:
            evidence, moved = set_aside(paths, facts)
            out["evidence"] = str(evidence)
        except Exception as exc:
            ok, receipt = False, {"error": f"could not set the trip markers aside: {exc}"}
        else:
            ok, receipt = reconcile(paths, str(lease.get("job_id")))
            if not ok:
                restore(moved)
                receipt["markers_restored"] = True
    else:
        ok, receipt = reconcile(paths, str(lease.get("job_id")))
    attempt = {"ts": facts["now"], "hold": hold_key(facts), "ok": ok,
               "kind": "guard-trip" if trip else "restart",
               "seconds": round(time.time() - started, 1)}
    state.setdefault("attempts", []).append(attempt)
    state["attempts"] = state["attempts"][-20:]
    state["consecutive_failures"] = 0 if ok else int(state.get("consecutive_failures", 0)) + 1
    state["last_logged"] = None
    _save(paths.state, state)
    out.update(ok=ok, receipt=receipt)
    _audit(paths, out)
    log(("cleared the hold" if ok else "reconcile did NOT clear the hold") + f": {receipt}")
    if not ok and state["consecutive_failures"] >= GIVE_UP_AFTER:
        state["gaveup_at"] = facts["now"]
        _save(paths.state, state)
        _save(paths.gaveup, {"ts": facts["now"], "why": f"{GIVE_UP_AFTER} failed attempts",
                             "lease_job": lease.get("job_id"), "receipt": receipt})
    return out


def set_aside_boot_stop(paths: Paths, evidence: Path, *, sleep=time.sleep,
                        guard_state=_guard_state) -> list[tuple[Path, Path]]:
    """Move a PREVIOUS boot's safety stop into the evidence and wait for the guard."""
    moved: list[tuple[Path, Path]] = []
    if not paths.safety_stop.exists():
        return moved
    dst = evidence / f"set-aside-{paths.safety_stop.name}"
    shutil.copy2(paths.safety_stop, dst)
    paths.safety_stop.unlink()
    moved.append((dst, paths.safety_stop))
    try:
        deadline = time.time() + GUARD_READY_WAIT_S
        while guard_state(paths) not in ("ready", "monitoring"):
            if time.time() >= deadline:
                raise RuntimeError(f"guard did not report ready after the old safety stop was "
                                   f"set aside ({guard_state(paths)})")
            sleep(1.0)
    except Exception:
        restore_trip_markers(moved)
        raise
    return moved


def _boot_clear(paths: Paths, facts: dict, state: dict, out: dict, why: str, *,
                reconcile, set_aside, restore) -> dict:
    boot = str(facts["boot_id"])
    job_id, _ = boot_hold_job(facts)
    log(f"AFTER REBOOT: {why}")
    started = time.time()
    moved: list = []
    receipt: dict = {}
    try:
        evidence = preserve_boot_evidence(paths, facts)
        out["evidence"] = str(evidence)
        moved = set_aside(paths, evidence)
        write_boot_clearance(paths, boot, evidence, why)
        receipt["boot_clearance"] = "written"
        if job_id:
            ok, rec = reconcile(paths, job_id)
            receipt.update(rec)
        else:
            ok = True
    except Exception as exc:                       # noqa: BLE001  recorded, then retried
        ok = False
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    if not ok and moved:
        restore(moved)
        receipt["markers_restored"] = True
    attempt = {"ts": facts["now"], "hold": hold_key(facts), "ok": ok, "kind": "boot", "boot": boot,
               "seconds": round(time.time() - started, 1)}
    state.setdefault("attempts", []).append(attempt)
    state["attempts"] = state["attempts"][-20:]
    state["last_logged"] = None
    mine = [a for a in state["attempts"] if a.get("kind") == "boot" and a.get("boot") == boot]
    if not ok and len(mine) >= BOOT_TRIES_PER_BOOT:
        state["gaveup_at"] = facts["now"]
        _save(paths.gaveup, {"ts": facts["now"], "why": f"{BOOT_TRIES_PER_BOOT} tries after the reboot "
                             f"failed: {receipt.get('error') or 'the hold is still there'}",
                             "lease_job": job_id, "receipt": receipt,
                             "next_step": "Read pool/autorecover.log, then spark1-clear-h3 from VibeX, "
                                          "then remove pool/autorecover-gaveup.json"})
    _save(paths.state, state)
    out.update(ok=ok, receipt=receipt)
    _audit(paths, out)
    log(("cleared H3 for this boot" + (" and the carried-over hold" if job_id else "")
         if ok else "after-reboot clear did NOT finish") + f": {receipt}")
    return out


if __name__ == "__main__":
    main()
