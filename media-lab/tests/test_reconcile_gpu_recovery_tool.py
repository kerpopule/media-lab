import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

TOOL = Path(__file__).parents[1] / "tools" / "reconcile-gpu-recovery.py"


def test_recovery_tool_is_exact_proof_gated_and_restart_safe():
    text = TOOL.read_text()
    ast.parse(text)
    assert 'lease.job_id != args.job_id' in text
    assert 'protocol.recover_startup()' in text
    assert 'contextlib.redirect_stdout(io.StringIO())' in text
    assert 'sys.path.insert(0, str(ROOT))' in text
    assert 'marker.get("job_id") != args.job_id' in text
    assert 'proof.get("processes_gone") is not True' in text
    assert 'proof.get("memory_recovered") is not True' in text
    assert text.index('recovery_disposition="terminal-error-no-auto-retry"') < text.index(
        'app.GPU_RECOVERY_HOLD.unlink()'
    ) < text.index('app.gpu_protocol().reconcile(lease, proof=proof)')
    assert 'finally:' in text
    assert 'systemctl("start", SERVICE' in text


def test_recovery_tool_can_reconcile_exact_orphan_job_only_after_full_reclaim():
    text = TOOL.read_text()
    ast.parse(text)
    assert 'if lease is None:' in text
    assert 'recovery marker exists without a durable lease' in text
    assert 'orphan_recovery_job' in text
    assert text.index('proof = app._gpu_reclaim_all(job)') < text.index(
        'for key in ("recovery_required", "recovery_reason")'
    )
    assert 'DELETE FROM' not in text and 'UPDATE gpu_lease' not in text


def test_recovery_tool_accepts_only_exact_terminal_parked_restart_hold():
    text = TOOL.read_text()
    ast.parse(text)
    assert 'terminal_parked_recovery' in text
    assert 'lease.phase == "parked"' in text
    assert 'job.get("status") in ("done", "error", "cancelled")' in text
    assert 'marker.get("job_id") is None' in text
    assert 'and not terminal_parked_recovery' in text
    assert 'durable-lease-recovery:' in text


def test_recovery_tool_accepts_exact_internal_residency_lease_without_queue_job():
    text = TOOL.read_text()
    ast.parse(text)
    assert 'internal_residency_recovery' in text
    assert 'lease.job_id.startswith("internal-")' in text
    assert 'marker.get("job_id") == job_id' in text
    assert 'and not internal_residency_recovery' in text
    assert 'app.reap_orphan_maestro_runners()' in text
    assert text.index('proof = app._gpu_reclaim_all(job)') < text.index(
        'app.gpu_protocol().reconcile(lease, proof=proof)'
    )


def test_internal_residency_marker_matches_jobless_startup_quarantine():
    spec = importlib.util.spec_from_file_location("reconcile_gpu_recovery", TOOL)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    lease = SimpleNamespace(
        state="recovery", job_id="internal-4321",
    )
    marker = {
        "job_id": None,
        "reason": "durable-lease-recovery:operation-uncertain:RuntimeError",
    }
    assert module.internal_residency_marker_matches(
        lease, {"reason": "operation-uncertain:RuntimeError"},
        None, marker, "internal-4321"
    ) is True


def test_main_reconciles_exact_jobless_internal_startup_hold(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("reconcile_gpu_recovery_main", TOOL)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    lease = SimpleNamespace(
        state="recovery", phase="unload", job_id="internal-4321",
        engine="h3", task="t2va",
    )
    reconciled = []

    class Protocol:
        def recover_startup(self):
            return lease

        def snapshot(self):
            return {"lease": {"reason": "owner-exited"}}

        def reconcile(self, current, proof):
            reconciled.append((current, proof))

    hold = tmp_path / "gpu-recovery-hold.json"
    hold.write_text('{"job_id":null,"reason":"durable-lease-recovery:owner-exited"}')
    fake_app = SimpleNamespace(
        _gpu_active_lease=None,
        GPU_RECOVERY_HOLD=hold,
        JOBS_FILE=tmp_path / "jobs.json",
        jobs={},
        gpu_protocol=lambda: Protocol(),
        _gpu_reclaim_all=lambda _job: {
            "processes_gone": True, "memory_recovered": True,
            "available_gib": 120.0, "survivors": [], "boot_id": "boot",
        },
        pool_cmd=lambda _action: "OK",
        save_state=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "configured_home", lambda: tmp_path)
    monkeypatch.setattr(module, "systemctl", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stderr=""
    ))
    monkeypatch.setattr(sys, "argv", [str(TOOL), "--job-id", "internal-4321"])

    assert module.main() == 0
    assert reconciled and reconciled[0][0] is lease
    assert reconciled[0][1]["processes_gone"] is True
    assert not hold.exists()


