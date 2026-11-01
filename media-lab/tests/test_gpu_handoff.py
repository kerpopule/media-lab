"""A planned restart hands an idle parked residency over instead of holding.

The protocol here is the real durable protocol on a temp SQLite file; the app
functions are the real ones from app.py, executed with fakes for the engine
probes (the same technique as test_engine_transition_boundaries.py).
"""
import ast
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_lab_core import gpu_handoff
from media_lab_core.durable_gpu_protocol import DurableGpuProtocol

APP = Path(__file__).parents[1] / "app.py"
BOOT = "boot-a"
RUNTIME_PID = 4242
RUNTIME_ID = f"{BOOT}:{RUNTIME_PID}:991"
T2VA = {"variant": "fl2va", "task": "t2va", "turbo_preset": None}
REAL_LONG = {"variant": "singularity", "task": "singularity", "turbo_preset": None}


def protocol(tmp_path, *, boot=BOOT, alive=lambda pid: pid == RUNTIME_PID):
    p = DurableGpuProtocol(tmp_path / "gpu.sqlite3", tmp_path / "gpu.lock",
                           boot_id=lambda: boot, available_gib=lambda: 120.0,
                           pid_alive=alive)
    for task in ("t2va", "singularity"):
        p.qualify("h3", task, peak_gib=10.0, warm_render_gib=2.0, reserve_gib=2.0,
                  evidence="fixture")
    return p


def parked_residency(p, *, task="t2va", owner=None):
    """What an idle restore leaves behind: a parked lease bound to the engine."""
    owner = owner or f"media-lab-simple:{os.getpid()}"
    lease = p.acquire(job_id="idle-restore-h3", engine="h3", task=task, owner=owner)
    for phase in ("unload", "reclaim", "load", "render"):
        p.advance(lease, phase)
    p.bind_process(lease, pid=RUNTIME_PID, identity=RUNTIME_ID)
    p.park(lease, proof={"engine": "h3", "task": task, "healthy": True, "busy": False})
    return lease


def graceful_record(p, lease, *, resident=T2VA, now=None):
    return gpu_handoff.plan(
        p.snapshot()["lease"], owner=lease.owner, boot_id=BOOT,
        live_identity=(RUNTIME_PID, RUNTIME_ID), warm={"healthy": True, "busy": False},
        resident_config=resident, running_jobs=0, hold_exists=False, now=now)


def restart(tmp_path, lease, **kw):
    """The old controller exits (its flock closes); a new one starts up."""
    lease._fd.close(); lease._fd = None
    p = protocol(tmp_path, **kw)
    recovered = p.recover_startup()
    return p, recovered, p.snapshot()["lease"]


def validate(record, row, **overrides):
    kw = dict(boot_id=BOOT, live_identity=(RUNTIME_PID, RUNTIME_ID),
              warm={"healthy": True, "busy": False}, resident_config=T2VA,
              running_jobs=0, hold_exists=False)
    kw.update(overrides)
    return gpu_handoff.validate(record, row, **kw)


@pytest.mark.parametrize("task,resident", [("t2va", T2VA), ("singularity", REAL_LONG)])
def test_planned_restart_adopts_parked_residency_without_a_hold(tmp_path, task, resident):
    p = protocol(tmp_path)
    lease = parked_residency(p, task=task)
    record = graceful_record(p, lease, resident=resident)
    p2, recovered, row = restart(tmp_path, lease)
    assert row["state"] == "recovery" and row["reason"] == "owner-exited"

    proof = validate(record, row, resident_config=resident)
    adopted = gpu_handoff.adopt(p2, recovered, owner="new-controller", proof=proof,
                                park_proof={"engine": "h3", "task": task,
                                            "healthy": True, "busy": False})

    after = p2.snapshot()["lease"]
    assert (after["state"], after["phase"], after["owner"]) == ("active", "parked", "new-controller")
    assert after["fence"] > record["fence"] and adopted.fence == after["fence"]
    assert (after["runtime_pid"], after["runtime_identity"]) == (RUNTIME_PID, RUNTIME_ID)
    assert (after["engine"], after["task"]) == ("h3", task)
    # The next job retargets the adopted residency like any parked lease.
    p2.retarget(adopted, job_id="next", engine="h3", task=task, owner="new-controller",
                warm_proof={"engine": "h3", "task": task, "healthy": True, "busy": False})
    assert p2.snapshot()["lease"]["phase"] == "render"


