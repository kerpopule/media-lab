from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TERMINAL = {"succeeded", "failed", "cancelled"}
INPUT_OWNER_BYTES = 256 * 1024**2
INPUT_TOTAL_BYTES = 2 * 1024**3
TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancel_requested", "queued"},  # queued: released by a busy worker (see release)
    "cancel_requested": {"cancelled", "failed"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


class InvalidTransition(RuntimeError):
    pass


class RequestConflict(ValueError):
    """A retry changed the content of an already accepted request."""


class JobStore:
    """Small transactional queue suitable for one API and one leased GPU worker."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    epoch INTEGER NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    claimed_by TEXT,
                    lease_until REAL,
                    heartbeat_at REAL,
                    cancel_requested_at REAL,
                    result_json TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_claim_idx
                    ON jobs(status, priority DESC, created_at ASC);
                CREATE TABLE IF NOT EXISTS studio_requests (
                    owner TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
                    request_json TEXT NOT NULL,
                    PRIMARY KEY(owner, request_id)
                );
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '1');
                CREATE TABLE IF NOT EXISTS studio_inputs (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    data BLOB NOT NULL,
                    bytes INTEGER NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(owner, sha256)
                );
                """
            )

    def enqueue(self, kind: str, payload: dict, *, priority: int = 0, job_id: str | None = None) -> str:
        now = time.time()
        jid = job_id or uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,kind,payload_json,status,stage,priority,created_at,updated_at) "
                "VALUES(?,?,?,'queued','queued',?,?,?)",
                (jid, kind, json.dumps(payload, sort_keys=True), priority, now, now),
            )
        return jid

    def enqueue_once(self, owner: str, request_id: str, kind: str, payload: dict, *, cancel: bool = False) -> str:
        """Commit ownership and submission together; retries survive API restarts.

        A cancellation creates a terminal record if submission never arrived,
        preventing late retries from queuing work. Both operations serialize in
        the same transaction, including cancellation of already accepted work.

        ``owner`` must come from verified authentication, never a request body.
        The worker sees the same queue as ordinary enqueue(), with no credentials
        or ownership fields injected into its model payload.
        """
        self._validate_owner(owner)
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
            raise ValueError("request ID must contain 16–128 letters, digits, underscores or hyphens")
        if not isinstance(kind, str) or not kind or not isinstance(payload, dict):
            raise ValueError("a job kind and payload object are required")
        # Reject non-JSON numbers before writing either record. Canonical JSON
        # makes a retried object independent of dictionary insertion order.
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False, separators=(",", ":"))
        fingerprint = json.dumps([kind, payload], sort_keys=True, allow_nan=False, separators=(",", ":"))
        now = time.time()
        jid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT job_id,request_json FROM studio_requests WHERE owner=? AND request_id=?",
                (owner, request_id),
            ).fetchone()
            if previous:
                if previous["request_json"] != fingerprint:
                    db.execute("ROLLBACK")
                    raise RequestConflict("This request ID already belongs to a different generation.")
                if cancel:
                    db.execute(
                        "UPDATE jobs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancel_requested' END, "
                        "epoch=epoch+1,cancel_requested_at=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
                        (now, now, previous["job_id"]),
                    )
                db.execute("COMMIT")
                return previous["job_id"]
            db.execute(
                "INSERT INTO jobs(id,kind,payload_json,status,stage,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)", (jid, kind, encoded, 'cancelled' if cancel else 'queued',
                                           'cancelled' if cancel else 'queued', now, now),
            )
            db.execute(
                "INSERT INTO studio_requests(owner,request_id,job_id,request_json) VALUES(?,?,?,?)",
                (owner, request_id, jid, fingerprint),
            )
            db.execute("COMMIT")
        return jid

    @staticmethod
    def _validate_owner(owner: str) -> None:
        if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner):
            raise ValueError("owner must be a verified 32-character device identity")

    def get_owned(self, owner: str, job_id: str) -> dict | None:
        self._validate_owner(owner)
        with self.connect() as db:
            row = db.execute(
                "SELECT jobs.* FROM jobs JOIN studio_requests ON jobs.id=studio_requests.job_id "
                "WHERE studio_requests.owner=? AND jobs.id=?", (owner, job_id),
            ).fetchone()
        return self._decode(row) if row else None

    def list_owned(self, owner: str, *, limit: int = 50, before: str | None = None) -> list[dict]:
        """Stable newest-first history; cursors must also belong to this owner."""
        self._validate_owner(owner)
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError('Invalid history page size.')
        with self.connect() as db:
            clause, args = '', [owner]
            if before is not None:
                pivot = db.execute(
                    'SELECT jobs.created_at FROM jobs JOIN studio_requests ON jobs.id=studio_requests.job_id '
                    'WHERE studio_requests.owner=? AND jobs.id=?', (owner, before),
                ).fetchone()
                if pivot is None:
                    raise ValueError('This history cursor is unavailable.')
                clause = ' AND (jobs.created_at < ? OR (jobs.created_at = ? AND jobs.id < ?))'
                args.extend([pivot['created_at'], pivot['created_at'], before])
            rows = db.execute(
                'SELECT jobs.* FROM jobs JOIN studio_requests ON jobs.id=studio_requests.job_id '
                'WHERE studio_requests.owner=?' + clause +
                ' ORDER BY jobs.created_at DESC, jobs.id DESC LIMIT ?', (*args, limit),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def find_owned_request(self, owner: str, request_id: str) -> dict | None:
        self._validate_owner(owner)
        with self.connect() as db:
            row = db.execute(
                "SELECT jobs.* FROM jobs JOIN studio_requests ON jobs.id=studio_requests.job_id "
                "WHERE studio_requests.owner=? AND studio_requests.request_id=?", (owner, request_id),
            ).fetchone()
        return self._decode(row) if row else None

    def get(self, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode(row) if row else None

    def put_input(self, owner: str, data: bytes, *, width: int, height: int) -> dict:
        """Atomically retain an already-decoded image; deduplicate per owner."""
        import hashlib
        self._validate_owner(owner)
        if not isinstance(data, bytes) or not 0 < len(data) <= 20 * 1024**2:
            raise ValueError('Choose an image no larger than 20 MiB.')
        digest = hashlib.sha256(data).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id,sha256,bytes,width,height FROM studio_inputs WHERE owner=? AND sha256=?',
                             (owner, digest)).fetchone()
            if row is None:
                owned = db.execute('SELECT COALESCE(SUM(bytes),0) FROM studio_inputs WHERE owner=?', (owner,)).fetchone()[0]
                total = db.execute('SELECT COALESCE(SUM(bytes),0) FROM studio_inputs').fetchone()[0]
                if owned + len(data) > INPUT_OWNER_BYTES or total + len(data) > INPUT_TOTAL_BYTES:
                    raise ValueError('The accepted-image storage limit has been reached.')
                iid = uuid.uuid4().hex
                db.execute('INSERT INTO studio_inputs VALUES(?,?,?,?,?,?,?,?)',
                           (iid, owner, digest, data, len(data), width, height, time.time()))
                row = db.execute('SELECT id,sha256,bytes,width,height FROM studio_inputs WHERE id=?', (iid,)).fetchone()
            db.execute('COMMIT')
        return dict(row)

    def input_metadata(self, owner: str, input_id: str) -> dict | None:
        self._validate_owner(owner)
        with self.connect() as db:
            row = db.execute('SELECT id,sha256,bytes,width,height FROM studio_inputs WHERE owner=? AND id=?',
                             (owner, input_id)).fetchone()
        return dict(row) if row else None

    def read_job_input(self, job_id: str, input_id: str) -> bytes:
        with self.connect() as db:
            row = db.execute('SELECT studio_inputs.data FROM studio_inputs JOIN studio_requests '
                             'ON studio_inputs.owner=studio_requests.owner '
                             'WHERE studio_requests.job_id=? AND studio_inputs.id=?', (job_id, input_id)).fetchone()
        if row is None:
            raise ValueError('This job does not own the accepted image.')
        return bytes(row['data'])

    def claim_next(self, worker_id: str, *, lease_seconds: float = 60) -> dict | None:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,epoch FROM jobs WHERE status='queued' "
                "ORDER BY priority DESC,created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            changed = db.execute(
                "UPDATE jobs SET status='running',stage='starting',epoch=epoch+1,"
                "claimed_by=?,lease_until=?,heartbeat_at=?,updated_at=? "
                "WHERE id=? AND status='queued' AND epoch=?",
                (worker_id, now + lease_seconds, now, now, row["id"], row["epoch"]),
            ).rowcount
            db.execute("COMMIT")
            if changed != 1:
                return None
        return self.get(row["id"])

    def heartbeat(self, job_id: str, worker_id: str, stage: str, *, lease_seconds: float = 60) -> bool:
        now = time.time()
        with self.connect() as db:
            changed = db.execute(
                "UPDATE jobs SET stage=?,heartbeat_at=?,lease_until=?,updated_at=? "
                "WHERE id=? AND claimed_by=? AND status IN ('running','cancel_requested')",
                (stage, now, now + lease_seconds, now, job_id, worker_id),
            ).rowcount
        return changed == 1

    def claim_cpu_job(self, worker_id: str, engine_id: str, *, lease_seconds: float = 60, kind: str = 'image') -> dict | None:
        """Claim/recover only owned jobs for an exact independent engine.

        Caller MUST hold the canonical CPU process lock through terminal
        publication. Expiry alone does not prove an orphaned model has stopped;
        its inherited lock supplies that proof. Use a fresh worker_id per run.
        Legacy jobs and jobs for every other engine remain untouched.
        """
        now = time.time()
        if kind not in ('image', 'model', 'audio', 'video'):
            raise ValueError('Unsupported CPU job kind.')
        scope = ("kind=? AND json_extract(payload_json,'$.engineId')=? "
                 "AND id IN (SELECT job_id FROM studio_requests)")
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(
                "UPDATE jobs SET status=CASE WHEN status='cancel_requested' THEN 'cancelled' ELSE 'queued' END,"
                "stage=CASE WHEN status='cancel_requested' THEN 'cancelled' ELSE 'recovering' END,"
                "epoch=epoch+1,claimed_by=NULL,lease_until=NULL,heartbeat_at=NULL,updated_at=? "
                "WHERE status IN ('running','cancel_requested') AND lease_until<=? AND " + scope,
                (now, now, kind, engine_id))
            row = db.execute("SELECT id FROM jobs WHERE status='queued' AND " + scope +
                             " ORDER BY priority DESC,created_at ASC LIMIT 1", (kind, engine_id)).fetchone()
            if row is not None:
                db.execute("UPDATE jobs SET status='running',stage='starting',epoch=epoch+1,"
                           "claimed_by=?,lease_until=?,heartbeat_at=?,updated_at=? WHERE id=?",
                           (worker_id, now + lease_seconds, now, now, row['id']))
            db.execute('COMMIT')
        return self.get(row['id']) if row is not None else None

    def request_cancel(self, job_id: str) -> bool:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,epoch FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] in TERMINAL:
                db.execute("ROLLBACK")
                return False
            target = "cancelled" if row["status"] == "queued" else "cancel_requested"
            changed = db.execute(
                "UPDATE jobs SET status=?,stage=?,epoch=epoch+1,cancel_requested_at=?,updated_at=? "
                "WHERE id=? AND epoch=?",
                (target, target, now, now, job_id, row["epoch"]),
            ).rowcount
            db.execute("COMMIT")
        return changed == 1

    def release(self, job_id: str, worker_id: str) -> dict:
        """Hand a claimed job back to the queue untouched when the worker cannot run it right now.

        Used when a shared resource (the GPU lease, memory) is busy: the job keeps its position and
        payload, loses its claim and lease, and a pending cancellation wins instead of a requeue."""
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,epoch,claimed_by FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(job_id)
            if row["claimed_by"] != worker_id or row["status"] not in ("running", "cancel_requested"):
                db.execute("ROLLBACK")
                raise InvalidTransition("worker does not own a running job")
            target = "cancelled" if row["status"] == "cancel_requested" else "queued"
            db.execute(
                "UPDATE jobs SET status=?,stage=?,epoch=epoch+1,claimed_by=NULL,lease_until=NULL,heartbeat_at=NULL,updated_at=? "
                "WHERE id=? AND epoch=?",
                (target, target, now, job_id, row["epoch"]),
            )
            db.execute("COMMIT")
        return self.get(job_id)

    def transition(
        self,
        job_id: str,
        worker_id: str,
        target: str,
        *,
        stage: str | None = None,
        result: dict | None = None,
        error: str | None = None,
    ) -> dict:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,epoch,claimed_by FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(job_id)
            current = row["status"]
            if row["claimed_by"] != worker_id:
                db.execute("ROLLBACK")
                raise InvalidTransition("worker does not own this job")
            if target not in TRANSITIONS.get(current, set()):
                db.execute("ROLLBACK")
                raise InvalidTransition(f"invalid transition {current} -> {target}")
            db.execute(
                "UPDATE jobs SET status=?,stage=?,epoch=epoch+1,updated_at=?,result_json=?,error=?,"
                "lease_until=CASE WHEN ? IN ('succeeded','failed','cancelled') THEN NULL ELSE lease_until END "
                "WHERE id=? AND epoch=?",
                (
                    target,
                    stage or target,
                    now,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    error,
                    target,
                    job_id,
                    row["epoch"],
                ),
            )
            db.execute("COMMIT")
        value = self.get(job_id)
        assert value is not None
        return value

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        raw_result = value.pop("result_json")
        value["result"] = json.loads(raw_result) if raw_result else None
        return value
