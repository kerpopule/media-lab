"""Real / Long (H3 Singularity) inside the studio: routing, jobs, lease task,
residency hand-back to warm Sol and the model picker's time estimates."""
import ast
import base64
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runner import h3_reference as h3ref

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app.py"
PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
    b"\x00\x00IEND\xaeB`\x82").decode()


def function(name, **namespace):
    tree = ast.parse(APP.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(APP), "exec"), namespace)
    return namespace[name]


# -------------------------------------------------------------- the contract
def test_real_long_is_chosen_explicitly_or_by_reference_routing(monkeypatch):
    assert h3ref.wants_singularity({"h3_engine": "singularity"})
    assert not h3ref.wants_singularity({"references": [{"b64": PNG}]})
    monkeypatch.setattr(h3ref, "ROUTE_REFERENCES_TO_SINGULARITY", True)
    assert h3ref.wants_singularity({"references": [{"b64": PNG}]})
    assert h3ref.wants_singularity({"video_references": [{"source": "/media/a.mp4"}]})
    assert not h3ref.wants_singularity({"prompt": "text only"})
    assert not h3ref.wants_singularity({"h3_fused_r1024": True, "references": [{"b64": PNG}]})


def test_lease_task_and_runtime_follow_the_choice(monkeypatch):
    assert h3ref.task_for({"h3_engine": "singularity", "start_image_b64": "x"}) == "singularity"
    assert h3ref.task_for({"references": [{"b64": PNG}]}) == "ref2va"
    assert h3ref.task_for({"source": "/media/a.png"}) == "fl2va"
    assert h3ref.task_for({}) == "t2va"
    assert h3ref.required_runtime_config({"h3_engine": "singularity"}) == \
        {"variant": "singularity", "turbo_preset": None}
    with pytest.raises(ValueError, match="Turbo"):
        h3ref.required_runtime_config({"h3_engine": "singularity", "h3_turbo": True})
    assert "singularity" in h3ref.H3_VARIANTS and "singularity" in h3ref.H3_TASKS
    task = function("_gpu_task_for_engine", _h3ref=h3ref)
    assert task("h3", {"request": {"h3_engine": "singularity", "references": [1]}}) == "singularity"
    assert task("h3", {"request": {"references": [1]}}) == "ref2va"


def test_real_long_takes_text_alone_or_up_to_nine_pictures():
    assert h3ref.validate_h3_reference_request("h3", "singularity", []) is None
    assert h3ref.validate_h3_reference_request("h3", "singularity", [{"b64": PNG}]) is None
    assert "decodable" in h3ref.validate_h3_reference_request("h3", "singularity", [{"b64": "!!"}])
    h3ref.assert_ref_count_ok(9, singularity=True)
    with pytest.raises(ValueError):
        h3ref.assert_ref_count_ok(10, singularity=True)
    with pytest.raises(ValueError):
        h3ref.assert_ref_count_ok(5)          # Sol's Ref2VA cap is unchanged


def test_audio_references_are_validated_like_video_references():
    good = h3ref.normalize_audio_references([{"source": "/media/voice.wav", "start_sec": 1.5}])
    assert good == [{"source": "/media/voice.wav", "role": "voice and sound", "start_sec": 1.5}]
    assert h3ref.normalize_audio_references([{"source": "/etc/passwd"}, {"source": "/media/x.exe"},
                                             {"source": "/media/a.wav", "start_sec": -1}]) == []
    with pytest.raises(ValueError):
        h3ref.normalize_audio_references([{"source": f"/media/{i}.wav"} for i in range(4)])


# ------------------------------------------------------------- engine calls
def _preflight():
    import runner.h3_singularity as sing
    return function("preflight", _h3ref=h3ref, _h3sing=sing, singularity_max_frames=lambda: 362,
                    H3_MIN_FRAMES=124, H3_MAX_FRAMES=345, print=lambda *a, **k: None)


def test_preflight_keeps_real_long_length_and_shape_and_leaves_sol_alone():
    pre = _preflight()
    body = pre("h3", {"prompt": "p", "frames": 300, "width": 768, "height": 1344,
                      "h3_engine": "singularity"})
    assert body["frames"] == 311 and body["orientation"] == "portrait"
    assert (body["width"], body["height"]) == (672, 1216)
    assert pre("h3", {"frames": 999, "h3_engine": "singularity"})["frames"] == 362
    sol = pre("h3", {"prompt": "p", "frames": 300, "width": 768, "height": 1344})
    assert sol["frames"] == 311 and (sol["width"], sol["height"]) == (768, 1344)
    assert "h3_engine" not in sol


def test_render_task_is_singularity_for_real_long_bodies():
    from contextlib import contextmanager
    seen = []

    @contextmanager
    def operation(engine, task, job):
        seen.append(task)
        yield object()
    generate = function("engine_generate", gpu_operation=operation, _h3ref=h3ref,
                        _engine_generate_authorized=lambda *a, **k: {"ok": True})
    generate("h3", {"h3_engine": "singularity", "start_image_b64": "x"}, {"id": "j"})
    generate("h3", {"start_image_b64": "x"}, {"id": "j"})
    assert seen == ["singularity", "fl2va"]