@pytest.mark.parametrize("change", [
    "stale", "future", "boot", "fence", "owner", "job", "task", "runtime_pid",
    "runtime_identity", "version",
])
def test_record_mismatch_refuses(tmp_path, change):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    record = graceful_record(p, lease)
    _, _, row = restart(tmp_path, lease)
    if change == "stale":
        record["created"] -= gpu_handoff.MAX_AGE_S + 1
    elif change == "future":
        record["created"] += 60
    elif change == "boot":
        record["boot_id"] = "boot-b"
    elif change == "fence":
        record["fence"] += 1
    elif change == "owner":
        record["owner"] = "someone-else"
    elif change == "job":
        record["job_id"] = "another-job"
    elif change == "task":
        record["task"] = "fl2va"
    elif change == "runtime_pid":
        record["runtime_pid"] = 9999
    elif change == "runtime_identity":
        record["runtime_identity"] = "boot-a:4242:stale"
    elif change == "version":
        record["version"] = 99
    with pytest.raises(gpu_handoff.HandoffRefused):
        validate(record, row)


@pytest.mark.parametrize("override", [
    {"live_identity": None},                                   # engine died
    {"live_identity": (RUNTIME_PID, "boot-a:4242:restarted")},  # engine restarted
    {"warm": {"healthy": True, "busy": True}},                 # engine busy
    {"warm": {"healthy": False, "busy": False}},               # engine unhealthy
    {"resident_config": {"variant": "ref2va", "task": "t2va", "turbo_preset": None}},
    {"running_jobs": 1},
    {"hold_exists": True},                                     # latch/safety-stop/hold
    {"boot_id": "boot-b"},                                     # reboot since the stop
])
def test_live_state_mismatch_refuses(tmp_path, override):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    record = graceful_record(p, lease)
    _, _, row = restart(tmp_path, lease)
    with pytest.raises(gpu_handoff.HandoffRefused):
        validate(record, row, **override)


def test_missing_record_refuses(tmp_path):
    """A crash or SIGKILL never runs the shutdown hook: hold as before."""
    p = protocol(tmp_path)
    lease = parked_residency(p)
    _, _, row = restart(tmp_path, lease)
    with pytest.raises(gpu_handoff.HandoffRefused, match="no handoff record"):
        validate(None, row)


def test_owner_still_alive_is_not_a_handoff(tmp_path):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    record = graceful_record(p, lease)
    _, _, row = restart(tmp_path, lease, alive=lambda pid: True)
    assert row["reason"] == "controller-restarted"
    with pytest.raises(gpu_handoff.HandoffRefused, match="owner-exited"):
        validate(record, row)


def test_boot_change_is_not_a_handoff(tmp_path):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    record = graceful_record(p, lease)
    _, _, row = restart(tmp_path, lease, boot="boot-b")
    assert row["reason"] == "boot-changed"
    with pytest.raises(gpu_handoff.HandoffRefused):
        validate(record, row, boot_id="boot-b")


@pytest.mark.parametrize("phase_steps", [(), ("unload",), ("render",)])
def test_shutdown_refuses_unless_idle_parked(tmp_path, phase_steps):
    """A running operation (drain/load/render) is never handed over."""
    p = protocol(tmp_path)
    owner = f"media-lab-simple:{os.getpid()}"
    lease = p.acquire(job_id="job-1", engine="h3", task="t2va", owner=owner)
    if phase_steps == ("render",):
        for phase in ("unload", "reclaim", "load", "render"):
            p.advance(lease, phase)
        p.bind_process(lease, pid=RUNTIME_PID, identity=RUNTIME_ID)
    elif phase_steps:
        p.advance(lease, "unload")
    with pytest.raises(gpu_handoff.HandoffRefused, match="not active/parked"):
        graceful_record(p, lease)


