"""GET /api/health: one read-only ok / warn / action answer."""
import json
import os
import time
from pathlib import Path

import pytest

from media_lab_core import studio_health as sh
from media_lab_core.durable_gpu_protocol import DurableGpuProtocol

NOW = 1_800_000_000.0


def base(**over):
    doc = {"queue": {"queued": 0, "running": 0, "stuck": False, "running_over_eta": False},
           "gpu": {"hold": {"exists": False}, "autorecover": {"gaveup": False},
                   "guard": {"state": "monitoring", "fresh": True},
                   "latch": False, "safety_stop": False, "handoff_age_s": None},
           "sol": {"configured": True, "loaded": True, "boot_cleared": True},
           "text": {"answering": True, "listed": True}, "disk_free_pct": 40.0,
           "statesnap_age_s": 600, "watchdog": {"restart_capped": False}}
    for path, value in over.items():
        target = doc
        keys = path.split(".")
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    return doc


def test_all_well_is_ok():
    assert sh.assess(base(), NOW) == ("ok", [])


@pytest.mark.parametrize("path,value,level", [
    ("gpu.hold", {"exists": True, "reason": "durable-lease-recovery:owner-exited", "age_s": 300}, "warn"),
    ("gpu.hold", {"exists": True, "reason": "durable-lease-recovery:owner-exited", "age_s": 1300}, "action"),
    ("gpu.hold", {"exists": True, "reason": "operation-uncertain:TimeoutError", "age_s": 60}, "action"),
    ("gpu.autorecover", {"gaveup": True}, "action"),
    ("gpu.safety_stop", True, "action"),
    ("gpu.latch", True, "action"),
    ("queue.stuck", True, "action"),
    ("queue.running_over_eta", True, "action"),
    ("watchdog.restart_capped", True, "action"),
    ("sol.boot_cleared", False, "action"),
    ("disk_free_pct", 6.0, "action"),
    ("disk_free_pct", 12.0, "warn"),
    ("text", {"answering": False, "listed": False}, "warn"),
    ("sol.loaded", False, "warn"),
    ("gpu.guard", {"state": None, "fresh": False}, "warn"),
    ("gpu.handoff_age_s", 3600, "warn"),
    ("statesnap_age_s", 5 * 3600, "warn"),
])
def test_levels(path, value, level):
    got, reasons = sh.assess(base(**{path: value}), NOW)
    assert got == level and reasons


def test_cold_load_guard_trip_is_a_warning_until_auto_recover_had_its_chance():
    trip = {"exists": True, "reason": "operation-uncertain:RuntimeError", "age_s": 900}
    doc = base(**{"gpu.hold": trip, "gpu.safety_stop": True, "gpu.latch": True,
                  "gpu.safety_stop_reason": "memory-psi"})
    level, reasons = sh.assess(doc, NOW)
    assert level == "warn" and len(reasons) == 1 and "memory-psi" in reasons[0]
    doc["gpu"]["hold"] = {**trip, "age_s": 41 * 60}
    assert sh.assess(doc, NOW)[0] == "action"
    doc["gpu"]["hold"] = trip
    doc["gpu"]["safety_stop_reason"] = "generation_failed"      # a render failure: a person looks
    assert sh.assess(doc, NOW)[0] == "action"


def test_collect_reads_files_and_the_lease_read_only(tmp_path, monkeypatch):
    root = tmp_path / "lab"
    pool = root / "pool"; pool.mkdir(parents=True)
    runtime = tmp_path / "run"; runtime.mkdir()
    sol = tmp_path / "sol"; sol.mkdir()
    (sol / "boot-clearance.json").write_text(json.dumps({"approved": True, "boot_id": "boot-a"}))
    (runtime / "solh3-control-plane-guard.json").write_text(json.dumps(
        {"state": "monitoring", "boot_id": "boot-a", "written_at": NOW - 2}))
    (pool / "gpu-recovery-hold.json").write_text(json.dumps(
        {"reason": "durable-lease-recovery:owner-exited", "job_id": None, "created": NOW - 1500}))
    (root / "deployed-source.json").write_text(json.dumps({"tag": "deploy-x", "commit": "abc"}))
    (root / "backups").mkdir()
    snap = root / "backups" / "storyboards.json.20260926-01"; snap.write_text("{}")
    os.utime(snap, (NOW - 120, NOW - 120))
    p = DurableGpuProtocol(pool / "gpu-lease.sqlite3", tmp_path / "gpu.lock",
                           boot_id=lambda: "boot-a", available_gib=lambda: 100.0,
                           pid_alive=lambda pid: False)
    p.qualify("h3", "t2va", peak_gib=10, warm_render_gib=2, reserve_gib=2, evidence="x")
    lease = p.acquire(job_id="idle-restore-h3", engine="h3", task="t2va", owner="old")
    lease._fd.close()
    p.recover_startup()
    before = (pool / "gpu-lease.sqlite3").stat().st_mtime_ns
    monkeypatch.setattr(sh, "text_models", lambda url, timeout=2.0: ["media-lab-text"])
    jobs = {"q1": {"status": "queued", "added": NOW - 1200}}
    doc = sh.collect(root=root, now=NOW, boot_id="boot-a", jobs=jobs, queue_ids=["q1"],
                     sol_root=sol, sol_configured=True, runtime_dir=runtime,
                     sol_state=lambda: {"loaded": True, "task": "t2va", "variant": "fl2va"},
                     restarts=lambda: 0)
    assert (pool / "gpu-lease.sqlite3").stat().st_mtime_ns == before
    assert doc["level"] == "action"
    assert doc["gpu"]["lease"]["reason"] == "owner-exited"
    assert doc["gpu"]["hold"]["age_s"] == 1500
    assert doc["queue"]["queued"] == 1 and doc["queue"]["stuck"] is False  # held, not stuck
    assert doc["sol"]["boot_cleared"] is True and doc["gpu"]["guard"]["fresh"] is True
    assert doc["deployed"] == {"tag": "deploy-x", "commit": "abc"}
    assert doc["statesnap_age_s"] == 120
    assert any("recovery hold" in r for r in doc["reasons"])


def test_the_route_is_behind_the_family_door(tmp_path, monkeypatch):
    text = (Path(__file__).parents[1] / "app.py").read_text()
    assert '@app.get("/api/health")' in text
    import app as studio
    assert "/api/health" not in studio.GATE_EXEMPT