def test_a_real_long_load_lease_boots_the_real_long_variant():
    source = APP.read_text()
    block = source[source.index("    def start_model(self, model, detail):"):
                   source.index("    def model_healthy(self, model):")]
    assert "lease.task == _h3ref.H3_SINGULARITY_TASK" in block
    assert 'target.update(variant=_h3ref.H3_SINGULARITY_VARIANT, turbo_preset=None)' in block


def test_boot_refuses_mismatched_variant_and_task():
    source = APP.read_text()
    boot = source[source.index("def _boot_engine("):source.index("def ensure_h3_variant(")]
    assert "the Real / Long variant and task family go together" in boot
    assert "runtime_env.update({k: v for k, v in local_config.singularity().items() if v})" in boot


# ------------------------------------------------- hand-back to warm Sol
def _stand_down(config, busy=False, jobs=None, idle=700.0, hold=False):
    stopped = []
    fn = function("stand_down_idle_real_long", h3_resident_config=lambda: config,
                  engine_busy=lambda name: busy, gpu_recovery_pending=lambda: hold,
                  ENGINE_MAINTENANCE=Path("/nonexistent/.engine-maintenance"),
                  jobs=jobs or {}, job_engine=lambda j: "h3", _h3ref=h3ref,
                  engine_idle_s=lambda name: idle, H3_SINGULARITY_LINGER_S=600,
                  stop_engine=stopped.append, print=lambda *a, **k: None)
    return fn(), stopped


def test_idle_real_long_is_stood_down_so_warm_sol_comes_back():
    real = {"variant": "singularity", "task": "singularity"}
    assert _stand_down(real) == (True, ["h3"])
    assert _stand_down(real, idle=120.0) == (False, [])            # still lingering
    assert _stand_down(real, busy=True) == (False, [])
    assert _stand_down(real, hold=True) == (False, [])
    queued = {"x": {"status": "queued", "request": {"h3_engine": "singularity"}}}
    assert _stand_down(real, jobs=queued) == (False, [])
    assert _stand_down({"variant": "fl2va", "task": "t2va"}) == (False, [])   # Sol is never touched
    assert _stand_down(None) == (False, [])


def test_the_reaper_runs_the_stand_down_before_the_idle_restore():
    source = APP.read_text()
    reaper = source[source.index("def reaper():"):source.index("def comfy_run(")]
    assert reaper.index("stand_down_idle_real_long()") < reaper.index("restore_warm_ltx_idle()")


