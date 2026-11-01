"""Configured-off Ming-Image engine option (t_e3595a34): selection-surface tests.

CPU-only execution of controller functions; never imports app startup/services.
"""
import ast
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

APP = Path(__file__).parents[1] / "app.py"


def controller_functions(*names, **dependencies):
    tree = ast.parse(APP.read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), dependencies)
    return dependencies


def selectors(monkeypatch, tmp_path=None, enabled="", root=""):
    monkeypatch.setenv("MING_IMAGE_ENABLED", enabled)
    monkeypatch.setenv("MING_IMAGE_MODEL_ROOT", str(root or ""))
    return controller_functions(
        "ming_image_ready", "char_engine",
        os=os, Path=Path,
        kontext_ready=Mock(return_value=False),
    )


def test_ming_is_off_by_default_and_explicit_request_never_substitutes(monkeypatch):
    ns = selectors(monkeypatch)
    assert ns["ming_image_ready"]() is False
    assert ns["char_engine"]("ming") == "ming"
    assert ns["char_engine"]("ming", selfie=True) == "ming"
    assert ns["char_engine"]("auto") == "qwen"
    assert ns["char_engine"]("qwen") == "qwen"


def test_ming_needs_enabled_flag(monkeypatch, tmp_path):
    ns = selectors(monkeypatch, tmp_path, enabled="", root=tmp_path)
    assert ns["ming_image_ready"]() is False
    assert ns["char_engine"]("ming") == "ming"


def test_ming_needs_staged_model_root(monkeypatch, tmp_path):
    missing = tmp_path / "not-staged"
    ns = selectors(monkeypatch, enabled="1", root=missing)
    assert ns["ming_image_ready"]() is False
    assert ns["char_engine"]("ming") == "ming"


def test_ming_resolves_only_when_explicitly_configured(monkeypatch, tmp_path):
    ns = selectors(monkeypatch, enabled="1", root=tmp_path)
    assert ns["ming_image_ready"]() is True
    assert ns["char_engine"]("ming") == "ming"
    # other selections unchanged
    assert ns["char_engine"]("qwen") == "qwen"
    assert ns["char_engine"]("auto") == "qwen"


def test_existing_kontext_qwen_selection_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("MING_IMAGE_ENABLED", "")
    monkeypatch.setenv("MING_IMAGE_MODEL_ROOT", "")
    kontext = Mock(return_value=True)
    ns = controller_functions(
        "ming_image_ready", "char_engine", os=os, Path=Path, kontext_ready=kontext,
    )
    assert ns["char_engine"]("kontext") == "kontext"
    assert ns["char_engine"]("auto", selfie=True) == "kontext"
    assert ns["char_engine"]("auto") == "qwen"


def test_ming_render_refusal_contract():
    ns = controller_functions("ming_render_refusal")
    refusal = ns["ming_render_refusal"]
    msg = refusal("ming")
    assert msg and "not enabled for rendering" in msg
    assert refusal("qwen") is None
    assert refusal("kontext") is None
    assert refusal("auto") is None


def test_every_engine_pick_site_is_fail_closed_for_ming():
    tree = ast.parse(APP.read_text())
    for function in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        calls = [n.func.id for n in ast.walk(function)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        if "char_engine" in calls:
            assert "ming_render_refusal" in calls, function.name


def test_explicit_ming_is_refused_before_any_runtime_or_file_dependency():
    for name in ("run_image", "run_character", "run_selfchar", "run_charremix", "run_storyboard"):
        ns = controller_functions(name, "ming_render_refusal", fail=lambda job, message: message)
        # No filesystem, model writer, GPU or engine dependencies are supplied.
        # Any work before the refusal raises rather than hiding a side effect.
        assert "not enabled for rendering" in ns[name]({"request": {"engine": "ming"}})


def test_storyboard_ming_cast_refuses_before_model_writer():
    ns = controller_functions("run_storyboard", "ming_render_refusal",
                    selectable_characters=lambda: [],
                    resolve_cast_records=lambda ids, chars: [{"engine": "ming"}],
                    fail=lambda job, message: message)
    assert "not enabled for rendering" in ns["run_storyboard"]({"request": {"cast": ["a"]}})
