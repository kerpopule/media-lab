"""The gitignored config/local/ overlay: a studio's own looks, templates and notes."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_lab_core import local_config, local_overlay

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def overlay(monkeypatch, tmp_path):
    local = tmp_path / "config" / "local"
    local.mkdir(parents=True)
    monkeypatch.setattr(local_overlay, "dirs", lambda: [local])
    return local


# ---------------------------------------------------------------- folders

def test_overlay_dirs_prefer_the_data_root_and_skip_missing(monkeypatch, tmp_path):
    home, source = tmp_path / "home", tmp_path / "src"
    (home / "config" / "local").mkdir(parents=True)
    (source / "config" / "local").mkdir(parents=True)
    monkeypatch.setenv("MEDIA_LAB_HOME", str(home))
    monkeypatch.setattr(local_config, "SOURCE_ROOT", source)
    assert local_config.overlay_dirs() == [home / "config" / "local", source / "config" / "local"]
    monkeypatch.setattr(local_config, "SOURCE_ROOT", home)          # a deployed tree: one folder
    assert local_config.overlay_dirs() == [home / "config" / "local"]
    monkeypatch.setenv("MEDIA_LAB_HOME", str(tmp_path / "nowhere"))
    monkeypatch.setattr(local_config, "SOURCE_ROOT", tmp_path / "nowhere")
    assert local_config.overlay_dirs() == []


def test_overlay_folder_is_gitignored_except_its_readme():
    def ignored(rel):
        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO).returncode == 0
    assert ignored("config/local/themes.json")
    assert ignored("config/local/prompt-templates/x.json")
    assert not ignored("config/local/README.md")


def test_deploy_never_writes_or_deletes_the_overlay():
    text = (REPO / "tools" / "deploy-spark.sh").read_text()
    protect = text[text.index("PROTECT=("):text.index(")", text.index("PROTECT=("))]
    assert "'/config/local'" in protect and "'/prompt-templates'" in protect


# ---------------------------------------------------------------- themes

def test_themes_are_validated_and_rendered_as_css(overlay):
    (overlay / "themes.json").write_text(json.dumps({"themes": [
        {"id": "harbour", "label": "Harbour", "accent": "#43CECA", "ink": "#0F0F11",
         "vars": {"--ink-2": "#141417", "--sheen": "linear-gradient(180deg,#fff,#000)",
                  "--bad": "red;}body{display:none", "--img": "url(https://x.test/a.png)",
                  "not-a-var": "#000"}},
        {"id": "linen", "label": "Linen", "accent": "#0A7A83", "ink": "#FFF8E7", "light": True},
        {"id": "Bad Id", "accent": "#fff", "ink": "#000"},
        {"id": "nocolor", "accent": "teal", "ink": "#000"},
        {"id": "harbour", "accent": "#000", "ink": "#000"},
    ]}))
    themes = local_overlay.themes()
    assert [t["id"] for t in themes] == ["harbour", "linen"]
    harbour = themes[0]
    assert harbour["vars"] == {"--ink-2": "#141417", "--sheen": "linear-gradient(180deg,#fff,#000)",
                               "--ink": "#0F0F11", "--gold": "#43CECA"}
    css = local_overlay.themes_css()
    assert ':root[data-theme="harbour"]{' in css and "display:none" not in css and "url(" not in css
    assert ':root[data-theme="linen"] :is(' in css            # light-ground repaint
    assert local_overlay.theme_ink("linen") == "#FFF8E7" and local_overlay.theme_ink("x") is None


def test_malformed_overlay_json_is_skipped(overlay):
    (overlay / "themes.json").write_text("{nope")
    assert local_overlay.themes() == [] and local_overlay.themes_css() == ""
    (overlay / "themes.json").write_text(json.dumps(
        [{"id": "odd", "accent": "#123456", "ink": "#000000", "vars": ["--ink", "#fff"]}]))
    assert [t["vars"] for t in local_overlay.themes()] == [{"--ink": "#000000", "--gold": "#123456"}]
    (overlay / "templates.json").write_text(json.dumps({"groups": [{"group": "G", "templates": 7}]}))
    assert local_overlay.template_groups(lambda t: "") == []


# ---------------------------------------------------------------- templates

def test_template_groups_accept_inline_and_file_prompts(overlay):
    (overlay / "prompt-templates").mkdir()
    (overlay / "prompt-templates" / "long-one.json").write_text(json.dumps(
        {"template_id": "long-one", "prompt": "A long attributed prompt."}))
    (overlay / "templates.json").write_text(json.dumps({"groups": [
        {"group": "Ours", "templates": [
            {"id": "inline-one", "emoji": "🌧️", "label": "Inline", "prompt": "Rainy night. ",
             "gif": "inline-one.gif", "description": "d"},
            {"id": "long-one", "label": "From file", "prompt_template": "long-one"},
            {"id": "missing-file", "prompt_template": "nope"},
            {"id": "../escape", "prompt": "x"},
            {"id": "no-prompt"},
            {"id": "bad-gif", "prompt": "x", "gif": "../../etc/passwd"},
        ]},
        {"group": "", "templates": [{"id": "orphan", "prompt": "x"}]},
    ]}))

    def load(template_id):
        path = local_overlay.prompt_template_path(template_id)
        if path is None:
            raise RuntimeError(f"Missing prompt template: {template_id}")
        return json.loads(path.read_text())["prompt"]

    groups = local_overlay.template_groups(load)
    assert [g for g, _ in groups] == ["Ours"]
    rows = {row[0]: row for row in groups[0][1]}
    assert set(rows) == {"inline-one", "long-one", "bad-gif"}
    assert rows["inline-one"][3] == "Rainy night. " and rows["inline-one"][4] == "inline-one.gif"
    assert rows["long-one"][3] == "A long attributed prompt."
    assert rows["bad-gif"][4] == ""


def test_asset_names_cannot_leave_the_overlay(overlay):
    (overlay / "templates").mkdir()
    (overlay / "templates" / "ok.gif").write_bytes(b"GIF89a")
    assert local_overlay.asset_path("ok.gif") == overlay / "templates" / "ok.gif"
    for bad in ("../ok.gif", "templates/ok.gif", ".hidden.gif", "ok.exe", ""):
        assert local_overlay.asset_path(bad) is None, bad


def test_sparky_notes(overlay):
    assert local_overlay.sparky_notes() == ""
    (overlay / "sparky.md").write_text("  The studio's regular performer is Ava.\n")
    assert local_overlay.sparky_notes() == "The studio's regular performer is Ava."


# ---------------------------------------------------------------- the studio

@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    home = tmp_path_factory.mktemp("overlay-home")
    root = home / "media-lab-simple"
    root.mkdir()
    for name in ("static", "config"):
        (root / name).symlink_to(REPO / name)
    old = {k: os.environ.get(k) for k in ("HOME", "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS")}
    os.environ["HOME"] = str(home)
    os.environ["MEDIA_LAB_DISABLE_BACKGROUND_WORKERS"] = "1"
    spec = importlib.util.spec_from_file_location("overlay_test_app", REPO / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["overlay_test_app"] = module
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


def _client(studio):
    client = TestClient(studio.app, base_url="http://127.0.0.1")
    assert client.post("/api/gate", json={"code": studio.ACCESS_CODE}).status_code == 200
    return client


def test_studio_serves_overlay_looks_and_previews(studio, overlay):
    (overlay / "themes.json").write_text(json.dumps([
        {"id": "harbour", "label": "Harbour", "accent": "#43CECA", "ink": "#0F0F11"}]))
    (overlay / "templates").mkdir()
    (overlay / "templates" / "ours.gif").write_bytes(b"GIF89a-test")
    client = _client(studio)
    css = client.get("/local/themes.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert ':root[data-theme="harbour"]' in css.text
    assert client.get("/api/local/themes").json() == {"themes": [
        {"id": "harbour", "label": "Harbour", "accent": "#43CECA", "ink": "#0F0F11"}]}
    assert client.get("/manifest.json?theme=harbour").json()["theme_color"] == "#0F0F11"
    assert client.get("/manifest.json?theme=gone").json()["theme_color"] == "#0B0806"
    assert client.get("/local/templates/ours.gif").content == b"GIF89a-test"
    assert client.get("/local/templates/nope.gif").status_code == 404
    assert studio._template_preview_url("ours.gif") == "/local/templates/ours.gif"
    assert studio._template_preview_url("h3-storyboard-sequence.gif") == \
        "/static/templates/h3-storyboard-sequence.gif"
    assert studio._template_preview_url("papercraft-stop-motion-explainer.gif") == ""


def test_overlay_routes_are_behind_the_gate(studio):
    for path in ("/local/themes.css", "/api/local/themes", "/local/templates/x.gif",
                 "/api/engines/licences"):
        assert not studio.gate_exempt(studio.gate_path(path)), path


def test_public_studio_ships_no_private_templates_or_themes(studio):
    ids = {tid for _, entries in studio.TEMPLATE_LIB for tid, *_ in entries}
    assert "h3-storyboard-sequential-beats" in ids and "papercraft-explain" in ids
    assert not (REPO / "prompt-templates").exists()
    gifs = sorted(p.name for p in (REPO / "static/templates").iterdir())
    assert gifs == ["duet-music-video-planning.gif", "h3-storyboard-sequence.gif"]
    assert set(studio.THEME_INK) == {"", "coagent", "ocean", "emerald", "violet", "paper"}
    html = (REPO / "static/index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/local/themes.css">' in html
    assert "fetch('/api/local/themes')" in html


def test_duet_planning_preset_is_served_with_its_schematic(studio):
    client = _client(studio)
    response = client.get("/api/styles")
    assert response.status_code == 200
    preset = next(t for group in response.json()["templates"] for t in group["templates"]
                  if t["id"] == "mv-audio-driven-duet")
    assert "planning" in preset["label"].lower()
    assert "unqualified" in preset["description"].lower()
    assert preset["gif"] == "/static/templates/duet-music-video-planning.gif"
    preview = client.get(preset["gif"])
    assert preview.status_code == 200 and preview.content.startswith(b"GIF89a")


def test_vapid_contact_is_configurable_and_never_a_person_by_default(monkeypatch):
    monkeypatch.delenv("MEDIA_LAB_VAPID_SUBJECT", raising=False)
    monkeypatch.setattr(local_config, "env_file_candidates", lambda: [])
    assert local_config.vapid_subject() == local_config.DEFAULT_VAPID_SUBJECT
    assert local_config.DEFAULT_VAPID_SUBJECT.startswith("https://")
    monkeypatch.setenv("MEDIA_LAB_VAPID_SUBJECT", "mailto:ops@example.com")
    assert local_config.vapid_subject() == "mailto:ops@example.com"
    monkeypatch.setenv("MEDIA_LAB_VAPID_SUBJECT", "not-a-contact")
    assert local_config.vapid_subject() == local_config.DEFAULT_VAPID_SUBJECT
    assert 'vapid_claims={"sub": local_config.vapid_subject()}' in (REPO / "app.py").read_text()


def test_qualification_cast_is_the_studios_own_list(monkeypatch):
    import chat_operator
    guard = "Qualification framing"
    monkeypatch.delenv("MEDIA_LAB_QUALIFICATION_CAST", raising=False)
    monkeypatch.setattr(local_config, "env_file_candidates", lambda: [])
    assert guard not in chat_operator.StudioOperator._qualification_text("Ava waves", ["Ava"])
    monkeypatch.setenv("MEDIA_LAB_QUALIFICATION_CAST", "ava, Mia")
    assert guard in chat_operator.StudioOperator._qualification_text("Ava waves", ["Ava"])
    assert guard not in chat_operator.StudioOperator._qualification_text("Leo waves", ["Leo"])
