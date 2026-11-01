"""CPU-only execution of controller functions; never imports app startup/services.

External text/image engines are fixture boundaries, not a live render claim.
"""
import ast
import copy
import json
from pathlib import Path
import random
import threading
import time
from unittest.mock import Mock

import pytest

APP = Path(__file__).parents[1] / "app.py"
POLICY = Path(__file__).parents[1] / "config/model-residency-policy.json"


def h3_retry_admission_floor():
    """The approved H3 recovery admission boundary, read from the live policy.

    ``_auto_retry_recovery_error`` admits a retry only when available memory
    covers the largest H3 phase plus the operational floor. A literal stood here
    while the approved boundary moved (115 GiB decode + 2 GiB floor = 117 GiB).
    """
    policy = json.loads(POLICY.read_text())
    phases = policy["models"]["h3"]["phases_gb"]
    return (max(float(phases[p]) for p in ("cold_load", "sampler", "decode"))
            + float(policy["operational_floor_gb"]))


H3_RETRY_FLOOR_GB = h3_retry_admission_floor()


def controller_functions(*names, **dependencies):
    tree = ast.parse(APP.read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), dependencies)
    return dependencies


@pytest.mark.parametrize("cast_ids", [[], ["custom"], ["known:test", "custom"]])
@pytest.mark.parametrize("with_stills", [False, True])
def test_storyboard_completes_with_one_consistent_character_snapshot(tmp_path, cast_ids, with_stills):
    chars = [{"id": "custom", "name": "Fixture", "appearance": "blue coat"}]
    known = [{"id": "known:test", "name": "Preset", "appearance": "red coat"}]
    chars_file, boards_file = tmp_path / "characters.json", tmp_path / "boards.json"
    chars_file.write_text(json.dumps(chars))
    boards_file.write_text(json.dumps([{"id": "older", "beats": []}]))
    original = chars_file.read_bytes()
    loads = []
    def load(path, default):
        loads.append(path)
        return json.loads(path.read_text()) if path.exists() else default
    composed = []
    def compose(board, records):
        composed.append(copy.deepcopy(records))
        board["beats"][0]["composed_prompt"] = "fixture prompt"
    ns = controller_functions(
        "run_storyboard", "resolve_cast_records", "selectable_characters", "ming_render_refusal",
        _load=load, _save=lambda p, v: p.write_text(json.dumps(v)),
        CHARS_FILE=chars_file, BOARDS_FILE=boards_file, known_characters=lambda: known,
        MAX_PREMISE=5000, BOARD_SYS="fixture", BOARD_MAX_TOKENS=500, MAX_BEATS=10,
        qwen_json=lambda *a, **kw: {"title": "Fixture", "beats": [{"duration": 6}]},
        clean_bible=lambda data, cast: {"style": "", "world": "", "camera": "", "characters": cast},
        beat_seconds=lambda x: x, recompose_board=compose, SIZES={"landscape": (1280, 720)},
        random=random, time=time, jobs={}, fail=Mock(side_effect=AssertionError("unexpected failure")),
        board_size=lambda board: (1280, 720), img_graph=lambda *a, **kw: {},
        still_prompt=lambda text: text, _image_engine_run=lambda *a: {},
        beat_likeness_char=lambda board, beat, records: composed.append(copy.deepcopy(records)),
    )
    request = {"cast": cast_ids, "idea": "Synthetic storyboard", "with_stills": with_stills, "seed": 123}
    j = {"id": "fixture-story", "request": copy.deepcopy(request)}
    ns["run_storyboard"](j)
    assert j["status"] == "done"
    boards = json.loads(boards_file.read_text())
    assert [b["id"] for b in boards] == ["fixture-story", "older"]
    assert [c["id"] for c in boards[0]["bible"]["characters"]] == cast_ids
    assert composed == [chars + known] * (2 if with_stills else 1)
    assert loads.count(chars_file) == 1
    assert j["request"] == request
    assert chars_file.read_bytes() == original


