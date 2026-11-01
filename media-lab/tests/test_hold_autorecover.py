"""The hold auto-recoverer acts only on the harmless restart hold, within limits."""
import json
import os
import time
from pathlib import Path

import pytest

from media_lab_core.durable_gpu_protocol import DurableGpuProtocol
from runner import hold_autorecover as har

NOW = 1_800_000_000.0


def facts(**over):
    base = {
        "now": NOW, "boot_id": "boot-a", "hold_exists": True,
        "hold": {"reason": "durable-lease-recovery:owner-exited", "job_id": None,
                 "created": NOW - 900},
        "lease": {"state": "recovery", "reason": "owner-exited", "boot_id": "boot-a",
                  "job_id": "idle-restore-h3", "phase": "parked", "fence": 7, "pid": 1},
        "lease_job_status": None, "persisted_running": 0, "studio_running": 0,
        "baton": False, "engine_maintenance": False, "safety_stop": False, "latch": False,
        "guard_state": "monitoring", "guard_fresh": True, "owner_is_controller": False,
        "mem_available_gib": 20.0,
    }
    for key, value in over.items():
        if key.startswith("lease_") and key != "lease_job_status":
            base["lease"] = {**base["lease"], key[6:]: value}
        elif key.startswith("hold_") and key != "hold_exists":
            base["hold"] = {**base["hold"], key[5:]: value}
        else:
            base[key] = value
    return base


def decide(f, state=None, **kw):
    return har.decide(f, state or {}, enabled=kw.pop("enabled", True),
                      gaveup=kw.pop("gaveup", False), **kw)


def test_harmless_idle_restart_hold_is_reconciled():
    assert decide(facts())[0] == "reconcile"
    assert decide(facts(lease_reason="controller-restarted",
                        hold_reason="durable-lease-recovery:controller-restarted"))[0] == "reconcile"
    finished = facts(lease_job_id="abc123", lease_job_status="done")
    assert decide(finished)[0] == "reconcile"


@pytest.mark.parametrize("over,action", [
    ({"hold_exists": False}, "idle"),
    ({"lease_reason": "operation-uncertain:TimeoutError"}, "skip"),
    ({"lease_reason": "boot-changed"}, "skip"),
    ({"lease_reason": "capacity-rejected-before-safe-release"}, "skip"),
    ({"hold_reason": "operation-finished-without-exact-idle-residency"}, "skip"),
    ({"hold_job_id": "abc123"}, "skip"),                       # a job-bound hold
    ({"lease_boot_id": "boot-old"}, "skip"),                   # another boot
    ({"lease_state": "active"}, "skip"),
    ({"lease": None}, "skip"),
    ({"hold": None}, "skip"),
    ({"lease_job_id": "abc123", "lease_job_status": "queued"}, "skip"),
    ({"lease_job_id": "abc123", "lease_job_status": "done", "lease_phase": "render"}, "skip"),
    ({"hold_created": NOW - 60}, "wait"),                      # a person gets 10 min
    ({"studio_running": 1}, "wait"),
    ({"persisted_running": 1}, "wait"),
    ({"studio_running": None}, "wait"),
    ({"baton": True}, "skip"),
    ({"engine_maintenance": True}, "skip"),
    ({"safety_stop": True}, "skip"),
    ({"latch": True}, "skip"),
    ({"guard_state": "tripped"}, "skip"),
    ({"guard_fresh": False}, "skip"),
    ({"owner_is_controller": True}, "skip"),
    ({"mem_available_gib": 2.0}, "wait"),
    ({"mem_available_gib": None}, "wait"),
])
def test_everything_else_is_left_alone(over, action):
    assert decide(facts(**over))[0] == action


def test_flag_off_and_gave_up_never_act():
    assert decide(facts(), enabled=False)[0] == "skip"
    assert decide(facts(), gaveup=True)[0] == "skip"