def _run_interrupted_boot_recovery(tmp_path, monkeypatch, *,
                                   marker_job_id=None, marker_reason="durable-lease-recovery:boot-changed",
                                   durable_reason="boot-changed", durable_fence=34,
                                   persisted_url=None, lock_owned=True, processes_gone=True,
                                   memory_recovered=None):
    spec = importlib.util.spec_from_file_location("reconcile_gpu_interrupted", TOOL)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    job_id = "173391b2f0b4"
    persisted = {
        "id": job_id, "kind": "video", "status": "running", "progress": 5,
        "stage": "starting H3 / T2VA…", "engine": "h3", "request": {"model": "h3-ltx25"},
    }
    if persisted_url is not None:
        persisted["url"] = persisted_url
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": {job_id: persisted}, "queue": []}))
    live_job = dict(persisted)
    live_job.update(status="queued", stage="queued", message=None, auto_retries=1)
    lease = SimpleNamespace(
        fence=34, owner="media-lab-simple:123", pid=123, boot_id="old-boot",
        state="recovery", phase="load", job_id=job_id, engine="h3", task="t2va",
        _fd=object() if lock_owned else None,
    )
    durable = {
        "fence": durable_fence, "owner": lease.owner, "pid": 123, "boot_id": "old-boot",
        "state": "recovery", "phase": "load", "job_id": job_id, "engine": "h3",
        "task": "t2va", "reason": durable_reason,
    }
    reconciled = []

    class Protocol:
        def recover_startup(self):
            return lease

        def snapshot(self):
            return {"lease": durable if not reconciled else None}

        def reconcile(self, current, proof):
            assert current is lease
            assert proof["boot_id"] == "new-boot"
            reconciled.append((current, proof))

    hold = tmp_path / "gpu-recovery-hold.json"
    hold.write_text(json.dumps({"job_id": marker_job_id, "reason": marker_reason}))
    saves = []
    fake_app = SimpleNamespace(
        _gpu_active_lease=None, GPU_RECOVERY_HOLD=hold, JOBS_FILE=tmp_path / "jobs.json",
        jobs={job_id: live_job},
        queue=[job_id], online_queue=[], gpu_protocol=lambda: Protocol(),
        _gpu_reclaim_all=lambda _job: {
            "processes_gone": processes_gone,
            "memory_recovered": (processes_gone if memory_recovered is None else memory_recovered),
            "available_gib": 118.0, "survivors": [] if processes_gone else ["h3"],
            "boot_id": "new-boot",
        },
        pool_cmd=lambda _action: "OK",
        save_state=lambda: saves.append({"job": dict(live_job), "queue": list(fake_app.queue)}),
    )
    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "configured_home", lambda: tmp_path)
    monkeypatch.setattr(module, "systemctl", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stderr=""
    ))
    monkeypatch.setattr(sys, "argv", [str(TOOL), "--job-id", job_id])
    return module, fake_app, live_job, hold, reconciled, saves


def test_exact_interrupted_running_boot_recovery_becomes_terminal_without_retry(tmp_path, monkeypatch):
    module, app, job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch
    )

    assert module.main() == 0
    assert reconciled
    assert job["status"] == "error"
    assert job["stage"] == "error"
    assert job["retryable"] is False
    assert "reboot" in job["message"].lower()
    assert "173391b2f0b4" not in app.queue
    assert len(saves) == 2
    assert saves[0]["job"]["recovery_disposition"] == "terminal-error-no-auto-retry"
    assert saves[0]["job"]["recovery_required"] is True
    assert saves[1]["job"]["status"] == "error"
    assert "173391b2f0b4" in saves[1]["queue"]
    assert not hold.exists()


def test_interrupted_running_recovery_rejects_mismatched_or_ambiguous_marker(tmp_path, monkeypatch):
    module, _app, _job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch, marker_reason="durable-lease-recovery:owner-exited"
    )

    try:
        module.main()
    except RuntimeError as exc:
        assert "neither recovery_required" in str(exc)
    else:
        raise AssertionError("mismatched marker was accepted")
    assert not reconciled and not saves and hold.exists()