@pytest.fixture
def recovery(tmp_path):
    j = {"id": "fixture-failed", "kind": "video", "engine": "h3", "status": "error", "stage": "error",
         "request": {"model": "h3", "source": "/media/fixture.png"},
         "retryable": True, "auto_retries": 0, "finished": time.time(),
         "message": "Remote end closed connection without response", "detail": "original error",
         "scenes": [{"url": "/media/completed.mp4"}]}
    state_file = tmp_path / "jobs.json"
    policy = Path(__file__).parents[1] / "config/model-residency-policy.json"
    health = Mock(return_value={"ok": True, "engine": "h3", "loaded": True,
                               "busy": False, "blocked": False, "loading": False,
                               "impl": "sol-h3-spark"})
    tree = ast.parse(APP.read_text())
    helpers = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name.startswith("_auto_retry_")]
    import math
    ns = controller_functions(
        "auto_requeue", "job_engine", "save_state", "_save", *helpers,
        jobs={j["id"]: j}, queue=[], online_queue=[], cv=threading.Condition(),
        JOBS_FILE=state_file, Path=Path, _iolock=threading.Lock(),
        ENGINE_MAINTENANCE=tmp_path / ".engine-maintenance", time=time, math=math,
        AUTO_RETRY_MAX=3, AUTO_RETRY_WINDOW_S=21600,
        INFRA_FAILURE_MARKS=["connection", "unavailable"],
        ENGINES={"h3": {"port": 8291, "health": "/health"},
                 "ltx": {"port": 8290, "health": "/health"}},
        http_json=health, _mem_available_gb=Mock(return_value=200),
        _residency_policy=policy, json=json,
        ensure_engine=Mock(side_effect=AssertionError("must not allocate")),
        pool_cmd=Mock(side_effect=AssertionError("must not allocate")),
        RESIDENCY=Mock(),
    )
    ns["save_state"] = Mock(side_effect=ns["save_state"])
    return ns, j, state_file


def test_unknown_health_preserves_terminal_state_and_persists_escalation(recovery):
    ns, j, state_file = recovery
    original = copy.deepcopy(j)
    ns["http_json"].side_effect = TimeoutError("fixture unavailable")
    ns["auto_requeue"]()
    assert ns["queue"] == []
    assert {k: j[k] for k in original} == original
    assert "health" in j["auto_retry_hold"]["reason"].lower()
    assert "operator" in j["auto_retry_hold"]["action"].lower()
    assert json.loads(state_file.read_text())["jobs"][j["id"]] == j
    ns["ensure_engine"].assert_not_called()
    ns["pool_cmd"].assert_not_called()
    assert not ns["RESIDENCY"].mock_calls


def test_maintenance_hold_is_exact_noop(recovery):
    ns, j, _ = recovery
    original = copy.deepcopy(j)
    ns["ENGINE_MAINTENANCE"].touch()
    ns["auto_requeue"]()
    assert j == original
    assert ns["queue"] == []
    ns["http_json"].assert_not_called()
    ns["save_state"].assert_not_called()


@pytest.mark.parametrize("change", [
    {"ok": False}, {"ok": "true"}, {"engine": "ltx"}, {"loaded": False},
    {"loaded": None}, {"busy": True}, {"busy": None}, {"blocked": True},
    {"blocked": None}, {"loading": True}, {"loading": None},
])
def test_unready_engine_never_requeues(recovery, change):
    ns, j, _ = recovery
    ns["http_json"].return_value.update(change)
    ns["auto_requeue"]()
    assert j["status"] == "error" and j["auto_retries"] == 0
    assert ns["queue"] == [] and j["auto_retry_hold"]


@pytest.mark.parametrize("payload", [None, [], {}, {"ok": True}])
def test_malformed_health_is_not_recovery(recovery, payload):
    ns, j, _ = recovery
    ns["http_json"].return_value = payload
    ns["auto_requeue"]()
    assert j["status"] == "error" and ns["queue"] == []


@pytest.mark.parametrize("available", [0, 3, 10.8, H3_RETRY_FLOOR_GB - 0.1, -1, float("nan"), float("inf"), None])
def test_h3_memory_floor_is_fail_closed(recovery, available):
    ns, j, _ = recovery
    ns["_mem_available_gb"].return_value = available
    ns["auto_requeue"]()
    assert j["status"] == "error" and ns["queue"] == []
    assert "memory" in j["auto_retry_hold"]["reason"]