def test_limits_one_try_per_hold_daily_cap_backoff_and_give_up():
    f = facts()
    key = har.hold_key(f)
    assert decide(f, {"attempts": [{"ts": NOW - 400, "hold": key, "ok": False}]})[0] == "giveup"
    three = [{"ts": NOW - 3600 * i, "hold": f"other-{i}", "ok": True} for i in (1, 2, 3)]
    assert decide(f, {"attempts": three})[0] == "giveup"
    old = [{"ts": NOW - 90000, "hold": "old", "ok": True}] * 3
    assert decide(f, {"attempts": old})[0] == "reconcile"
    one_fail = {"attempts": [{"ts": NOW - 300, "hold": "x", "ok": False}], "consecutive_failures": 1}
    assert decide(f, one_fail)[0] == "wait"                      # 10 min backoff
    one_fail["attempts"][0]["ts"] = NOW - 700
    assert decide(f, one_fail)[0] == "reconcile"
    assert decide(f, {"attempts": [], "consecutive_failures": 2})[0] == "giveup"


# ---------------------------------------------------------------- whole pass

class FakePaths(har.Paths):
    def __init__(self, root: Path):
        super().__init__(root=root, runtime=root / "run", sol_root=root / "sol")
        self.pool.mkdir(parents=True, exist_ok=True)


def _enable(monkeypatch, on=True):
    monkeypatch.setenv(har.FLAG, "1" if on else "0")


def test_pass_reconciles_once_then_gives_up_if_hold_remains(tmp_path, monkeypatch):
    _enable(monkeypatch)
    paths = FakePaths(tmp_path)
    calls = []

    def fake_reconcile(p, job_id):
        calls.append(job_id)
        return False, {"rc": 1, "error": "memory did not recover"}

    first = har.main([], paths=paths, reconcile=fake_reconcile, facts=facts())
    assert first["action"] == "reconcile" and first["ok"] is False and calls == ["idle-restore-h3"]
    second = har.main([], paths=paths, reconcile=fake_reconcile, facts=facts(now=NOW + 700))
    assert second["action"] == "giveup" and calls == ["idle-restore-h3"]
    gave = json.loads(paths.gaveup.read_text())
    assert "reconcile-gpu-recovery.py" in gave["next_step"]
    third = har.main([], paths=paths, reconcile=fake_reconcile, facts=facts(now=NOW + 1400))
    assert third["action"] == "skip" and calls == ["idle-restore-h3"]
    lines = [json.loads(l) for l in paths.log.read_text().splitlines()]
    assert [l["action"] for l in lines] == ["reconcile", "giveup", "skip"]


def test_pass_success_resets_failures(tmp_path, monkeypatch):
    _enable(monkeypatch)
    paths = FakePaths(tmp_path)
    out = har.main([], paths=paths, reconcile=lambda p, j: (True, {"rc": 0}), facts=facts())
    assert out["ok"] is True
    state = json.loads(paths.state.read_text())
    assert state["consecutive_failures"] == 0 and len(state["attempts"]) == 1
    assert not paths.gaveup.exists()


def test_dry_run_and_flag_off_change_nothing(tmp_path, monkeypatch):
    paths = FakePaths(tmp_path)
    boom = lambda p, j: pytest.fail("must not reconcile")
    _enable(monkeypatch, on=False)
    assert har.main([], paths=paths, reconcile=boom, facts=facts())["action"] == "skip"
    _enable(monkeypatch)
    assert har.main(["--dry-run"], paths=paths, reconcile=boom, facts=facts())["action"] == "reconcile"
    assert not paths.gaveup.exists()
    assert not (paths.state.exists() and json.loads(paths.state.read_text()).get("attempts"))


def test_gather_reads_the_real_lease_database_read_only(tmp_path, monkeypatch):
    paths = FakePaths(tmp_path)
    boot = Path("/proc/sys/kernel/random/boot_id")
    boot_id = boot.read_text().strip() if boot.exists() else ""
    p = DurableGpuProtocol(paths.db, tmp_path / "gpu.lock", boot_id=lambda: boot_id,
                           available_gib=lambda: 120.0, pid_alive=lambda pid: False)
    p.qualify("h3", "t2va", peak_gib=10, warm_render_gib=2, reserve_gib=2, evidence="x")
    lease = p.acquire(job_id="idle-restore-h3", engine="h3", task="t2va", owner="old")
    for phase in ("unload", "reclaim", "load", "render"):
        p.advance(lease, phase)
    p.bind_process(lease, pid=4242, identity="x")
    p.park(lease, proof={"engine": "h3", "task": "t2va", "healthy": True, "busy": False})
    lease._fd.close()
    p.recover_startup()
    paths.hold.write_text(json.dumps({"reason": "durable-lease-recovery:owner-exited",
                                      "job_id": None, "created": time.time() - 900}))
    before = paths.db.stat().st_mtime_ns
    monkeypatch.setattr(har, "studio_running_jobs", lambda timeout=15.0: 0)
    f = har.gather(paths)
    assert f["lease"]["reason"] == "owner-exited" and f["lease"]["job_id"] == "idle-restore-h3"
    assert f["hold_exists"] and f["studio_running"] == 0
    assert paths.db.stat().st_mtime_ns == before


