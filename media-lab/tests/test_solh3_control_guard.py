import json
import os
import shutil
import subprocess

import pytest

from media_lab_core.solh3_control_guard import (
    GuardThresholds,
    PressureSample,
    PressureTracker,
    find_unit_cgroup,
    heartbeat_allows_h3,
    terminate_cgroup,
    write_runtime_environment,
)


def sample(*, available_gib=20, swap_used_gib=0, psi_full=0):
    return PressureSample(
        available_kib=int(available_gib * 1024 * 1024),
        swap_total_kib=16 * 1024 * 1024,
        swap_free_kib=int((16 - swap_used_gib) * 1024 * 1024),
        psi_some_avg10=psi_full * 2,
        psi_full_avg10=psi_full,
    )


def thresholds():
    return GuardThresholds(
        min_available_gib=8,
        max_swap_growth_gib=2,
        max_psi_full_avg10=10,
        consecutive=3,
    )


def test_pressure_guard_trips_on_swap_growth_before_legacy_memory_floor():
    tracker = PressureTracker(thresholds(), sample(swap_used_gib=0.5))
    assert tracker.observe(sample(available_gib=10, swap_used_gib=3.0)) is None
    assert tracker.observe(sample(available_gib=10, swap_used_gib=3.0)) is None
    assert tracker.observe(sample(available_gib=10, swap_used_gib=3.0)) == "swap-growth"


def test_pressure_guard_trips_on_sustained_memory_psi():
    tracker = PressureTracker(thresholds(), sample())
    for _ in range(2):
        assert tracker.observe(sample(available_gib=12, psi_full=12)) is None
    assert tracker.observe(sample(available_gib=12, psi_full=12)) == "memory-psi"


def test_pressure_guard_resets_strikes_after_healthy_sample():
    tracker = PressureTracker(thresholds(), sample())
    assert tracker.observe(sample(available_gib=7)) is None
    assert tracker.observe(sample(available_gib=12)) is None
    assert tracker.observe(sample(available_gib=7)) is None
    assert tracker.observe(sample(available_gib=7)) is None
    assert tracker.observe(sample(available_gib=7)) == "low-memavailable"


def test_runtime_environment_is_private_atomic_and_rejects_controls(tmp_path):
    target = tmp_path / "h3.env"
    write_runtime_environment(target, {"SOL_PRELOAD": "t2va", "QUOTED": 'a"b\\c'})
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text().splitlines() == [
        'QUOTED="a\\"b\\\\c"',
        'SOL_PRELOAD="t2va"',
    ]
    with pytest.raises(ValueError):
        write_runtime_environment(target, {"BAD-KEY": "value"})
    with pytest.raises(ValueError):
        write_runtime_environment(target, {"GOOD_KEY": "line\nbreak"})


def test_heartbeat_requires_fresh_matching_boot_and_ready_state(tmp_path):
    heartbeat = tmp_path / "guard.json"
    heartbeat.write_text(json.dumps({
        "version": 1,
        "boot_id": "boot-a",
        "state": "ready",
        "written_at": 100.0,
    }))
    assert heartbeat_allows_h3(heartbeat, boot_id="boot-a", now=104.0, max_age_s=5)
    assert not heartbeat_allows_h3(heartbeat, boot_id="boot-b", now=104.0, max_age_s=5)
    assert not heartbeat_allows_h3(heartbeat, boot_id="boot-a", now=106.0, max_age_s=5)
    heartbeat.write_text("{")
    assert not heartbeat_allows_h3(heartbeat, boot_id="boot-a", now=104.0, max_age_s=5)


def test_find_unit_cgroup_requires_one_exact_unit(tmp_path):
    first = tmp_path / "user.slice" / "app.slice" / "media-lab-sol-h3.service"
    first.mkdir(parents=True)
    assert find_unit_cgroup(tmp_path, "media-lab-sol-h3.service") == first
    second = tmp_path / "other.slice" / "media-lab-sol-h3.service"
    second.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="ambiguous"):
        find_unit_cgroup(tmp_path, "media-lab-sol-h3.service")


def test_cgroup_membership_read_failure_is_not_misreported_as_empty(tmp_path):
    import media_lab_core.solh3_control_guard as guard_module

    unit = tmp_path / "media-lab-sol-h3.service"
    unit.mkdir()
    (unit / "cgroup.procs").write_text("not-a-pid\n")
    with pytest.raises(RuntimeError, match="cannot read exact H3 cgroup membership"):
        guard_module.cgroup_pids(unit)


def test_cgroup_membership_treats_removed_unit_as_empty(tmp_path):
    import media_lab_core.solh3_control_guard as guard_module

    unit = tmp_path / "media-lab-sol-h3.service"
    assert guard_module.cgroup_pids(unit) == set()