# ------------------------------------------------------------- the studio
@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    home = tmp_path_factory.mktemp("real-long-home")
    root = home / "media-lab-simple"
    root.mkdir()
    for name in ("static", "config"):
        (root / name).symlink_to(REPO / name)
    old = {k: os.environ.get(k) for k in ("HOME", "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS")}
    os.environ["HOME"] = str(home)
    os.environ["MEDIA_LAB_DISABLE_BACKGROUND_WORKERS"] = "1"
    spec = importlib.util.spec_from_file_location("real_long_test_app", REPO / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["real_long_test_app"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    module.pick_next_job = lambda: None
    module.engine_up = lambda name: False
    module.h3_resident_config = lambda: None
    yield module


@pytest.fixture
def installed(studio, monkeypatch):
    monkeypatch.setattr(studio, "singularity_installed", lambda: True)
    return studio


def _drop(studio, job):
    studio.jobs.pop(job["id"], None)
    if job["id"] in studio.queue:
        studio.queue.remove(job["id"])


def test_the_studio_never_routes_references_on_a_host_without_real_long(studio):
    assert studio._h3ref.ROUTE_REFERENCES_TO_SINGULARITY is False
    with pytest.raises(ValueError, match="not installed"):
        studio.make_video_job({"model": "h3-real", "prompt": "hello"})


def test_licence_gates_real_long(installed, monkeypatch):
    monkeypatch.setenv("MEDIA_LAB_PERSONAL_ENGINES", "h3")
    with pytest.raises(ValueError, match="MEDIA_LAB_PERSONAL_ENGINES=h3-singularity"):
        installed.make_video_job({"model": "h3-real", "prompt": "hello"})
    monkeypatch.setenv("MEDIA_LAB_PERSONAL_ENGINES", "h3-singularity")
    with pytest.raises(ValueError, match="MEDIA_LAB_PERSONAL_ENGINES=h3"):
        installed.make_video_job({"model": "h3-real", "prompt": "hello"})


def test_a_real_long_job_keeps_its_length_shape_and_label(installed):
    job = installed.make_video_job({"model": "h3-real", "prompt": "She talks to the camera.",
                                    "duration": "12", "orientation": "portrait"})
    try:
        assert job["engine"] == "h3" and job["request"]["model"] == "h3"
        assert job["request"]["h3_engine"] == "singularity"
        assert job["frames"] == 294 and (job["w"], job["h"]) == (672, 1216)
        assert job["engine_label"] == "Real / Long" and job["warm"] is False
        assert installed.eta_key(job) == "video/h3-real/294/cold"
        # 12 s cold: ~740 s render + ~430 s load
        assert 17 <= installed.eta_estimate(job) <= 22
        assert installed.job_engine(job) == "h3"
    finally:
        _drop(installed, job)


def test_remix_keeps_real_long_and_refuses_turbo(installed):
    job = installed.make_video_job({"model": "h3", "h3_engine": "singularity", "prompt": "again"})
    try:
        assert job["request"]["h3_engine"] == "singularity"
    finally:
        _drop(installed, job)
    with pytest.raises(ValueError, match="Turbo"):
        installed.make_video_job({"model": "h3-real", "prompt": "x", "h3_turbo": True})
    with pytest.raises(ValueError, match="audio references need"):
        installed.make_video_job({"model": "h3", "prompt": "x",
                                  "audio_references": [{"source": "/media/a.wav"}]})


def test_references_route_to_real_long_when_the_host_has_it(installed, monkeypatch):
    monkeypatch.setattr(installed._h3ref, "ROUTE_REFERENCES_TO_SINGULARITY", True)
    refs = [{"b64": PNG, "role": f"person {i}"} for i in range(6)]    # > Sol's 4
    job = installed.make_video_job({"model": "h3", "prompt": "Six friends wave.", "references": refs})
    try:
        assert job["request"]["h3_engine"] == "singularity" and len(job["request"]["references"]) == 6
        assert installed._gpu_task_for_engine("h3", job) == "singularity"
    finally:
        _drop(installed, job)
    monkeypatch.setattr(installed._h3ref, "ROUTE_REFERENCES_TO_SINGULARITY", False)
    plain = installed.make_video_job({"model": "h3", "prompt": "One friend.", "references": refs[:1]})
    try:
        assert "h3_engine" not in plain["request"]
        assert installed._gpu_task_for_engine("h3", plain) == "ref2va"
    finally:
        _drop(installed, plain)


def _client(studio):
    client = TestClient(studio.app, base_url="http://127.0.0.1")
    assert client.post("/api/gate", json={"code": studio.ACCESS_CODE}).status_code == 200
    return client


def test_eta_route_prices_each_engine_with_its_spin_up(installed, monkeypatch):
    monkeypatch.setattr(installed, "_picker_residency",
                        lambda max_age_s=5.0: {"ltx25": False, "h3": True, "h3-real": False, "h3-ltx25": True})
    body = _client(installed).get("/api/engines/eta", params={"duration": "12"}).json()
    engines = body["engines"]
    assert body["real_long"] == {"available": True, "max_seconds": 15.083, "label": "Real / Long",
                                 "linger_s": 600, "restore_s": 380}
    assert engines["h3"]["total_s"] == 70 and engines["h3"]["fixed_length"] is True
    assert engines["h3"]["clip_seconds"] == 5.04
    real = engines["h3-real"]
    assert real["spinup_s"] == 430 and real["resident"] is False
    assert "to load Real / Long" in real["text"]
    assert engines["ltx25"]["spinup_s"] > 0
    refs = _client(installed).get("/api/engines/eta", params={"duration": "5", "images": "2"}).json()
    assert refs["engines"]["h3"]["routed_to"] == "h3-real"
    assert "references run on Real / Long" in refs["engines"]["h3"]["text"]


def test_eta_route_hides_real_long_where_it_is_not_available(studio, monkeypatch):
    monkeypatch.setattr(studio, "_picker_residency",
                        lambda max_age_s=5.0: {"ltx25": True, "h3": False, "h3-real": False, "h3-ltx25": False})
    body = _client(studio).get("/api/engines/eta", params={"duration": "8", "images": "1"}).json()
    assert body["real_long"]["available"] is False
    assert "h3-real" not in body["engines"] and "routed_to" not in body["engines"]["h3"]
    assert body["engines"]["h3"]["spinup_s"] == 380
    assert body["engines"]["ltx25"]["spinup_s"] == 0


def test_finished_takes_feed_the_picker(installed, tmp_path, monkeypatch):
    path = tmp_path / "render-timings.json"
    monkeypatch.setattr(installed, "RENDER_TIMINGS_FILE", path)
    monkeypatch.setattr(installed, "ETA_FILE", tmp_path / "eta-stats.json")
    now = time.time()
    job = {"id": "done1", "kind": "video", "engine": "h3", "status": "done", "frames": 124,
           "started": now - 800, "finished": now, "admit_s": 450.0,
           "request": {"h3_engine": "singularity"}}
    installed.eta_record(job)
    import json
    rows = json.loads(path.read_text())["h3-real"]
    assert rows[0]["spinup_s"] == 450.0 and rows[0]["render_s"] == 350.0