def test_timer_and_service_are_shipped():
    root = Path(__file__).parents[1] / "config"
    service = (root / "media-lab-hold-autorecover.service").read_text()
    timer = (root / "media-lab-hold-autorecover.timer").read_text()
    assert "runner/hold_autorecover.py" in service and "Type=oneshot" in service
    assert "OnUnitActiveSec=5min" in timer


def test_removing_the_give_up_flag_resets_the_failure_count(tmp_path, monkeypatch):
    _enable(monkeypatch)
    paths = FakePaths(tmp_path)
    fail = lambda p, j: (False, {"rc": 1})
    har.main([], paths=paths, reconcile=fail, facts=facts())
    har.main([], paths=paths, reconcile=fail, facts=facts(hold_created=NOW - 800, now=NOW + 5000))
    assert json.loads(paths.state.read_text())["consecutive_failures"] == 2
    assert paths.gaveup.exists()
    paths.gaveup.unlink()                      # a person looked and cleared it
    calls = []
    ok = lambda p, j: calls.append(j) or (True, {"rc": 0})
    out = har.main([], paths=paths, reconcile=ok, facts=facts(hold_created=NOW - 700, now=NOW + 20000))
    assert out["action"] == "reconcile" and calls == ["idle-restore-h3"]
    assert json.loads(paths.state.read_text())["consecutive_failures"] == 0


# ------------------------------------------- guard trip during an H3 cold load
# Modelled on 2026-09-26 02:46: an fl2va cold load for job 47dc63a7064e hit
# PSI 52.45 > 50; the guard stopped H3; the studio held operation-uncertain.

TRIP_AT = NOW - 1800


def trip_facts(**over):
    base = facts(
        hold={"reason": "operation-uncertain:RuntimeError", "job_id": "47dc63a7064e",
              "created": TRIP_AT + 10},
        lease={"state": "recovery", "reason": "operation-uncertain:RuntimeError",
               "boot_id": "boot-a", "job_id": "47dc63a7064e", "phase": "load",
               "engine": "h3", "task": "fl2va", "fence": 9, "pid": 1},
        safety_stop=True, latch=True, guard_state="quarantined",
        mem_available_gib=117.0,
    )
    base.update({
        "safety_record": {"reason": "memory-psi", "unit": "media-lab-sol-h3.service",
                          "boot_id": "boot-a", "incident_id": "inc-1", "time": TRIP_AT},
        "incident_status": "terminated", "latch_text": "inc-1",
        "last_load": {"outcome": "guard-lost", "task": "fl2va",
                      "started_at": TRIP_AT - 270, "load_s": 273.0},
        "trip_job": {"status": "error", "recovery_required": True},
        "h3_active": False, "psi_full_avg60": 0.1, "kernel_trouble": 0,
    })
    for key, value in over.items():
        if key.startswith("lease_") and key != "lease_job_status":
            base["lease"] = {**base["lease"], key[6:]: value}
        elif key.startswith("hold_") and key != "hold_exists":
            base["hold"] = {**base["hold"], key[5:]: value}
        elif key.startswith("stop_"):
            base["safety_record"] = {**base["safety_record"], key[5:]: value}
        elif key.startswith("load_"):
            base["last_load"] = {**base["last_load"], key[5:]: value}
        elif key.startswith("job_"):
            base["trip_job"] = {**base["trip_job"], key[4:]: value}
        else:
            base[key] = value
    return base


def test_guard_trip_during_cold_load_is_reconciled_once_memory_recovered():
    action, why = decide(trip_facts())
    assert action == "reconcile-trip", why
    assert "memory-psi" in why
    # also an idle-restore load (no job) and the other guard reasons
    assert decide(trip_facts(lease_job_id="idle-restore-h3", hold_job_id="idle-restore-h3",
                             trip_job=None))[0] == "reconcile-trip"
    assert decide(trip_facts(stop_reason="swap-growth"))[0] == "reconcile-trip"
    assert decide(trip_facts(latch=False, latch_text=None))[0] == "reconcile-trip"