def test_terminate_cgroup_targets_only_recursive_members(tmp_path):
    unit = tmp_path / "media-lab-sol-h3.service"
    child = unit / "workers"
    child.mkdir(parents=True)
    (unit / "cgroup.procs").write_text("101\n")
    (child / "cgroup.procs").write_text("202\n203\n")
    sent = []
    alive = {101, 202, 203}

    def fake_signal(pidfd, sig):
        pid = pidfd
        sent.append((pid, sig))
        if sig == 9:
            alive.discard(pid)

    survivors = terminate_cgroup(
        unit,
        term_wait_s=0,
        kill_wait_s=0,
        pidfd_open_fn=lambda pid: pid,
        pidfd_signal_fn=fake_signal,
        close_fn=lambda _: None,
        alive_fn=lambda pid: pid in alive,
        sleep_fn=lambda _: None,
    )
    assert survivors == []
    assert {pid for pid, sig in sent if sig == 15} == {101, 202, 203}
    assert {pid for pid, sig in sent if sig == 9} == {101, 202, 203}
    assert all(pid not in {os.getpid(), 999} for pid, _ in sent)


def test_terminate_cgroup_stops_a_real_synthetic_member(tmp_path):
    if not hasattr(os, "pidfd_open"):
        pytest.skip("pidfd is Linux-only")
    unit = tmp_path / "media-lab-sol-h3.service"
    unit.mkdir()
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        (unit / "cgroup.procs").write_text(f"{sleeper.pid}\n")
        assert terminate_cgroup(
            unit,
            term_wait_s=1,
            kill_wait_s=1,
            alive_fn=lambda pid: pid == sleeper.pid and sleeper.poll() is None,
        ) == []
        assert sleeper.wait(timeout=1) < 0
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=1)


def test_terminate_cgroup_revalidates_membership_after_opening_pidfd(tmp_path):
    unit = tmp_path / "media-lab-sol-h3.service"
    unit.mkdir()
    members = unit / "cgroup.procs"
    members.write_text("101\n")
    sent = []

    def open_then_move(pid):
        members.write_text("")
        return pid

    assert terminate_cgroup(
        unit,
        term_wait_s=0,
        kill_wait_s=0,
        pidfd_open_fn=open_then_move,
        pidfd_signal_fn=lambda pidfd, sig: sent.append((pidfd, sig)),
        close_fn=lambda _: None,
        alive_fn=lambda _: True,
        sleep_fn=lambda _: None,
    ) == []
    assert sent == []


def test_terminate_cgroup_accepts_unit_removal_after_member_exits(tmp_path):
    unit = tmp_path / "media-lab-sol-h3.service"
    unit.mkdir()
    (unit / "cgroup.procs").write_text("101\n")
    sent = []

    def open_then_remove_unit(pid):
        shutil.rmtree(unit)
        return pid

    assert terminate_cgroup(
        unit,
        term_wait_s=0,
        kill_wait_s=0,
        pidfd_open_fn=open_then_remove_unit,
        pidfd_signal_fn=lambda pidfd, sig: sent.append((pidfd, sig)),
        close_fn=lambda _: None,
        alive_fn=lambda _: True,
        sleep_fn=lambda _: None,
    ) == []
    assert sent == []


def test_trip_persists_incident_before_signalling_exact_cgroup(tmp_path, monkeypatch):
    import media_lab_core.solh3_control_guard as guard_module

    unit = tmp_path / "media-lab-sol-h3.service"
    unit.mkdir()
    (unit / "cgroup.procs").write_text("4242\n")
    guard = guard_module.ControlPlaneGuard.__new__(guard_module.ControlPlaneGuard)
    guard.boot_id = "boot-a"
    guard.limits = thresholds()
    guard.safety_stop = tmp_path / "safety-stop.json"
    guard.latch = tmp_path / "guard.latch"
    guard.incident_dir = tmp_path / "incidents"
    guard.cgroup = unit

    observed = {}

    def terminate_after_receipt(cgroup):
        receipts = list(guard.incident_dir.glob("*.json"))
        observed["receipt_count"] = len(receipts)
        observed["receipt"] = json.loads(receipts[0].read_text()) if receipts else None
        return []

    monkeypatch.setattr(guard_module, "terminate_cgroup", terminate_after_receipt)
    guard._trip("swap-growth", sample(available_gib=9, swap_used_gib=3))

    assert observed["receipt_count"] == 1
    assert observed["receipt"]["status"] == "terminating"
    receipt = json.loads(next(guard.incident_dir.glob("*.json")).read_text())
    assert receipt["status"] == "terminated"
    assert receipt["target_pids"] == [4242]
    assert receipt["survivors"] == []
