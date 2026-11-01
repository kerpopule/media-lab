"""One restart owner for the studio, with strikes, an hourly cap and 'stuck'."""
import json
import urllib.error
from unittest import mock

import pytest

from runner import queue_watchdog as qw
from runner import service_supervisor as sv

NOW = 1_800_000_000.0


def run_main(state, *, queue=None, unreachable=False, held=False, active="active"):
    """One watchdog pass with every side effect captured."""
    calls = {"restarts": [], "saved": None, "logs": []}

    def fake_run(cmd, *a, **k):
        if cmd[:3] == ["systemctl", "--user", "restart"]:
            calls["restarts"].append(cmd[3])
        return mock.Mock(returncode=0, stdout=active + "\n", stderr="")

    resp = mock.MagicMock()
    resp.__enter__.return_value = resp
    opener = mock.patch.object(
        qw.LOCAL_OPENER, "open",
        side_effect=urllib.error.URLError("refused") if unreachable else None,
        return_value=resp)
    with opener, \
         mock.patch.object(qw.json, "load", return_value={"active": queue or []}), \
         mock.patch.object(qw, "load_state", return_value=state), \
         mock.patch.object(qw, "save_state", side_effect=lambda st: calls.update(saved=dict(st))), \
         mock.patch.object(qw, "gpu_recovery_held", return_value=held), \
         mock.patch.object(qw, "ensure_pool_lock"), mock.patch.object(qw, "check_tunnel"), \
         mock.patch.object(qw, "maestro_queue_runner_active", return_value=False), \
         mock.patch.object(qw.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(qw, "log", side_effect=lambda m: calls["logs"].append(m)):
        qw.main()
    return calls


def test_unreachable_needs_three_strikes_before_a_restart():
    st = {}
    for expected in (0, 0, 1):
        calls = run_main(st, unreachable=True)
        st = calls["saved"]
        assert len(calls["restarts"]) == expected
    assert st["unreachable_strikes"] == 0


def test_a_good_answer_resets_the_unreachable_strikes():
    calls = run_main({"unreachable_strikes": 2})
    assert calls["saved"]["unreachable_strikes"] == 0 and calls["restarts"] == []


def test_restarts_are_capped_per_hour_then_flagged(monkeypatch):
    monkeypatch.setattr(qw.time, "time", lambda: NOW)
    st = {"restarts": [NOW - 3000, NOW - 2000, NOW - 1000], "last_restart": NOW - 1000}
    with mock.patch.object(qw.subprocess, "run") as run, mock.patch.object(qw, "log"):
        assert qw.restart_app(st, "test") is False
    run.assert_not_called()
    assert st["restart_capped"] is True
    st = {"restarts": [NOW - 4000], "last_restart": NOW - 4000}
    with mock.patch.object(qw.subprocess, "run") as run, mock.patch.object(qw, "log"):
        assert qw.restart_app(st, "test") is True
    assert st["restart_capped"] is False and st["restarts"] == [NOW]


def test_a_stalled_queue_gets_one_restart_then_is_marked_stuck():
    queue = [{"id": "a", "status": "queued"}]
    st = {"idle_strikes": 1}
    first = run_main(st, queue=queue)
    assert first["restarts"] == ["media-lab-simple.service"]
    st = first["saved"]
    assert st["stall_restarted"] is True
    st["last_restart"] = 0          # cooldown long over
    second = run_main(st, queue=queue)
    third = run_main(second["saved"], queue=queue)
    assert second["restarts"] == [] and third["restarts"] == []
    assert third["saved"]["stuck"] is True
    cleared = run_main(third["saved"], queue=[])
    assert "stuck" not in cleared["saved"] and "stall_restarted" not in cleared["saved"]


def test_hold_still_stands_clear():
    calls = run_main({}, queue=[{"id": "a", "status": "queued"}], held=True)
    assert calls["restarts"] == []


def test_pool_lock_owned_by_the_controller_is_not_missing():
    with mock.patch.object(qw.os.path, "exists", return_value=True), \
         mock.patch.object(qw, "controller_holds_gpu_lock", return_value=True), \
         mock.patch.object(qw.subprocess, "run",
                           return_value=mock.Mock(returncode=3)) as run, \
         mock.patch.object(qw, "log") as log:
        qw.ensure_pool_lock()
    assert all("pool_lock.sh" not in " ".join(c.args[0]) for c in run.call_args_list)
    log.assert_not_called()


def test_public_edge_probe_uses_a_door_exempt_path():
    if qw.TUNNEL_URL:
        assert qw.TUNNEL_URL.endswith("/manifest.json")
    source = (qw.__file__ and open(qw.__file__).read())
    assert 'f"https://{_PUBLIC[0]}/manifest.json"' in source


def test_supervisor_never_restarts_the_studio_or_starts_local_chat(monkeypatch):
    assert "media-lab-simple.service" not in [u for u, _, _ in sv.UNITS]
    assert sv.SUPERVISE_LOCAL_CHAT is False
    ran = []
    monkeypatch.setattr(sv, "load", lambda: {"strikes": {"chat:qwen": 9}})
    monkeypatch.setattr(sv, "save", lambda st: None)
    monkeypatch.setattr(sv, "unit_active", lambda u: True)
    monkeypatch.setattr(sv, "probe", lambda url, timeout=10: True)
    monkeypatch.setattr(sv, "text_bridge_models", lambda timeout=10: None)
    monkeypatch.setattr(sv.urllib.request, "urlopen", mock.Mock(side_effect=OSError("no image")))

    def fake_run(cmd, *a, **k):
        ran.append(cmd)
        return mock.Mock(returncode=0, stdout="true\nunless-stopped\n", stderr="")
    monkeypatch.setattr(sv.subprocess, "run", fake_run)
    monkeypatch.setattr(sv, "log", lambda m: None)
    sv.main()
    assert not any(c[:2] == ["docker", "restart"] for c in ran)
    assert not any(c[:2] == ["docker", "start"] for c in ran)
    assert not any("media-lab-simple.service" in c for c in ran)