@pytest.mark.parametrize("over,action", [
    ({"safety_record": None}, "skip"),
    ({"stop_reason": "generation_failed"}, "skip"),            # a render failure, not the guard
    ({"stop_unit": "other.service"}, "skip"),
    ({"stop_boot_id": "boot-old"}, "skip"),
    ({"lease_boot_id": "boot-old"}, "skip"),
    ({"incident_status": "survivors"}, "skip"),                # H3 cgroup not fully gone
    ({"incident_status": None}, "skip"),
    ({"latch_text": "inc-other"}, "skip"),
    ({"lease_phase": "render"}, "skip"),                       # tripped mid-render: not benign
    ({"lease_engine": "ltx"}, "skip"),
    ({"hold_reason": "operation-uncertain:TimeoutError"}, "skip"),
    ({"load_outcome": "ready"}, "skip"),
    ({"load_task": "t2va"}, "skip"),
    ({"load_started_at": TRIP_AT + 600}, "skip"),              # trip not inside that load
    ({"last_load": None}, "skip"),
    ({"hold_job_id": "someone-else"}, "skip"),
    ({"job_status": "queued"}, "skip"),                        # a clear would re-run it
    ({"job_video_url": "/media/x.mp4"}, "skip"),               # it produced output
    ({"trip_job": None}, "skip"),                              # unknown real job
    ({"hold_created": NOW - 300}, "wait"),                     # 15 min first
    ({"stop_time": NOW - 300, "load_started_at": NOW - 500}, "wait"),  # trip too recent
    ({"studio_running": 1}, "wait"),
    ({"persisted_running": 1}, "wait"),
    ({"studio_running": None}, "wait"),
    ({"baton": True}, "skip"),
    ({"engine_maintenance": True}, "skip"),
    ({"guard_state": "ready"}, "skip"),                        # markers vs guard disagree
    ({"guard_fresh": False}, "skip"),
    ({"h3_active": True}, "skip"),
    ({"h3_active": None}, "skip"),
    ({"owner_is_controller": True}, "skip"),
    ({"mem_available_gib": 60.0}, "wait"),                     # not recovered yet
    ({"mem_available_gib": None}, "wait"),
    ({"psi_full_avg60": 8.0}, "wait"),
    ({"psi_full_avg60": None}, "wait"),
    ({"kernel_trouble": 2}, "skip"),                           # Xid / hung task / OOM
    ({"kernel_trouble": None}, "skip"),
])
def test_guard_trip_refusals(over, action):
    assert decide(trip_facts(**over))[0] == action


def test_other_operation_uncertain_holds_are_still_left_alone():
    # No safety stop: an operation-uncertain hold from a render failure.
    assert decide(trip_facts(safety_stop=False, latch=False))[0] == "skip"


def test_only_one_guard_trip_clear_per_day():
    f = trip_facts()
    earlier = {"attempts": [{"ts": NOW - 3600, "hold": "other", "ok": True, "kind": "guard-trip"}]}
    assert decide(f, earlier) == ("giveup", "a second guard trip within 24 h: a person must look")
    restart = {"attempts": [{"ts": NOW - 3600, "hold": "other", "ok": True, "kind": "restart"}]}
    assert decide(f, restart)[0] == "reconcile-trip"
    old = {"attempts": [{"ts": NOW - 90000, "hold": "other", "ok": True, "kind": "guard-trip"}]}
    assert decide(f, old)[0] == "reconcile-trip"


def _trip_files(paths):
    paths.sol_root.mkdir(parents=True, exist_ok=True)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    paths.incidents.mkdir(parents=True, exist_ok=True)
    paths.safety_stop.write_text(json.dumps({"reason": "memory-psi", "incident_id": "inc-1"}))
    paths.latch.write_text("inc-1\n")
    (paths.incidents / "inc-1.json").write_text(json.dumps({"status": "terminated"}))
    paths.hold.write_text(json.dumps({"reason": "operation-uncertain:RuntimeError"}))


