"""Durable, fenced admission for Media Lab GPU work.

This module is deliberately stdlib-only so the app, engine shims, shell-launch
helpers and recovery tools can share one protocol.  A POSIX flock provides live
cross-process exclusion; SQLite provides durable ownership, monotonically
increasing fences, queue idempotency and reboot/crash quarantine.

An active or recovery lease is never aged out.  Only exact-fence reconciliation
with process, boot and memory proof may clear it.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, ClassVar


class LeaseBusy(RuntimeError):
    pass


class StaleFence(RuntimeError):
    pass


class CapacityUnqualified(RuntimeError):
    pass


@dataclass
class Lease:
    fence: int
    owner: str
    pid: int
    boot_id: str
    job_id: str
    engine: str
    task: str
    phase: str
    state: str = "active"
    _fd: IO[Any] | None = field(default=None, repr=False, compare=False)


class DurableGpuProtocol:
    """One durable local-GPU owner plus an independent durable job queue."""

    _NEXT_PHASES: ClassVar[dict[str, set[str]]] = {
        "drain": {"unload"},
        "unload": {"reclaim"},
        "reclaim": {"load", "released"},
        # A load refused before any engine started goes back through unload and
        # reclaim; release still needs exact process and memory proof.
        "load": {"render", "unload"},
        "render": {"unload"},
        "parked": {"drain", "render"},
    }

    def __init__(
        self,
        db_path: str | Path,
        lock_path: str | Path,
        *,
        boot_id: Callable[[], str] | None = None,
        available_gib: Callable[[], float] | None = None,
        pid_alive: Callable[[int], bool] | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.db_path = Path(db_path)
        self.lock_path = Path(lock_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._boot_id = boot_id or self._read_boot_id
        self._available_gib = available_gib or self._read_available_gib
        self._pid_alive = pid_alive or self._default_pid_alive
        self._now = now
        self._initialize()

    @staticmethod
    def _read_boot_id() -> str:
        linux = Path("/proc/sys/kernel/random/boot_id")
        if linux.exists():
            return linux.read_text().strip()
        # macOS has no /proc boot UUID. kern.boottime is stable for one boot and
        # changes on reboot; hash it with the host name so receipts stay opaque.
        try:
            boot = subprocess.check_output(
                ["sysctl", "-n", "kern.boottime"], text=True, timeout=5
            ).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("stable boot identity is unavailable") from exc
        return hashlib.sha256(f"{os.uname().nodename}:{boot}".encode()).hexdigest()

    @staticmethod
    def _read_available_gib() -> float:
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
        try:
            text = subprocess.check_output(["vm_stat"], text=True, timeout=5)
            page_size = 4096
            first = text.splitlines()[0] if text else ""
            digits = "".join(ch for ch in first if ch.isdigit())
            if digits:
                page_size = int(digits)
            pages = 0
            for line in text.splitlines()[1:]:
                label, _, value = line.partition(":")
                if label in {"Pages free", "Pages inactive", "Pages speculative"}:
                    pages += int(value.strip().rstrip("."))
            if pages:
                return pages * page_size / (1024 ** 3)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise RuntimeError("MemAvailable is unavailable") from exc
        raise RuntimeError("MemAvailable is unavailable")

    @staticmethod
    def _default_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sequence (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO sequence(name, value) VALUES ('gpu_fence', 0);
                INSERT OR IGNORE INTO sequence(name, value) VALUES ('job_fence', 0);
                CREATE TABLE IF NOT EXISTS gpu_lease (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    fence INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    boot_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    task TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','recovery')),
                    reason TEXT,
                    runtime_pid INTEGER,
                    runtime_identity TEXT,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capacity (
                    engine TEXT NOT NULL,
                    task TEXT NOT NULL,
                    peak_gib REAL NOT NULL CHECK(peak_gib > 0),
                    warm_render_gib REAL CHECK(warm_render_gib > 0),
                    reserve_gib REAL NOT NULL CHECK(reserve_gib >= 0),
                    evidence TEXT NOT NULL,
                    measured REAL NOT NULL,
                    PRIMARY KEY(engine, task)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    lane TEXT NOT NULL CHECK(lane IN ('local','cloud')),
                    engine TEXT NOT NULL,
                    task TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','done','cancelled','recovery')),
                    owner TEXT,
                    fence INTEGER,
                    result_hash TEXT,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_lane_state_created
                    ON jobs(lane, state, created, job_id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    event TEXT NOT NULL,
                    job_id TEXT,
                    fence INTEGER,
                    detail TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(gpu_lease)")}
            if "runtime_pid" not in columns:
                db.execute("ALTER TABLE gpu_lease ADD COLUMN runtime_pid INTEGER")
            if "runtime_identity" not in columns:
                db.execute("ALTER TABLE gpu_lease ADD COLUMN runtime_identity TEXT")
            capacity_columns = {row[1] for row in db.execute("PRAGMA table_info(capacity)")}
            if "warm_render_gib" not in capacity_columns:
                db.execute("ALTER TABLE capacity ADD COLUMN warm_render_gib REAL")

    @staticmethod
    def _row_lease(row: sqlite3.Row, fd: IO[Any] | None = None) -> Lease:
        return Lease(
            fence=row["fence"], owner=row["owner"], pid=row["pid"],
            boot_id=row["boot_id"], job_id=row["job_id"], engine=row["engine"],
            task=row["task"], phase=row["phase"], state=row["state"], _fd=fd,
        )

    @staticmethod
    def _next_fence(db: sqlite3.Connection, name: str) -> int:
        db.execute("UPDATE sequence SET value=value+1 WHERE name=?", (name,))
        row = db.execute("SELECT value FROM sequence WHERE name=?", (name,)).fetchone()
        return int(row[0])

    def qualify(self, engine: str, task: str, *, peak_gib: float,
                reserve_gib: float, evidence: str,
                warm_render_gib: float | None = None) -> None:
        if (not evidence.strip() or peak_gib <= 0 or reserve_gib < 0
                or (warm_render_gib is not None and warm_render_gib <= 0)):
            raise ValueError("capacity evidence is required")
        with self._connect() as db:
            db.execute(
                "INSERT INTO capacity(engine,task,peak_gib,warm_render_gib,reserve_gib,evidence,measured) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(engine,task) DO UPDATE SET "
                "peak_gib=excluded.peak_gib,warm_render_gib=excluded.warm_render_gib,"
                "reserve_gib=excluded.reserve_gib,"
                "evidence=excluded.evidence,measured=excluded.measured",
                (engine, task, float(peak_gib),
                 float(warm_render_gib) if warm_render_gib is not None else None,
                 float(reserve_gib), evidence, self._now()),
            )

    def _check_capacity(self, db: sqlite3.Connection, engine: str, task: str,
                        *, warm: bool = False) -> None:
        row = db.execute(
            "SELECT peak_gib,warm_render_gib,reserve_gib FROM capacity "
            "WHERE engine=? AND task=?",
            (engine, task),
        ).fetchone()
        if row is None:
            raise CapacityUnqualified(f"no measured capacity for {engine}/{task}")
        if warm and row["warm_render_gib"] is None:
            raise CapacityUnqualified(
                f"no measured warm-render capacity for {engine}/{task}"
            )
        envelope = row["warm_render_gib"] if warm else row["peak_gib"]
        required = float(envelope) + float(row["reserve_gib"])
        available = float(self._available_gib())
        if available < required:
            raise CapacityUnqualified(
                f"{engine}/{task} requires {required:.1f} GiB including reserve; "
                f"only {available:.1f} GiB available"
            )

    def capacity_deficit_gib(self, engine: str, task: str, *, warm: bool = False) -> float:
        """Return the measured admission deficit without weakening admission.

        Callers may use this before ``acquire``/``retarget`` to reclaim only
        known-idle capacity consumers.  Admission still calls ``_check_capacity``
        against a fresh measurement, so this observation is never an approval.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT peak_gib,warm_render_gib,reserve_gib FROM capacity "
                "WHERE engine=? AND task=?",
                (engine, task),
            ).fetchone()
        if row is None:
            raise CapacityUnqualified(f"no measured capacity for {engine}/{task}")
        if warm and row["warm_render_gib"] is None:
            raise CapacityUnqualified(
                f"no measured warm-render capacity for {engine}/{task}"
            )
        envelope = row["warm_render_gib"] if warm else row["peak_gib"]
        required = float(envelope) + float(row["reserve_gib"])
        return max(0.0, required - float(self._available_gib()))

    def acquire(self, *, job_id: str, engine: str, task: str, owner: str) -> Lease:
        fd = self.lock_path.open("a+")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LeaseBusy("GPU live lock is held") from exc
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM gpu_lease WHERE singleton=1").fetchone()
                if current is not None:
                    raise LeaseBusy(
                        f"GPU lease {current['fence']} is {current['state']} "
                        f"for {current['owner']}:{current['job_id']}"
                    )
                fence = self._next_fence(db, "gpu_fence")
                boot = self._boot_id()
                now = self._now()
                db.execute(
                    "INSERT INTO gpu_lease(singleton,fence,owner,pid,boot_id,job_id,engine,task,phase,state,reason,updated) "
                    "VALUES(1,?,?,?,?,?,?,?,?, 'active', NULL, ?)",
                    (fence, owner, os.getpid(), boot, job_id, engine, task, "drain", now),
                )
                db.execute("COMMIT")
                return Lease(fence, owner, os.getpid(), boot, job_id, engine, task, "drain", _fd=fd)
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                db.close()
        except Exception:
            fd.close()
            raise

    def _exact(self, db: sqlite3.Connection, lease: Lease) -> sqlite3.Row:
        row = db.execute("SELECT * FROM gpu_lease WHERE singleton=1").fetchone()
        if row is None or row["fence"] != lease.fence or row["owner"] != lease.owner:
            raise StaleFence(f"lease fence {lease.fence} no longer owns the GPU")
        return row

    def advance(self, lease: Lease, phase: str) -> Lease:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._exact(db, lease)
                if row["state"] != "active":
                    raise LeaseBusy("recovery lease cannot advance")
                if phase not in self._NEXT_PHASES.get(row["phase"], set()):
                    raise ValueError(f"invalid GPU phase transition {row['phase']} -> {phase}")
                if phase == "load":
                    self._check_capacity(db, row["engine"], row["task"])
                db.execute("UPDATE gpu_lease SET phase=?,updated=? WHERE singleton=1",
                           (phase, self._now()))
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        lease.phase = phase
        return lease

    def park(self, lease: Lease, *, proof: Mapping[str, object]) -> Lease:
        if proof.get("engine") != lease.engine or proof.get("healthy") is not True or proof.get("busy") is not False:
            raise ValueError("park requires exact healthy idle engine proof")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._exact(db, lease)
                if row["state"] != "active" or row["phase"] != "render":
                    raise LeaseBusy("only a rendering lease can become parked residency")
                now = self._now()
                db.execute("UPDATE gpu_lease SET phase='parked',updated=? WHERE singleton=1", (now,))
                db.execute("INSERT INTO events(ts,event,job_id,fence,detail) VALUES(?,?,?,?,?)",
                           (now, "parked", lease.job_id, lease.fence,
                            json.dumps({"engine": lease.engine, "task": lease.task}, sort_keys=True)))
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction: db.execute("ROLLBACK")
                raise
        lease.phase = "parked"
        return lease

    def adopt_warm(self, lease: Lease, *, proof: Mapping[str, object]) -> Lease:
        if (proof.get("engine") != lease.engine or proof.get("task") != lease.task or
                proof.get("healthy") is not True or proof.get("busy") is not False):
            raise ValueError("warm adoption requires exact healthy idle engine/task proof")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._exact(db, lease)
                if row["state"] != "active" or row["phase"] != "drain":
                    raise LeaseBusy("warm residency may be adopted only during drain")
                self._check_capacity(db, row["engine"], row["task"], warm=True)
                db.execute("UPDATE gpu_lease SET phase='render',updated=? WHERE singleton=1", (self._now(),))
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction: db.execute("ROLLBACK")
                raise
        lease.phase = "render"
        return lease

    def bind_process(self, lease: Lease, *, pid: int, identity: str) -> None:
        """Persist the exact engine process that received this render fence."""
        if int(pid) <= 0 or not str(identity).strip():
            raise ValueError("exact runtime pid and process identity are required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._exact(db, lease)
                if row["state"] != "active" or row["phase"] not in ("load", "render"):
                    raise LeaseBusy("runtime identity may be bound only during load/render")
                db.execute(
                    "UPDATE gpu_lease SET runtime_pid=?,runtime_identity=?,updated=? WHERE singleton=1",
                    (int(pid), str(identity), self._now()),
                )
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def adopt_recovered(self, lease: Lease, *, owner: str,
                        proof: Mapping[str, object]) -> Lease:
        """Resume only the same job on the same proven idle runtime after restart."""
        if lease.state != "recovery" or lease._fd is None:
            raise LeaseBusy("recovery adoption requires the retained live exclusion lock")
        required = {"boot_id": lease.boot_id, "job_id": lease.job_id,
                    "engine": lease.engine, "task": lease.task}
        if any(proof.get(key) != value for key, value in required.items()):
            raise ValueError("recovery proof does not identify the exact prior operation")
        if proof.get("healthy") is not True or proof.get("busy") is not False:
            raise ValueError("recovery adoption requires a healthy idle runtime")
        try:
            proof_pid = int(proof.get("pid") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("recovery process pid is invalid") from exc
        if proof.get("boot_id") != self._boot_id() or not self._pid_alive(proof_pid):
            raise ValueError("current-boot live process proof is required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._exact(db, lease)
                if row["state"] != "recovery" or row["phase"] not in ("render", "parked"):
                    raise LeaseBusy("only an idle render/parked recovery lease may be adopted")
                if (row["runtime_pid"] is None or int(row["runtime_pid"]) != proof_pid or
                        not row["runtime_identity"] or
                        row["runtime_identity"] != proof.get("process_identity")):
                    raise ValueError("runtime process identity is missing or changed")
                fence = self._next_fence(db, "gpu_fence")
                now = self._now()
                boot = self._boot_id()
                db.execute(
                    "UPDATE gpu_lease SET fence=?,owner=?,pid=?,boot_id=?,phase='render',"
                    "state='active',reason=NULL,updated=? WHERE singleton=1",
                    (fence, owner, os.getpid(), boot, now),
                )
                db.execute("INSERT INTO events(ts,event,job_id,fence,detail) VALUES(?,?,?,?,?)",
                           (now, "recovery-adopted", lease.job_id, fence,
                            json.dumps({"engine": lease.engine, "task": lease.task,
                                        "runtime_pid": proof_pid}, sort_keys=True)))
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        lease.fence, lease.owner, lease.pid = fence, owner, os.getpid()
        lease.boot_id, lease.phase, lease.state = boot, "render", "active"
        return lease

    def retarget(self, lease: Lease, *, job_id: str, engine: str, task: str,
                 owner: str, warm_proof: Mapping[str, object] | None = None) -> Lease:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._exact(db, lease)
                if row["state"] != "active" or row["phase"] != "parked":
                    raise LeaseBusy("only parked residency can be retargeted")
                warm = bool(warm_proof and engine == lease.engine and
                            task == lease.task and warm_proof.get("engine") == engine and
                            warm_proof.get("task") == task and
                            warm_proof.get("healthy") is True and warm_proof.get("busy") is False)
                if warm:
                    self._check_capacity(db, engine, task, warm=True)
                fence = self._next_fence(db, "gpu_fence")
                phase = "render" if warm else "drain"
                boot = self._boot_id(); now = self._now()
                db.execute("UPDATE gpu_lease SET job_id=?,engine=?,task=?,owner=?,fence=?,phase=?,pid=?,boot_id=?,reason=NULL,runtime_pid=NULL,runtime_identity=NULL,updated=? WHERE singleton=1",
                           (job_id, engine, task, owner, fence, phase, os.getpid(), boot, now))
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction: db.execute("ROLLBACK")
                raise
        lease.fence, lease.job_id, lease.engine, lease.task = fence, job_id, engine, task
        lease.owner, lease.pid, lease.boot_id, lease.phase = owner, os.getpid(), boot, phase
        return lease

    def mark_recovery(self, lease: Lease, reason: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._exact(db, lease)
                db.execute(
                    "UPDATE gpu_lease SET state='recovery',reason=?,updated=? WHERE singleton=1",
                    (reason, self._now()),
                )
                db.execute("UPDATE jobs SET state='recovery',updated=? WHERE job_id=? AND state='running'",
                           (self._now(), lease.job_id))
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        lease.state = "recovery"

    @staticmethod
    def _validate_reclamation(proof: Mapping[str, object], *, boot_id: str | None = None) -> None:
        if proof.get("processes_gone") is not True:
            raise ValueError("exact process proof is required")
        if proof.get("memory_recovered") is not True:
            raise ValueError("measured memory proof is required")
        if boot_id is not None and proof.get("boot_id") != boot_id:
            raise ValueError("current boot proof is required")

    def release(self, lease: Lease, *, proof: Mapping[str, object]) -> None:
        self._validate_reclamation(proof, boot_id=self._boot_id())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._exact(db, lease)
                if row["state"] != "active" or row["phase"] != "reclaim":
                    raise LeaseBusy("active lease may release only after reclamation")
                db.execute("DELETE FROM gpu_lease WHERE singleton=1")
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        if lease._fd is not None:
            lease._fd.close()
            lease._fd = None

    def reconcile(self, lease: Lease, *, proof: Mapping[str, object]) -> None:
        current_boot = self._boot_id()
        self._validate_reclamation(proof, boot_id=current_boot)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._exact(db, lease)
                if row["state"] != "recovery":
                    raise LeaseBusy("only a recovery lease may be reconciled")
                db.execute("DELETE FROM gpu_lease WHERE singleton=1")
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        if lease._fd is not None:
            lease._fd.close()
            lease._fd = None

    def recover_startup(self) -> Lease | None:
        """Quarantine prior ownership and retain/re-take the live exclusion lock."""
        boot = self._boot_id()
        recovered: Lease | None = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM gpu_lease WHERE singleton=1").fetchone()
                if row is not None:
                    # A recovery reason is part of the durable, fence-bound
                    # operator hold. Preserve it across later controller
                    # restarts so an exact marker can still reconcile the same
                    # quarantined lease. Only active/parked ownership receives
                    # a newly diagnosed restart reason.
                    if row["state"] == "recovery" and row["reason"]:
                        reason = str(row["reason"])
                    else:
                        reason = "controller-restarted"
                        if row["boot_id"] != boot:
                            reason = "boot-changed"
                        elif not self._pid_alive(int(row["pid"])):
                            reason = "owner-exited"
                    db.execute(
                        "UPDATE gpu_lease SET state='recovery',reason=?,updated=? WHERE singleton=1",
                        (reason, self._now()),
                    )
                    recovered = Lease(
                        int(row["fence"]), str(row["owner"]), int(row["pid"]),
                        str(row["boot_id"]), str(row["job_id"]), str(row["engine"]),
                        str(row["task"]), str(row["phase"]), state="recovery",
                    )
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        if recovered is None:
            return None
        fd = self.lock_path.open("a+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fd.close()
        else:
            recovered._fd = fd
        return recovered

    def wait_for_recovery_lock(self, lease: Lease) -> bool:
        """Queue behind a surviving owner, then retain exclusion for recovery."""
        if lease.state != "recovery":
            raise LeaseBusy("only a recovery lease may wait for exclusion")
        if lease._fd is not None:
            return True
        fd = self.lock_path.open("a+")
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self._connect() as db:
            row = db.execute("SELECT * FROM gpu_lease WHERE singleton=1").fetchone()
            exact = bool(
                row is not None and row["state"] == "recovery"
                and int(row["fence"]) == lease.fence and row["owner"] == lease.owner
            )
        if not exact:
            fd.close()
            return False
        lease._fd = fd
        return True

    def authorize(self, *, fence: int, job_id: str, engine: str, task: str,
                  phases: tuple[str, ...] = ("render",)) -> bool:
        """Validate a controller-delegated engine request against the live fence."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM gpu_lease WHERE singleton=1").fetchone()
            exact = bool(
                row is not None
                and row["state"] == "active"
                and row["phase"] in phases
                and row["boot_id"] == self._boot_id()
                and int(row["fence"]) == int(fence)
                and row["job_id"] == job_id
                and row["engine"] == engine
                and row["task"] == task
            )
        if not exact:
            return False
        # PID namespaces make host-PID liveness checks invalid inside the LTX
        # container.  The canonical flock is the process-lifetime proof: a
        # delegated child must observe it held by the controller.  If this probe
        # can acquire the file, the owner is gone (or never held it), so the
        # durable row alone cannot authorize work.
        probe = self.lock_path.open("a+")
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(probe, fcntl.LOCK_UN)
                return False
        finally:
            probe.close()

    def submit(self, job_id: str, *, lane: str, engine: str, task: str,
               payload_hash: str) -> dict:
        if lane not in {"local", "cloud"}:
            raise ValueError("lane must be local or cloud")
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is not None:
                    identity = (row["lane"], row["engine"], row["task"], row["payload_hash"])
                    if identity != (lane, engine, task, payload_hash):
                        raise ValueError(f"idempotency conflict for {job_id}")
                    db.execute("COMMIT")
                    return dict(row)
                db.execute(
                    "INSERT INTO jobs(job_id,lane,engine,task,payload_hash,state,created,updated) "
                    "VALUES(?,?,?,?,?,'queued',?,?)",
                    (job_id, lane, engine, task, payload_hash, now, now),
                )
                row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                db.execute("COMMIT")
                return dict(row)
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def claim_next(self, *, lane: str, owner: str) -> dict | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM jobs WHERE lane=? AND state='queued' ORDER BY created,job_id LIMIT 1",
                    (lane,),
                ).fetchone()
                if row is None:
                    db.execute("COMMIT")
                    return None
                fence = self._next_fence(db, "job_fence")
                db.execute(
                    "UPDATE jobs SET state='running',owner=?,fence=?,updated=? "
                    "WHERE job_id=? AND state='queued'",
                    (owner, fence, self._now(), row["job_id"]),
                )
                claimed = db.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
                db.execute("COMMIT")
                return dict(claimed)
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def cancel(self, job_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute(
                "UPDATE jobs SET state='cancelled',updated=? WHERE job_id=? AND state IN ('queued','running')",
                (self._now(), job_id),
            )
            return cur.rowcount == 1

    def complete(self, job_id: str, *, fence: int, result_hash: str) -> bool:
        with self._connect() as db:
            cur = db.execute(
                "UPDATE jobs SET state='done',result_hash=?,updated=? "
                "WHERE job_id=? AND state='running' AND fence=?",
                (result_hash, self._now(), job_id, int(fence)),
            )
            return cur.rowcount == 1

    def job(self, job_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return dict(row) if row is not None else None

    def snapshot(self) -> dict:
        with self._connect() as db:
            lease = db.execute("SELECT * FROM gpu_lease WHERE singleton=1").fetchone()
            jobs = db.execute(
                "SELECT lane,state,COUNT(*) AS count FROM jobs GROUP BY lane,state"
            ).fetchall()
            return {
                "lease": dict(lease) if lease is not None else None,
                "jobs": [dict(row) for row in jobs],
            }


@dataclass(frozen=True)
class TransitionHooks:
    drain: Callable[[Lease], object]
    unload: Callable[[Lease], Mapping[str, object]]
    reclaim: Callable[[Lease], Mapping[str, object]]
    load: Callable[[Lease], object]
    render: Callable[[Lease, Mapping[str, str]], object]


def run_transition_sequence(
    protocol: DurableGpuProtocol,
    items: Sequence[Mapping[str, str]],
    *,
    hooks: TransitionHooks,
    owner: str,
) -> list[object]:
    """Execute a synthetic or real mixed-engine sequence under exact fences.

    Each item is a complete drain→unload→reclaim→load→render→unload→reclaim
    transaction.  Any uncertain hook outcome leaves the durable lease in
    recovery and stops the sequence; later work is never admitted over it.
    """
    results: list[object] = []
    for item in items:
        lease: Lease | None = None
        try:
            lease = protocol.acquire(
                job_id=item["job_id"], engine=item["engine"], task=item["task"], owner=owner
            )
            hooks.drain(lease)
            protocol.advance(lease, "unload")
            initial_process = hooks.unload(lease)
            if initial_process.get("processes_gone") is not True:
                raise RuntimeError("outgoing process reclamation unverified")
            protocol.advance(lease, "reclaim")
            initial_memory = hooks.reclaim(lease)
            if initial_memory.get("memory_recovered") is not True:
                raise RuntimeError("outgoing memory reclamation unverified")
            protocol.advance(lease, "load")
            hooks.load(lease)
            protocol.advance(lease, "render")
            results.append(hooks.render(lease, item))
            protocol.advance(lease, "unload")
            final_process = hooks.unload(lease)
            if final_process.get("processes_gone") is not True:
                raise RuntimeError("final process reclamation unverified")
            protocol.advance(lease, "reclaim")
            final_memory = hooks.reclaim(lease)
            proof = {
                "processes_gone": final_process.get("processes_gone") is True,
                "memory_recovered": final_memory.get("memory_recovered") is True,
                "boot_id": lease.boot_id,
            }
            protocol.release(lease, proof=proof)
        except Exception as exc:
            if lease is not None:
                try:
                    protocol.mark_recovery(lease, f"transition-failed:{type(exc).__name__}")
                except StaleFence:
                    pass
            raise
    return results


def payload_digest(payload: Mapping[str, object]) -> str:
    """Stable queue-idempotency digest without storing request contents here."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