def test_shutdown_refuses_recovery_lease_running_job_and_foreign_owner(tmp_path):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    row = p.snapshot()["lease"]
    base = dict(owner=lease.owner, boot_id=BOOT, live_identity=(RUNTIME_PID, RUNTIME_ID),
                warm={"healthy": True, "busy": False}, resident_config=T2VA,
                running_jobs=0, hold_exists=False)
    for override in ({"running_jobs": 2}, {"hold_exists": True}, {"owner": "other"},
                     {"live_identity": None}, {"warm": {"healthy": True, "busy": True}},
                     {"boot_id": "boot-b"}):
        with pytest.raises(gpu_handoff.HandoffRefused):
            gpu_handoff.plan(row, **{**base, **override})
    p.mark_recovery(lease, "operation-uncertain:TimeoutError")
    with pytest.raises(gpu_handoff.HandoffRefused):
        gpu_handoff.plan(p.snapshot()["lease"], **base)


def test_record_is_written_atomically_and_consumed_once(tmp_path):
    path = tmp_path / "gpu-handoff.json"
    gpu_handoff.write(path, {"version": 1, "fence": 7})
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert gpu_handoff.consume(path) == {"version": 1, "fence": 7}
    assert not path.exists()
    assert gpu_handoff.consume(path) is None
    path.write_text("{half-written")
    assert gpu_handoff.consume(path) is None and not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_failed_repark_fails_closed(tmp_path):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    record = graceful_record(p, lease)
    p2, recovered, row = restart(tmp_path, lease)
    proof = validate(record, row)
    with pytest.raises(ValueError):
        gpu_handoff.adopt(p2, recovered, owner="new", proof=proof,
                          park_proof={"engine": "h3", "task": "t2va",
                                      "healthy": False, "busy": False})
    after = p2.snapshot()["lease"]
    assert after["state"] == "recovery" and after["reason"] == "handoff-repark-failed"


# ---------------------------------------------------------------- app wiring

def _functions(*names, **namespace):
    tree = ast.parse(APP.read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), namespace)
    return namespace


class _Config:
    def __init__(self, flag):
        self.flag = flag

    def int_value(self, key, default):
        return self.flag if key == gpu_handoff.FLAG else default


def _app(tmp_path, p, *, flag=1, engine_alive=True, resident=T2VA, busy=False, jobs=None,
         held=False, workers_disabled=False):
    holds = []
    # Model this fixture's controller, not the pytest process's worker setting.
    # Keep the process-wide safety flag untouched for real app imports.
    fixture_os = SimpleNamespace(**{
        **vars(os),
        "getenv": lambda key, default=None: ("1" if workers_disabled else "0")
        if key == "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS" else os.getenv(key, default),
    })
    ns = _functions(
        "_gpu_graceful_handoff_enabled", "_gpu_resident_config", "_gpu_handoff_warm",
        "_gpu_write_handoff_on_shutdown", "_gpu_adopt_handoff", "initialize_gpu_cutover",
        os=fixture_os, threading=threading, print=lambda *a, **k: None,
        local_config=_Config(flag), _gpu_handoff=gpu_handoff,
        gpu_protocol=lambda: p, GPU_HANDOFF=tmp_path / "gpu-handoff.json",
        GPU_RECOVERY_HOLD=tmp_path / "gpu-recovery-hold.json",
        _gpu_protocol_mutex=threading.RLock(), _gpu_active_lease=None, _gpu_cutover_ready=False,
        ENGINES={"h3": {}}, jobs=jobs if jobs is not None else {},
        gpu_recovery_pending=lambda: held,
        _gpu_process_identity=lambda e: (RUNTIME_PID, RUNTIME_ID) if engine_alive else None,
        _gpu_exact_idle=lambda e: not busy, engine_busy=lambda e: busy,
        h3_resident_config=lambda: resident,
        _gpu_restart_adoption_proof=lambda r: (_ for _ in ()).throw(
            RuntimeError("restart ownership is missing exact job/process identity proof")),
        hold_gpu_recovery=lambda reason, j=None: holds.append(reason),
    )
    ns["holds"] = holds
    return ns


def test_disabled_controller_never_writes_a_handoff(tmp_path):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    old = _app(tmp_path, p, workers_disabled=True)
    old["_gpu_active_lease"] = lease
    assert old["_gpu_write_handoff_on_shutdown"]() is None
    assert not (tmp_path / "gpu-handoff.json").exists()