def test_trip_pass_sets_markers_aside_keeps_evidence_and_reconciles(tmp_path, monkeypatch):
    _enable(monkeypatch)
    paths = FakePaths(tmp_path)
    _trip_files(paths)
    seen = {}

    def fake_reconcile(p, job_id):
        seen["job"] = job_id
        seen["markers_gone"] = not p.safety_stop.exists() and not p.latch.exists()
        return True, {"rc": 0}

    set_aside = lambda p, f: har.set_aside_trip_markers(p, f, sleep=lambda _s: None,
                                                        guard_state=lambda _p: "ready")
    out = har.main([], paths=paths, reconcile=fake_reconcile, facts=trip_facts(),
                   set_aside=set_aside)
    assert out["action"] == "reconcile-trip" and out["ok"] is True
    assert seen == {"job": "47dc63a7064e", "markers_gone": True}
    evidence = Path(out["evidence"])
    assert (evidence / "safety-stop.json").exists() and (evidence / "flashnext-memwatch.latch").exists()
    assert (evidence / "inc-1.json").exists() and (evidence / "gpu-recovery-hold.json").exists()
    state = json.loads(paths.state.read_text())
    assert state["attempts"][-1]["kind"] == "guard-trip"


def test_trip_pass_puts_markers_back_when_reconcile_fails(tmp_path, monkeypatch):
    _enable(monkeypatch)
    paths = FakePaths(tmp_path)
    _trip_files(paths)
    set_aside = lambda p, f: har.set_aside_trip_markers(p, f, sleep=lambda _s: None,
                                                        guard_state=lambda _p: "ready")
    out = har.main([], paths=paths, reconcile=lambda p, j: (False, {"rc": 1}),
                   facts=trip_facts(), set_aside=set_aside)
    assert out["ok"] is False and out["receipt"]["markers_restored"] is True
    assert paths.safety_stop.exists() and paths.latch.read_text().strip() == "inc-1"


def test_trip_markers_go_back_if_the_guard_never_reports_ready(tmp_path, monkeypatch):
    _enable(monkeypatch)
    paths = FakePaths(tmp_path)
    _trip_files(paths)
    monkeypatch.setattr(har, "GUARD_READY_WAIT_S", 0.0)
    boom = lambda p, j: pytest.fail("must not reconcile")
    set_aside = lambda p, f: har.set_aside_trip_markers(p, f, sleep=lambda _s: None,
                                                        guard_state=lambda _p: "quarantined")
    out = har.main([], paths=paths, reconcile=boom, facts=trip_facts(), set_aside=set_aside)
    assert out["ok"] is False and "guard did not report ready" in out["receipt"]["error"]
    assert paths.safety_stop.exists() and paths.latch.exists()


def test_kernel_trouble_counts_only_bad_lines(monkeypatch):
    class R:
        returncode = 0
        stdout = ("usb 1-1: new device\nNVRM: Xid (PCI:0000:01:00): 79\n"
                  "INFO: task x blocked for more than 120 seconds\n"
                  "NVRM: nvCheckOkFailedNoLog: Check failed: Out of memory [NV_ERR_NO_MEMORY]\n")
    monkeypatch.setattr(har.subprocess, "run", lambda *a, **k: R())
    assert har.kernel_trouble_since(NOW) == 2
    R.returncode = 1
    assert har.kernel_trouble_since(NOW) is None


def test_last_load_reads_the_newest_record(tmp_path):
    log = tmp_path / "h3-load-pressure.jsonl"
    log.write_text(json.dumps({"outcome": "ready"}) + "\n" + json.dumps({"outcome": "guard-lost"}) + "\n")
    assert har._last_load(log)["outcome"] == "guard-lost"
    assert har._last_load(tmp_path / "missing") is None


# ------------------------------------ page-cache trim for an idle H3 restore
# 2026-09-26 20:38-20:48: after a clean recovery the idle restore was refused
# every minute: "h3 decode requires 117.0 GiB including the 2.0 GiB floor;
# 116.9 GiB would be available". Dropping the clean page cache fixed it.

def trim_facts(**over):
    base = {"now": NOW, "hold_exists": False, "hold": None, "lease": None,
            "h3_shortfall_gib": 0.1, "studio_active": (0, 0), "persisted_running": 0,
            "h3_active": False, "cached_gib": 24.5, "safety_stop": False, "latch": False,
            "baton": False, "engine_maintenance": False}
    base.update(over)
    return base


