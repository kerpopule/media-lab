from pathlib import Path

UI = Path(__file__).parents[1] / "static/index.html"


def test_character_section_exposes_local_ai_presenter_workflow():
    html = UI.read_text()

    assert "AI Presenter / Talking Head" in html
    assert "stay on this studio machine" in html
    assert "never interrupt an active render" in html
    assert 'id="p_cast"' in html
    assert 'id="p_go"' in html
    assert "sayIt(cid)" in html


def test_presenter_take_uses_existing_local_queue_and_engine_choice():
    html = UI.read_text()

    assert 'id="sy_engine"' in html
    assert 'data-v="ltx25"' in html
    assert 'data-v="h3"' in html
    assert "engine:sel('#sy_engine')||'ltx25'" in html
    assert "fetch(`/api/characters/${SY.cid}/say`" in html
    assert "The take waits its turn behind any active render." in html


def test_presenter_ui_does_not_import_or_call_the_reference_project():
    html = UI.read_text()

    assert "cclank" not in html
    assert "lanshu" not in html.lower()
    assert "create-ai-presenter-video" not in html
