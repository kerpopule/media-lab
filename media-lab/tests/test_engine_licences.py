"""Public defaults: engines with personal / non-commercial licences are opt-in.

The rest of the suite runs with every engine enabled (conftest opts the test
process in, like a host owner would). Here the switch is turned off
("none" names no engine) to prove what a fresh public install does.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_lab_core import engine_licences, local_config

REPO = Path(__file__).resolve().parents[1]
PERSONAL = {"h3", "h3-singularity", "yue2", "kontext", "qwen-image-21", "hunyuan-avatar"}


# ---------------------------------------------------------------- the table

def test_personal_set_is_exactly_the_restricted_engines():
    assert {e.id for e in engine_licences.LICENCES.values() if e.personal} == PERSONAL
    for open_id in ("ltx25", "music3", "qwen", "fal", "acestep", "wan22", "chatterbox",
                    "triposr", "birefnet"):
        assert not engine_licences.is_personal(open_id), open_id


def test_aliases_resolve_request_and_host_names():
    assert engine_licences.canonical("h3-ltx25") == "h3"
    assert engine_licences.canonical("qwen-image-21-gpu") == "qwen-image-21"
    assert engine_licences.canonical("fal-video") == "fal"
    assert engine_licences.canonical("music") == "music3"
    assert engine_licences.licence("unknown-engine") is None
    assert engine_licences.enabled("unknown-engine", set()) is True


def test_opt_in_is_per_engine_or_all():
    assert not engine_licences.enabled("yue2", set())
    assert engine_licences.enabled("yue2", {"yue2"})
    assert not engine_licences.enabled("h3-ltx25", {"yue2"})
    assert engine_licences.enabled("h3-ltx25", {"h3"})
    assert all(engine_licences.enabled(e, {"all"}) for e in PERSONAL)
    assert engine_licences.enabled("ltx25", set()) and engine_licences.enabled("music3", set())
    # a host may name an engine by any of its request/host names
    assert engine_licences.enabled("h3", {"h3-ltx25"})
    assert engine_licences.enabled("qwen-image-21", {"qwen-image-21-gpu"})


def test_switch_reads_local_config(monkeypatch):
    monkeypatch.setenv("MEDIA_LAB_PERSONAL_ENGINES", " YuE2 , kontext ")
    assert local_config.personal_engines() == {"yue2", "kontext"}
    assert engine_licences.enabled("yue2") and engine_licences.enabled("kontext")
    assert not engine_licences.enabled("h3")
    view = engine_licences.public_view()
    assert view["badge"] == "personal / non-commercial"
    assert view["engines"]["yue2"]["enabled"] is True
    assert view["engines"]["h3"]["enabled"] is False and view["engines"]["h3"]["personal"] is True
    assert view["engines"]["ltx25"]["enabled"] is True and view["engines"]["ltx25"]["personal"] is False
    assert view["aliases"]["h3-ltx25"] == "h3"


def test_refusal_names_the_badge_the_licence_and_the_switch():
    msg = engine_licences.refusal("yue2")
    assert "personal / non-commercial" in msg and "CC BY-NC 4.0" in msg
    assert "MEDIA_LAB_PERSONAL_ENGINES=yue2" in msg
    assert "4.0). The studio's owner" in msg      # one sentence ends before the next


def test_yue2_notice_matches_the_studio_wording():
    assert engine_licences.licence("yue2").notice == \
        "Non-commercial use only (YuE2 weights are CC BY-NC 4.0)"


def test_every_personal_engine_is_documented():
    doc = (REPO / "docs/ENGINE-LICENCES.md").read_text(encoding="utf-8")
    example = (REPO / "config/local.env.example").read_text(encoding="utf-8")
    assert "MEDIA_LAB_PERSONAL_ENGINES" in doc and "MEDIA_LAB_PERSONAL_ENGINES=" in example
    for engine in PERSONAL:
        assert f"`{engine}`" in doc, engine
    status = (REPO.parent / "docs/CAPABILITY-STATUS.md").read_text(encoding="utf-8")
    assert "personal / non-commercial" in status and "ENGINE-LICENCES.md" in status


# ---------------------------------------------------------------- the studio

@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    home = tmp_path_factory.mktemp("licence-home")
    root = home / "media-lab-simple"
    root.mkdir()
    for name in ("static", "config"):
        (root / name).symlink_to(REPO / name)
    old = {k: os.environ.get(k) for k in ("HOME", "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS")}
    os.environ["HOME"] = str(home)
    os.environ["MEDIA_LAB_DISABLE_BACKGROUND_WORKERS"] = "1"
    spec = importlib.util.spec_from_file_location("licence_test_app", REPO / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["licence_test_app"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    module.pick_next_job = lambda: None
    yield module


@pytest.fixture
def public(monkeypatch):
    """A fresh public install: no personal engine enabled."""
    monkeypatch.setenv("MEDIA_LAB_PERSONAL_ENGINES", "none")


def _client(studio):
    client = TestClient(studio.app, base_url="http://127.0.0.1")
    assert client.post("/api/gate", json={"code": studio.ACCESS_CODE}).status_code == 200
    return client


def test_licence_route_reports_what_is_off(studio, public):
    body = _client(studio).get("/api/engines/licences").json()
    assert body["engines"]["yue2"]["enabled"] is False
    assert body["engines"]["h3"]["enabled"] is False
    assert body["engines"]["music3"]["enabled"] is True


def test_music_defaults_to_music3_and_refuses_yue2(studio, public):
    assert studio.default_music_engine() == "music3"
    client = _client(studio)
    engines = client.get("/api/music/engines").json()
    assert engines["default"] == "music3"
    rows = {e["id"]: e for e in engines["engines"]}
    assert rows["yue2"]["enabled"] is False and rows["yue2"]["personal"] is True
    assert rows["music3"]["enabled"] is True and rows["music3"]["name"] == "MiniMax Music 3"
    refused = client.post("/api/music", json={"vibe": "rainy jazz", "engine": "yue2",
                                              "duration_seconds": 30})
    assert refused.status_code == 403 and "personal / non-commercial" in refused.json()["error"]
    for path, body in (("/api/music/plan", {"style": "jazz"}),
                       ("/api/music/x/rearrange", {"style": "jazz"}),
                       ("/api/music/x/cover", {"style": "jazz"})):
        r = client.post(path, json=body)
        assert r.status_code == 403, (path, r.text)
    ok = client.post("/api/music", json={"vibe": "rainy jazz", "duration_seconds": 30})
    assert ok.status_code == 200, ok.text
    assert ok.json()["engine"] == "music3" and ok.json()["license"] == ""


def test_music_default_follows_the_opt_in(studio, monkeypatch):
    monkeypatch.setenv("MEDIA_LAB_PERSONAL_ENGINES", "yue2")
    assert studio.default_music_engine() == "yue2"


def test_h3_video_is_refused_before_it_queues(studio, public):
    client = _client(studio)
    before = set(studio.jobs)
    for model in ("h3", "h3-ltx25"):
        r = client.post("/api/generate", json={"prompt": "a lighthouse", "model": model})
        assert r.status_code == 400 and "personal / non-commercial" in r.json()["error"], r.text
    assert set(studio.jobs) == before
    with pytest.raises(ValueError, match="MEDIA_LAB_PERSONAL_ENGINES=h3"):
        studio.make_video_job({"prompt": "x", "model": "h3"})


def test_engine_loader_never_boots_a_disabled_engine(studio, public, monkeypatch):
    monkeypatch.setattr(studio, "gpu_recovery_pending", lambda: False)
    calls = []
    monkeypatch.setattr(studio.subprocess, "run", lambda *a, **k: calls.append(a))
    for name in ("h3", "yue2"):
        j = {}
        assert studio._boot_engine(name, j) is False
        assert "personal / non-commercial" in j["detail"]
    assert calls == []


def test_kontext_and_avatar_need_the_opt_in(studio, public, monkeypatch, tmp_path):
    weights = []
    for name in ("KONTEXT_UNET", "KONTEXT_T5", "KONTEXT_CLIP", "KONTEXT_VAE"):
        p = tmp_path / f"{name}.safetensors"
        p.write_bytes(b"x")
        weights.append(p)
        monkeypatch.setattr(studio, name, p)
    assert studio.kontext_ready() is False
    assert studio.char_engine("kontext") == "qwen"
    assert studio.hunyuan_avatar_ready() is False
    client = _client(studio)
    r = client.post("/api/image", json={"prompt": "a portrait", "engine": "kontext"})
    assert r.status_code == 403
    styles = client.get("/api/styles").json()
    assert [e["id"] for e in styles["char_engines"]] == ["auto", "qwen"]
    monkeypatch.setenv("MEDIA_LAB_PERSONAL_ENGINES", "kontext")
    assert studio.kontext_ready() is True


def test_page_badges_and_greys_out_personal_engines():
    html = (REPO / "static/index.html").read_text(encoding="utf-8")
    assert "fetch('/api/engines/licences')" in html
    assert "function licDecorate" in html and ".chip.lic-off" in html
    assert "LICENCES.badge" in html


def test_studio_host_keeps_the_research_image_pack_off(public, tmp_path):
    from media_lab_core.image_host import ImageHost
    host = ImageHost(lambda: None, tmp_path, tmp_path / "image-config.json")
    host.start()
    assert host.thread is None and host.ready is False
    assert "Qwen-Image-2.1" in host.error and "MEDIA_LAB_PERSONAL_ENGINES" in host.error
    assert host.engines() == []