def test_degraded_line_is_parsed_and_success_wins(monkeypatch):
    class R:
        stdout = ("[residency] idle reconciliation degraded: h3 decode requires 117.0 GiB including "
                  "the 2.0 GiB floor; 116.9 GiB would be available\n")
    monkeypatch.setattr(har.subprocess, "run", lambda *a, **k: R())
    assert har.h3_restore_shortfall_gib() == 0.1
    R.stdout += "[residency] reconciled idle profile qwen-h3 (abc)\n"
    assert har.h3_restore_shortfall_gib() is None


def test_trim_when_an_idle_restore_misses_by_a_hair():
    action, why = har.decide_trim(trim_facts(), {}, enabled=True)
    assert action == "trim" and "0.1 GiB short" in why


@pytest.mark.parametrize("over,action", [
    ({"h3_shortfall_gib": None}, "idle"),
    ({"h3_shortfall_gib": 5.0}, "skip"),           # not a page-cache problem
    ({"hold_exists": True}, "skip"),
    ({"safety_stop": True}, "skip"),
    ({"latch": True}, "skip"),
    ({"baton": True}, "skip"),
    ({"engine_maintenance": True}, "skip"),
    ({"studio_active": None}, "wait"),
    ({"studio_active": (1, 0)}, "wait"),
    ({"studio_active": (0, 2)}, "wait"),
    ({"persisted_running": 1}, "wait"),
    ({"h3_active": True}, "skip"),
    ({"h3_active": None}, "skip"),
    ({"cached_gib": 0.3}, "skip"),
])
def test_trim_refusals(over, action):
    assert har.decide_trim(trim_facts(**over), {}, enabled=True)[0] == action


def test_trim_is_off_by_default_and_rate_limited():
    assert har.decide_trim(trim_facts(), {}, enabled=False)[0] == "idle"
    assert har.decide_trim(trim_facts(), {"trims": [NOW - 600]}, enabled=True)[0] == "wait"
    assert har.decide_trim(trim_facts(), {"trims": [NOW - 1900]}, enabled=True)[0] == "trim"
    many = {"trims": [NOW - 1900 - 60 * i for i in range(12)]}
    assert har.decide_trim(trim_facts(), many, enabled=True)[0] == "skip"


def test_trim_pass_runs_once_and_records(tmp_path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv(har.TRIM_FLAG, "1")
    paths = FakePaths(tmp_path)
    calls = []
    out = har.main([], paths=paths, facts=trim_facts(),
                   trim=lambda p: calls.append(1) or (True, {"rc": 0}))
    assert out["action"] == "cache-trim" and out["ok"] and calls == [1]
    again = har.main([], paths=paths, facts=trim_facts(now=NOW + 60),
                     trim=lambda p: pytest.fail("rate limited"))
    assert again["action"] == "cache-wait"
    monkeypatch.setenv(har.TRIM_FLAG, "0")
    off = har.main([], paths=paths, facts=trim_facts(now=NOW + 4000),
                   trim=lambda p: pytest.fail("flag off"))
    assert off["action"] == "idle"


def test_cli_status_probes_the_bound_address(tmp_path, monkeypatch):
    from media_lab_core import cli
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "local.env").write_text('MEDIA_LAB_BIND_HOST="100.64.0.9"\n')
    monkeypatch.delenv("MEDIA_LAB_BIND_HOST", raising=False)
    assert cli._bind_from_local_env(tmp_path) == "100.64.0.9"
    assert cli._bind_from_local_env(tmp_path / "nowhere") is None


# ------------------------------------------------ after a reboot (2026-09-27)
# Modelled on 2026-09-27 10:56: Spark 1 reset itself while idle; the warm H3
# lease of finished take 5f569bb64ce9 carried over as boot-changed, and H3
# waited for a person. Owner decision: after any reboot it must come back by itself.

def boot_facts(**over):
    base = facts(
        boot_id="boot-new", hold={"reason": har.BOOT_HOLD_REASON, "job_id": None, "created": NOW - 300},
        lease={"state": "recovery", "reason": "boot-changed", "boot_id": "boot-old",
               "job_id": "5f569bb64ce9", "engine": "h3", "task": "t2va", "phase": "parked",
               "fence": 172, "pid": 3105509},
        lease_job_status="done", sol_configured=True, boot_cleared=False, uptime_s=600.0,
        studio_active=(0, 0), studio_running=0, h3_active=False, guard_state="ready",
        mem_available_gib=117.0, psi_full_avg60=0.0, gpu_ok=True, kernel_trouble_boot=0,
        owner_is_controller=True)   # a recycled pid from the old boot must not matter
    for key, value in over.items():
        if key.startswith("lease_") and key != "lease_job_status":
            base["lease"] = {**base["lease"], key[6:]: value}
        elif key.startswith("hold_") and key != "hold_exists":
            base["hold"] = {**base["hold"], key[5:]: value}
        else:
            base[key] = value
    return base


