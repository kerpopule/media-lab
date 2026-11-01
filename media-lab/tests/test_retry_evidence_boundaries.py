"""Recovery evidence boundary regressions; no app startup, engine or model calls.

Derived from the independent QA reproducers, extended to saved-state and actual
constructor/runner contracts. Only procfs, health and allocation are fixtures.
"""
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from media_lab_core import engine_licences
from .test_storyboard_retry_recovery import controller_functions, recovery


@pytest.mark.parametrize("meminfo,admitted", [
    (None, False),  # unreadable procfs
    ("", False),
    ("MemTotal: 134217728 kB\n", False),
    ("MemAvailable: invalid kB\n", False),
    ("MemAvailable:\n", False),
    ("MemAvailable: 125829120\n", False),
    ("MemAvailable: 125829120 MB\n", False),
    ("MemAvailable: 125829120 kB extra\n", False),
    ("MemAvailable: -125829120 kB\n", False),
    ("MemAvailable: 125829120.0 kB\n", False),
    ("MemAvailable: 0 kB\n", False),
    ("MemAvailable: 1048576 kB\n", False),
    ("MemTotal: 134217728 kB\nMemAvailable: 125829120 kB\n", True),
])
def test_procfs_evidence_controls_persisted_recovery(recovery, meminfo, admitted):
    ns, job, saved = recovery
    original = copy.deepcopy(job)
    reader = (Mock(side_effect=OSError("fixture unreadable procfs")) if meminfo is None
              else Mock(return_value=io.StringIO(meminfo)))
    ns["_mem_available_gb"] = controller_functions(
        "_mem_available_gb", open=reader)["_mem_available_gb"]
    ns["auto_requeue"]()
    reader.assert_called_once_with("/proc/meminfo")
    persisted = json.loads(saved.read_text())
    assert persisted["jobs"][job["id"]] == job
    if admitted:
        assert job["status"] == "queued" and job["auto_retries"] == 1
        assert persisted["queue"] == ns["queue"] == [job["id"]]
    else:
        assert {k: job[k] for k in original} == original
        assert persisted["queue"] == ns["queue"] == []
        assert "memory" in job["auto_retry_hold"]["reason"]
        assert "Operator recovery required" in job["auto_retry_hold"]["action"]
    ns["ensure_engine"].assert_not_called()
    ns["pool_cmd"].assert_not_called()
    assert not ns["RESIDENCY"].mock_calls


@pytest.mark.parametrize("meminfo,expected", [
    (None, 999.0), ("MemTotal: 123 kB\n", 999.0),
    ("MemAvailable: invalid kB\n", 999.0),
    ("MemAvailable: 1048576 kB\n", 1.0),
    ("MemAvailable: 125829120 kB\n", 120.0),
])
def test_legacy_memory_callers_keep_their_existing_contract(meminfo, expected):
    reader = (Mock(side_effect=OSError("fixture unreadable procfs")) if meminfo is None
              else Mock(return_value=io.StringIO(meminfo)))
    ns = controller_functions("_mem_available_gb", open=reader)
    assert ns["_mem_available_gb"]() == expected


class AllocationBoundary(Exception):
    pass


def make_fixture_video(request):
    maker = controller_functions(
        "make_video_job", normalize_video_source=lambda r: r,
        STYLES={"none": {"prefix": ""}},
        _h3ref=SimpleNamespace(required_turbo_preset=lambda r: None,
                               wants_singularity=lambda r: False,
                               H3_SINGULARITY_VARIANT="singularity"),
        engine_frames=lambda *a: 121, SIZES={"landscape": (1280, 720)},
        H3_SIZES={"landscape": (1280, 720)}, cast_lines=lambda *a: [],
        engine_up=lambda *a: False, h3_variant_warm=lambda singularity: False,
        engine_licences=engine_licences,
        submit_job=lambda kind, request, extra: dict(kind=kind, request=request, **extra))
    return maker["make_video_job"](request)