def test_h3_memory_admits_only_at_the_approved_boundary(recovery):
    """117 GiB is the approved cold admission; one tenth below it must hold."""
    ns, j, _ = recovery
    assert H3_RETRY_FLOOR_GB == 117
    ns["_mem_available_gb"].return_value = H3_RETRY_FLOOR_GB - 0.1
    ns["auto_requeue"]()
    assert j["status"] == "error" and ns["queue"] == []
    assert "memory" in j["auto_retry_hold"]["reason"]
    ns["_mem_available_gb"].return_value = H3_RETRY_FLOOR_GB
    ns["auto_requeue"]()
    assert j["status"] == "queued" and ns["queue"] == [j["id"]]


@pytest.mark.parametrize("update", [
    {"auto_retries": 3}, {"auto_retries": -1}, {"auto_retries": "bad"},
    {"auto_retries": 0.5}, {"finished": 1}, {"finished": float("nan")},
    {"finished": "bad"}, {"finished": time.time() + 3600},
    {"retryable": False}, {"retryable": "true"},
])
def test_ineligible_failure_stays_terminal_with_action(recovery, update):
    ns, j, _ = recovery
    j.update(update)
    ns["auto_requeue"]()
    assert j["status"] == "error" and ns["queue"] == []
    assert j["auto_retry_hold"]
    ns["http_json"].assert_not_called()


@pytest.mark.parametrize("update", [{"cancel": True}, {"status": "cancelled"}, {"status": "done"}])
def test_user_stop_and_other_terminal_states_are_untouched(recovery, update):
    ns, j, _ = recovery
    j.update(update)
    original = copy.deepcopy(j)
    ns["auto_requeue"]()
    assert j == original and ns["queue"] == []
    ns["http_json"].assert_not_called()


def test_one_verified_job_only_preserves_sources_and_checkpoints(recovery):
    ns, j, path = recovery
    second = copy.deepcopy(j); second["id"] = "second"
    ns["jobs"]["second"] = second
    original = copy.deepcopy(j)
    ns["auto_requeue"]()
    assert ns["queue"] == [j["id"]]
    assert j["status"] == "queued" and j["auto_retries"] == 1
    assert second["status"] == "error" and second["auto_retries"] == 0
    for key in ("request", "scenes", "finished", "detail"):
        assert j[key] == original[key]
    assert json.loads(path.read_text())["queue"] == [j["id"]]
    ns["auto_requeue"]()
    assert ns["queue"] == [j["id"]] and j["auto_retries"] == 1


@pytest.mark.parametrize("event", ["cancel", "maintenance", "new-job"])
def test_queue_commit_rechecks_state_changed_during_probe(recovery, event):
    ns, j, _ = recovery
    healthy = ns["http_json"].return_value
    def probe(*a, **kw):
        if event == "cancel":
            j["cancel"] = True
        elif event == "maintenance":
            ns["ENGINE_MAINTENANCE"].touch()
        else:
            ns["jobs"]["new"] = {"status": "queued"}
        return healthy
    ns["http_json"].side_effect = probe
    ns["auto_requeue"]()
    assert ns["queue"] == [] and j["status"] == "error" and j["auto_retries"] == 0


def test_repeated_unhealthy_passes_do_not_burn_budget_or_rewrite_state(recovery):
    ns, j, _ = recovery
    ns["http_json"].return_value["blocked"] = True
    for _ in range(5):
        ns["auto_requeue"]()
    assert ns["queue"] == [] and j["auto_retries"] == 0
    assert ns["save_state"].call_count == 1


def test_exact_engine_not_warm_default_is_probed(recovery):
    ns, _, _ = recovery
    ns["auto_requeue"]()
    ns["http_json"].assert_called_once_with("http://127.0.0.1:8291/health", timeout=3)


def test_failed_queue_persistence_does_not_publish_work(recovery):
    ns, j, _ = recovery
    original = copy.deepcopy(j)
    ns["save_state"].side_effect = OSError("fixture disk failure")
    with pytest.raises(OSError):
        ns["auto_requeue"]()
    assert j == original and ns["queue"] == []