def bdecide(f, state=None, boot=True):
    return har.decide(f, state or {}, enabled=True, gaveup=False, boot_enabled=boot)


def test_after_reboot_h3_and_the_carried_over_hold_clear_themselves():
    assert bdecide(boot_facts())[0] == "boot-clear"
    assert bdecide(boot_facts(lease_job_id="idle-restore-h3", lease_job_status=None))[0] == "boot-clear"
    # a take the reboot interrupted: the tool ends it as an error, never re-runs it
    assert bdecide(boot_facts(lease_phase="load", lease_job_status="running"))[0] == "boot-clear"
    # no hold at all, H3 just not cleared for this boot
    assert bdecide(boot_facts(hold_exists=False, hold=None, lease=None))[0] == "boot-clear"
    # an old boot's safety stop is evidence, not a reason to wait for a person
    old_stop = {"reason": "memory-psi", "boot_id": "boot-old", "incident_id": "i"}
    assert bdecide(boot_facts(safety_stop=True, safety_record=old_stop,
                              guard_state="quarantined"))[0] == "boot-clear"


@pytest.mark.parametrize("over,action", [
    ({"uptime_s": 30.0}, "wait"),
    ({"studio_active": None}, "wait"),
    ({"studio_active": (1, 0)}, "wait"),
    ({"baton": True}, "wait"),
    ({"h3_active": True}, "wait"),
    ({"guard_fresh": False}, "wait"),
    ({"guard_state": "quarantined"}, "wait"),          # quarantined with no old stop
    ({"mem_available_gib": 60.0}, "wait"),
    ({"psi_full_avg60": 9.0}, "wait"),
    ({"kernel_trouble_boot": None}, "wait"),
    ({"mem_available_gib": 60.0, "uptime_s": 2800.0}, "giveup"),   # 45 min: alert, never silent
    ({"latch": True}, "giveup"),
    ({"safety_stop": True, "safety_record": {"reason": "memory-psi", "boot_id": "boot-new"}}, "giveup"),
    ({"safety_stop": True, "safety_record": {"reason": "x"}}, "giveup"),
    ({"gpu_ok": False}, "giveup"),
    ({"kernel_trouble_boot": 2}, "giveup"),
    ({"lease_job_status": "queued", "lease_phase": "parked", "lease_job_id": "j"}, "boot-clear"),
    ({"lease_job_status": None, "lease_job_id": "abc"}, "giveup"),     # not exactly reconcilable
    ({"hold_job_id": "someone-else"}, "giveup"),
    ({"lease_boot_id": "boot-new"}, "giveup"),
])
def test_after_reboot_checks(over, action):
    assert bdecide(boot_facts(**over))[0] == action


def test_after_reboot_flag_off_keeps_the_old_rule():
    assert bdecide(boot_facts(), boot=False) == (
        "skip", f"{har.BOOT_FLAG} is off; H3 waits for spark1-clear-h3")
    assert bdecide(boot_facts(hold_exists=False, hold=None, lease=None, boot_cleared=True))[0] == "idle"


def test_after_reboot_limits_retries_backoff_and_reboot_loop():
    f = boot_facts()
    one = {"attempts": [{"ts": NOW - 60, "kind": "boot", "boot": "boot-new", "ok": False}]}
    assert bdecide(f, one)[0] == "wait"                              # 5 min backoff
    one["attempts"][0]["ts"] = NOW - 400
    assert bdecide(f, one)[0] == "boot-clear"
    three = {"attempts": [{"ts": NOW - 5000 + i, "kind": "boot", "boot": "boot-new", "ok": False}
                          for i in range(3)]}
    assert bdecide(f, three)[0] == "giveup"
    loop = {"attempts": [{"ts": NOW - 3600 * i, "kind": "boot", "boot": f"b{i}", "ok": True}
                         for i in (1, 2, 3)]}
    assert bdecide(f, loop)[0] == "giveup"
    loop["attempts"][0]["ts"] = NOW - 90000
    assert bdecide(f, loop)[0] == "boot-clear"


