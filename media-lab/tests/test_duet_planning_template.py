"""Public duet planning preset: CPU-only, with no app startup or private media."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_duet_preset_in_the_real_catalog():
    tree = ast.parse((ROOT / "app.py").read_text())
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "TEMPLATE_LIB" for t in n.targets))
    catalog = ast.literal_eval(assignment.value)
    entry = next(e for _, entries in catalog for e in entries
                 if e[0] == "mv-audio-driven-duet")
    assert len(entry) == 6
    assert "planning" in entry[2].lower()
    assert "resting face" in entry[3]
    assert "native frame rate" in entry[3]
    assert "unqualified" in entry[5].lower()
    assert (ROOT / "static" / "templates" / entry[4]).is_file()


def test_public_recipe_cannot_enable_an_engine_or_cloud_fallback():
    recipe = json.loads((ROOT / "config" / "workflows" / "duet-music-video.json").read_text())
    assert recipe["status"] == "planning_only"
    assert recipe["automatic_execution"] is False
    assert recipe["cloud_fallback"] is False
    assert recipe["audio"]["preserve_original"] is True
    assert {"solo", "shared_handoff", "overlap"} <= set(recipe["qualification"]["required_cases"])
    assert recipe["enhancer"]["qualified"] is False
    assert recipe["enhancer"]["revision"] == "8b284cb76b77bd2ad788e233be35da8ed7fe1598"
    installs = json.loads((ROOT / "config" / "engine-installs.json").read_text())
    assert installs["h3"]["blocked"] and installs["ltx"]["blocked"]


def test_schematic_preview_is_readable_and_has_real_animation():
    from PIL import Image
    with Image.open(ROOT / "static" / "templates" / "duet-music-video-planning.gif") as gif:
        assert gif.n_frames == 16
        assert gif.size == (480, 270)
        first = gif.convert("RGB").tobytes()
        gif.seek(8)
        assert gif.convert("RGB").tobytes() != first