def test_failed_hold_persistence_is_retried_not_silently_cached(recovery):
    ns, j, path = recovery
    save = ns["save_state"].side_effect
    ns["save_state"].side_effect = OSError("fixture disk failure")
    ns["http_json"].return_value["blocked"] = True
    with pytest.raises(OSError):
        ns["auto_requeue"]()
    ns["save_state"].side_effect = save
    ns["auto_requeue"]()
    assert json.loads(path.read_text())["jobs"][j["id"]]["auto_retry_hold"]


def test_ltx_uses_its_own_phase_budget_and_health(recovery):
    ns, j, _ = recovery
    j["request"] = {"model": "ltx25"}
    j["engine"] = "ltx25"
    ns["http_json"].return_value = {"ok": True, "engine": "ltx", "loaded": True, "busy": False}
    ns["_mem_available_gb"].return_value = 49
    ns["auto_requeue"]()
    assert j["status"] == "queued"
    ns["http_json"].assert_called_once_with("http://127.0.0.1:8290/health", timeout=3)


@pytest.mark.parametrize("kind,model", [
    ("musicvideo", "h3"), ("storyboard", "h3"), ("image", "h3"),
    ("video", "fal-video"), ("video", "unknown"),
])
def test_unverified_recovery_contract_escalates_without_network(recovery, kind, model):
    ns, j, _ = recovery
    j["kind"] = kind; j["request"] = {"model": model}
    ns["auto_requeue"]()
    assert j["status"] == "error" and j["auto_retry_hold"]
    ns["http_json"].assert_not_called()


@pytest.mark.parametrize("malformed", ["missing", "not-json", "missing-decode", "nan-floor"])
def test_unreadable_or_invalid_memory_policy_refuses_retry(recovery, tmp_path, malformed):
    ns, j, _ = recovery
    original = json.loads(ns["_residency_policy"].read_text())
    ns["_residency_policy"] = tmp_path / "policy.json"
    if malformed == "not-json":
        ns["_residency_policy"].write_text("not-json")
    elif malformed != "missing":
        if malformed == "missing-decode":
            del original["models"]["h3"]["phases_gb"]["decode"]
        else:
            original["operational_floor_gb"] = float("nan")
        ns["_residency_policy"].write_text(json.dumps(original))
    ns["auto_requeue"]()
    assert j["status"] == "error" and "memory" in j["auto_retry_hold"]["reason"]


def test_restart_preserves_hold_then_recovery_clears_only_hold(recovery):
    ns, j, path = recovery
    ns["http_json"].return_value["blocked"] = True
    ns["auto_requeue"]()
    saved = json.loads(path.read_text())
    ns["jobs"] = saved["jobs"]; ns["queue"] = saved["queue"]
    ns["auto_requeue"]()
    assert ns["queue"] == []
    ns["http_json"].return_value["blocked"] = False
    ns["auto_requeue"]()
    current = ns["jobs"][j["id"]]
    assert current["status"] == "queued" and "auto_retry_hold" not in current
    assert current["request"] == j["request"] and current["scenes"] == j["scenes"]


def test_request_changed_during_probe_is_not_requeued(recovery):
    ns, j, _ = recovery
    health = ns["http_json"].return_value
    def probe(*a, **kw):
        j["request"]["model"] = "ltx25"
        return health
    ns["http_json"].side_effect = probe
    ns["auto_requeue"]()
    assert j["status"] == "error" and ns["queue"] == []


def test_source_and_completed_asset_bytes_are_never_modified(recovery, tmp_path):
    import hashlib
    ns, j, _ = recovery
    source = tmp_path / "fixture-source.bin"
    completed = tmp_path / "fixture-completed.bin"
    for p in (source, completed):
        p.write_bytes(b"synthetic fixture bytes: not a media render")
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
              for p in (source, completed)}
    j["request"]["source"] = str(source)
    j["scenes"] = [{"url": str(completed)}]
    ns["http_json"].return_value["blocked"] = True
    ns["auto_requeue"]()
    ns["http_json"].return_value["blocked"] = False
    ns["auto_requeue"]()
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                      for p in (source, completed)}