def _boot_pass(paths, **kw):
    return har.main([], paths=paths, boot_set_aside=lambda p, e: har.set_aside_boot_stop(
        p, e, sleep=lambda _s: None, guard_state=lambda _p: "ready"), **kw)


def test_after_reboot_pass_keeps_evidence_writes_clearance_and_reconciles(tmp_path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv(har.BOOT_FLAG, "1")
    paths = FakePaths(tmp_path)
    paths.sol_root.mkdir(parents=True)
    paths.hold.write_text(json.dumps({"reason": har.BOOT_HOLD_REASON, "job_id": None}))
    paths.boot_clearance.write_text(json.dumps({"approved": True, "boot_id": "boot-old"}))
    paths.safety_stop.write_text(json.dumps({"reason": "memory-psi", "boot_id": "boot-old"}))
    seen = {}

    def fake_reconcile(p, job_id):
        permit = json.loads(p.boot_clearance.read_text())
        seen.update(job=job_id, cleared=permit["boot_id"], stop_gone=not p.safety_stop.exists())
        return True, {"rc": 0, "result": {"hold_exists": False}}

    old_stop = {"reason": "memory-psi", "boot_id": "boot-old"}
    out = _boot_pass(paths, reconcile=fake_reconcile,
                     facts=boot_facts(safety_stop=True, safety_record=old_stop))
    assert out["action"] == "boot-clear" and out["ok"] is True
    assert seen == {"job": "5f569bb64ce9", "cleared": "boot-new", "stop_gone": True}
    permit = json.loads(paths.boot_clearance.read_text())
    assert permit["approved"] is True and permit["approved_by"].startswith("auto:")
    assert list(paths.sol_root.glob("boot-clearance.pre-boot-new-*.json"))
    ev = Path(out["evidence"])
    for name in ("gpu-recovery-hold.json", "boot-clearance.json", "safety-stop.json",
                 "set-aside-safety-stop.json", "facts.json", "previous-boot-journal-tail.txt"):
        assert (ev / name).exists(), name
    state = json.loads(paths.state.read_text())
    assert state["attempts"][-1]["kind"] == "boot" and state["attempts"][-1]["boot"] == "boot-new"
    assert not paths.gaveup.exists()


def test_after_reboot_pass_without_hold_only_writes_clearance(tmp_path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv(har.BOOT_FLAG, "1")
    paths = FakePaths(tmp_path)
    boom = lambda p, j: pytest.fail("no hold: nothing to reconcile")
    out = _boot_pass(paths, reconcile=boom, facts=boot_facts(hold_exists=False, hold=None, lease=None))
    assert out["ok"] is True and json.loads(paths.boot_clearance.read_text())["boot_id"] == "boot-new"


def test_after_reboot_failures_retry_then_give_up_with_a_reason(tmp_path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv(har.BOOT_FLAG, "1")
    paths = FakePaths(tmp_path)
    paths.sol_root.mkdir(parents=True)
    paths.safety_stop.write_text(json.dumps({"reason": "memory-psi", "boot_id": "boot-old"}))
    fail = lambda p, j: (False, {"rc": 1, "error": "memory did not recover"})
    old_stop = {"reason": "memory-psi", "boot_id": "boot-old"}
    t = NOW
    for n in range(3):
        out = _boot_pass(paths, reconcile=fail, facts=boot_facts(
            now=t, safety_stop=True, safety_record=old_stop))
        assert out["action"] == "boot-clear" and out["ok"] is False
        assert paths.safety_stop.exists()                      # put back after a failure
        t += 1000
    gave = json.loads(paths.gaveup.read_text())
    assert "3 tries after the reboot failed" in gave["why"] and "spark1-clear-h3" in gave["next_step"]
    out = _boot_pass(paths, reconcile=fail, facts=boot_facts(now=t))
    assert out["action"] == "skip"


def test_boot_clearance_file_matches_what_the_sol_engine_reads(tmp_path, monkeypatch):
    paths = FakePaths(tmp_path)
    har.write_boot_clearance(paths, "0f7b7a77-2676-40ce-ba41-9520c30efb6c", tmp_path, "why")
    assert har.boot_cleared(paths, "0f7b7a77-2676-40ce-ba41-9520c30efb6c")
    assert not har.boot_cleared(paths, "another")