@pytest.mark.parametrize("kind,payload,top_engine,expected", [
    ("video", {"model": "h3", "prompt": "fixture"}, None, "h3"),
    ("video", {"model": "ltx25", "prompt": "fixture"}, None, "ltx"),
    ("video", {"prompt": "fixture"}, None, "ltx"),  # constructor default
    ("video", {"model": "h3", "engine": "ltx25"}, None, "h3"),
    ("video", {"model": "ltx25", "engine": "h3"}, None, "ltx"),
    ("video", {"model": "ltx25"}, "h3", "h3"),  # saved runner field wins
    ("video", {"model": "h3"}, "ltx25", "ltx"),
    ("filmbeat", {"engine": "h3"}, None, "ltx"),
    ("filmbeat", {"model": "h3"}, "h3", "ltx"),
    ("filmbeat", {}, None, "ltx"),
])
def test_probe_budget_and_actual_runner_agree(recovery, tmp_path, kind, payload, top_engine, expected):
    ns, job, saved = recovery
    if kind == "video":
        job.update(make_fixture_video(payload))
        runner_name = "run_video"
        deps = {"_h3_v2v_stage": lambda j: ([], None)}
    else:
        job["kind"] = "filmbeat"
        job.pop("engine", None)
        job["request"] = dict(payload, board_id="fixture-board", beat=0)
        runner_name = "run_filmbeat"
        deps = {"_load": lambda *a: [{"id": "fixture-board", "beats": [{}]}],
                "BOARDS_FILE": tmp_path / "boards.json"}
    if top_engine is not None:
        job["engine"] = top_engine
    original = copy.deepcopy(job)
    ns["http_json"].return_value = {
        "engine": expected, "ok": True, "loaded": True, "busy": False,
        "blocked": False, "loading": False}
    # LTX readiness at its budget must never authorize an H3 attempt.
    ns["_mem_available_gb"].return_value = 49
    ns["auto_requeue"]()
    assert job["status"] == ("queued" if expected == "ltx" else "error")
    if expected == "h3":
        assert {k: job[k] for k in original} == original
        assert "memory" in job["auto_retry_hold"]["reason"]
        ns["_mem_available_gb"].return_value = 118
        ns["auto_requeue"]()
    assert job["status"] == "queued" and job["auto_retries"] == 1
    assert job["request"] == original["request"]
    assert job["scenes"] == original["scenes"]
    assert json.loads(saved.read_text())["queue"] == [job["id"]]
    ns["ensure_engine"].assert_not_called()
    ns["pool_cmd"].assert_not_called()
    assert not ns["RESIDENCY"].mock_calls
    # Run the actual consumer until just before any model allocation.
    allocation = Mock(side_effect=AllocationBoundary)
    runner = controller_functions(runner_name, ensure_engine=allocation, **deps)
    with pytest.raises(AllocationBoundary):
        runner[runner_name](job)
    execution_engine = allocation.call_args.args[0]
    assert execution_engine == expected
    url = f"http://127.0.0.1:{ns['ENGINES'][execution_engine]['port']}/health"
    assert all(call.args[0] == url for call in ns["http_json"].call_args_list)


@pytest.mark.parametrize("field,value", [
    ("engine", None), ("engine", ""), ("engine", "ltx"),
    ("engine", "H3"), ("engine", "fal-video"), ("engine", "unknown"),
    ("engine", []), ("engine", {}), ("engine", 1),
    ("request", None), ("request", []), ("request", "h3"),
    ("request", {"engine": "fal-video"}), ("request", {"model": "unknown"}),
    ("request", {"model": None}), ("request", {"engine": []}),
    ("request", {"engine": ""}), ("request", {"model": "H3"}),
])
def test_malformed_or_unsupported_identity_is_held_without_probe(recovery, field, value):
    ns, job, saved = recovery
    job[field] = value
    original = copy.deepcopy(job)
    ns["auto_requeue"]()
    assert {k: job[k] for k in original} == original
    assert ns["queue"] == []
    assert job["auto_retry_hold"]
    assert json.loads(saved.read_text())["jobs"][job["id"]] == job
    ns["http_json"].assert_not_called()
    ns["ensure_engine"].assert_not_called()


def test_missing_video_execution_field_never_infers_from_request(recovery):
    ns, job, _ = recovery
    del job["engine"]
    ns["auto_requeue"]()
    assert job["status"] == "error" and job["auto_retries"] == 0
    assert "execution engine" in job["auto_retry_hold"]["reason"]
    ns["http_json"].assert_not_called()


def test_execution_engine_mutation_during_probe_never_publishes_work(recovery):
    ns, job, _ = recovery
    healthy = ns["http_json"].return_value
    def probe(*a, **kw):
        job["engine"] = "ltx25"
        return healthy
    ns["http_json"].side_effect = probe
    ns["auto_requeue"]()
    assert job["status"] == "error" and job["auto_retries"] == 0
    assert ns["queue"] == []
    ns["save_state"].assert_not_called()