def test_app_planned_restart_end_to_end_leaves_no_hold(tmp_path):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    old = _app(tmp_path, p)
    old["_gpu_active_lease"] = lease
    record = old["_gpu_write_handoff_on_shutdown"]()
    assert record and (tmp_path / "gpu-handoff.json").exists()
    # The stopping controller keeps the mutex: no GPU operation can start now.
    grabbed = []
    t = threading.Thread(target=lambda: grabbed.append(old["_gpu_protocol_mutex"].acquire(timeout=0.2)))
    t.start(); t.join()
    assert grabbed == [False]

    lease._fd.close(); lease._fd = None
    p2 = protocol(tmp_path)
    new = _app(tmp_path, p2)
    assert new["initialize_gpu_cutover"]() is True
    assert new["holds"] == [] and new["_gpu_cutover_ready"] is True
    assert not (tmp_path / "gpu-handoff.json").exists()
    after = p2.snapshot()["lease"]
    assert (after["state"], after["phase"]) == ("active", "parked")
    assert after["owner"] == f"media-lab-simple:{os.getpid()}"
    assert new["_gpu_active_lease"].fence == after["fence"]


@pytest.mark.parametrize("case", ["flag-off", "crash", "engine-died", "config-changed",
                                  "stale", "hold"])
def test_app_restart_without_exact_handoff_holds_as_before(tmp_path, case):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    old = _app(tmp_path, p)
    old["_gpu_active_lease"] = lease
    if case != "crash":
        assert old["_gpu_write_handoff_on_shutdown"]()
    if case == "stale":
        path = tmp_path / "gpu-handoff.json"
        rec = json.loads(path.read_text()); rec["created"] = time.time() - 3600
        path.write_text(json.dumps(rec))
    lease._fd.close(); lease._fd = None
    p2 = protocol(tmp_path)
    new = _app(tmp_path, p2,
               flag=0 if case == "flag-off" else 1,
               engine_alive=case != "engine-died",
               resident={"variant": "ref2va", "task": "t2va", "turbo_preset": None}
               if case == "config-changed" else T2VA,
               held=case == "hold")
    assert new["initialize_gpu_cutover"]() is False
    assert new["holds"] == ["durable-lease-recovery:owner-exited"]
    assert p2.snapshot()["lease"]["state"] == "recovery"
    assert not (tmp_path / "gpu-handoff.json").exists(), "a record is never left to replay"


def test_app_shutdown_writes_nothing_while_an_operation_runs(tmp_path):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    old = _app(tmp_path, p)
    old["_gpu_active_lease"] = lease
    held = threading.Event(); done = threading.Event()

    def operation():
        with old["_gpu_protocol_mutex"]:
            held.set(); done.wait(10)
    t = threading.Thread(target=operation); t.start(); held.wait(5)
    try:
        started = time.monotonic()
        assert old["_gpu_write_handoff_on_shutdown"]() is None
        assert time.monotonic() - started < 8
    finally:
        done.set(); t.join()
    assert not (tmp_path / "gpu-handoff.json").exists()


def test_app_shutdown_refuses_running_job_and_flag_off(tmp_path):
    p = protocol(tmp_path)
    lease = parked_residency(p)
    busy_app = _app(tmp_path, p, jobs={"a": {"status": "running"}})
    busy_app["_gpu_active_lease"] = lease
    assert busy_app["_gpu_write_handoff_on_shutdown"]() is None
    off = _app(tmp_path, p, flag=0)
    off["_gpu_active_lease"] = lease
    assert off["_gpu_write_handoff_on_shutdown"]() is None
    assert not (tmp_path / "gpu-handoff.json").exists()


def test_lifespan_shutdown_calls_the_handoff_before_stopping_workers():
    text = APP.read_text()
    tree = ast.parse(text)
    node = next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_studio_lifespan")
    body = ast.get_source_segment(text, node)
    assert body.index("_gpu_write_handoff_on_shutdown()") < body.index("_stop_studio_background_host()")
    init = ast.get_source_segment(text, next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "initialize_gpu_cutover"))
    assert init.index("_gpu_handoff.consume(GPU_HANDOFF)") < init.index("protocol.recover_startup()")
