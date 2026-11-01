import json
from pathlib import Path

import app as media_app
from media_lab_core import local_overlay


ROOT = Path(__file__).parents[1]
UI = ROOT / "static/index.html"

# A synthetic catalog in the shape a studio keeps in its local overlay.
CATALOG = {
    "schema_version": 1,
    "source": {"dataset": "example/catalog", "license": "CC0-1.0"},
    "characters": [
        {"id": "known:0001", "name": "Captain Example", "actor": "A. Performer",
         "franchise": "Example Saga", "status": "good"},
        {"id": "known:0002", "name": "Doctor Sample", "actor": "B. Player",
         "franchise": "Sample Hour", "status": "onthefence"},
        {"id": "known:0003", "name": "Agent Fixture", "actor": "C. Lead",
         "franchise": "Fixture Files", "status": "bad"},
        {"id": "no-prefix", "name": "Dropped", "status": "good"},
    ],
}


def _overlay(monkeypatch, tmp_path, catalog=None):
    local = tmp_path / "config" / "local"
    local.mkdir(parents=True)
    if catalog is not None:
        (local / "known-characters.json").write_text(json.dumps(catalog))
    monkeypatch.setattr(local_overlay, "dirs", lambda: [local])
    return local


def test_public_tree_ships_no_character_or_actor_catalog():
    assert not (ROOT / "config/h3-known-characters.json").exists()
    for path in (ROOT / "config").glob("*.json"):
        assert '"actor"' not in path.read_text(), path


def test_without_a_local_catalog_the_known_list_is_empty(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path)
    assert media_app.known_characters() == []
    assert media_app.known_characters_list() == []


def test_local_catalog_is_metadata_only_and_preserves_status(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, CATALOG)
    rows = media_app.known_characters_list()

    assert [row["id"] for row in rows] == ["known:0001", "known:0002", "known:0003"]
    assert {row["known_status"] for row in rows} == {"good", "onthefence", "bad"}
    assert all(row["known"] is True and row["prompt_only"] is True for row in rows)
    assert all("appearance" not in row and "sheet_url" not in row for row in rows)


def test_known_character_resolves_into_prompt_without_claiming_an_identity_sheet(
        monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, CATALOG)
    captain = next(row for row in media_app.known_characters() if row["name"] == "Captain Example")
    lines = media_app.cast_lines([captain["id"]])

    assert len(lines) == 1
    assert "Captain Example" in lines[0]
    assert "A. Performer" in lines[0]
    assert "Example Saga" in lines[0]
    assert "prompt-only preset" in lines[0]
    assert captain.get("sheet_url") is None


def test_malformed_local_catalog_is_ignored(monkeypatch, tmp_path):
    local = _overlay(monkeypatch, tmp_path)
    (local / "known-characters.json").write_text("{not json")
    assert media_app.known_characters() == []


def test_resolve_cast_records_preserves_custom_first_request_order():
    custom = {"id": "custom-mia", "name": "Mia", "appearance": "Custom Mia"}
    known = {"id": "known:test", "name": "Known Test", "appearance": "Known test prompt"}

    rows = media_app.resolve_cast_records(
        ["known:test", "custom-mia"], chars=[custom, known]
    )

    assert [row["id"] for row in rows] == ["known:test", "custom-mia"]


def test_all_five_creation_flows_use_the_searchable_known_character_picker():
    html = UI.read_text()

    assert "fetch('/api/known-characters')" in html
    assert 'placeholder="Search name, actor, or show…"' in html
    assert "Custom characters appear first" in html
    assert "H3 known entries are prompt-only" in html
    for target in ("#v_cast", "#mv_cast", "#i_cast", "#sb_cast", "#s_cast"):
        assert f"castPicker('{target}'" in html