def test_interrupted_running_recovery_rejects_surviving_lock_owner(tmp_path, monkeypatch):
    module, _app, _job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch, lock_owned=False
    )

    try:
        module.main()
    except RuntimeError as exc:
        assert "exclusion lock" in str(exc)
    else:
        raise AssertionError("active lock owner was accepted")
    assert not reconciled and not saves and hold.exists()


def test_interrupted_running_recovery_rejects_surviving_managed_process(tmp_path, monkeypatch):
    module, _app, _job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch, processes_gone=False
    )

    try:
        module.main()
    except RuntimeError as exc:
        assert "managed GPU processes survived" in str(exc)
    else:
        raise AssertionError("surviving managed process was accepted")
    assert not reconciled and not saves and hold.exists()


def test_interrupted_running_recovery_rejects_mismatched_fence(tmp_path, monkeypatch):
    module, _app, _job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch, durable_fence=35
    )

    try:
        module.main()
    except RuntimeError as exc:
        assert "neither recovery_required" in str(exc)
    else:
        raise AssertionError("mismatched durable fence was accepted")
    assert not reconciled and not saves and hold.exists()


def test_interrupted_running_recovery_rejects_configured_jobs_path_drift(tmp_path, monkeypatch):
    module, app, _job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch
    )
    app.JOBS_FILE = tmp_path / "other-home" / "jobs.json"

    try:
        module.main()
    except RuntimeError as exc:
        assert "configured jobs path changed" in str(exc)
    else:
        raise AssertionError("configured jobs path drift was accepted")
    assert not reconciled and not saves and hold.exists()


def test_interrupted_running_recovery_rejects_existing_output(tmp_path, monkeypatch):
    module, _app, _job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch, persisted_url="/media/already-exists.mp4"
    )

    try:
        module.main()
    except RuntimeError as exc:
        assert "neither recovery_required" in str(exc)
    else:
        raise AssertionError("interrupted job with output was accepted")
    assert not reconciled and not saves and hold.exists()


def test_interrupted_running_recovery_rejects_unrecovered_memory(tmp_path, monkeypatch):
    module, _app, _job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch, memory_recovered=False
    )

    try:
        module.main()
    except RuntimeError as exc:
        assert "memory did not recover" in str(exc)
    else:
        raise AssertionError("unrecovered memory was accepted")
    assert not reconciled and not saves and hold.exists()


def _checkpoint_interrupted_transition(tmp_path, job, hold):
    job.update(
        status="queued", stage="queued", recovery_required=True,
        recovery_reason="interrupted-running-boot-recovery",
        recovery_disposition="terminal-error-no-auto-retry",
    )
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": {job["id"]: job}, "queue": [job["id"]]}))
    hold.unlink()


def test_interrupted_transition_resumes_after_marker_removal_before_lease_reconcile(
        tmp_path, monkeypatch):
    module, app, job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch
    )
    _checkpoint_interrupted_transition(tmp_path, job, hold)

    assert module.main() == 0
    assert reconciled
    assert len(saves) == 1
    assert saves[0]["job"]["status"] == "error"
    assert saves[0]["job"]["retryable"] is False
    assert job["id"] in saves[0]["queue"]
    assert job["id"] not in app.queue


def test_interrupted_transition_resumes_after_lease_and_marker_are_cleared(
        tmp_path, monkeypatch):
    module, app, job, hold, reconciled, saves = _run_interrupted_boot_recovery(
        tmp_path, monkeypatch
    )
    _checkpoint_interrupted_transition(tmp_path, job, hold)

    class OrphanProtocol:
        def recover_startup(self):
            return None

        def snapshot(self):
            return {"lease": None}

        def reconcile(self, *_args, **_kwargs):
            raise AssertionError("an already-cleared lease must not be reconciled again")

    app._gpu_active_lease = None
    app.gpu_protocol = lambda: OrphanProtocol()

    assert module.main() == 0
    assert not reconciled
    assert len(saves) == 1
    assert saves[0]["job"]["status"] == "error"
    assert saves[0]["job"]["retryable"] is False
    assert job["id"] in saves[0]["queue"]
    assert job["id"] not in app.queue
