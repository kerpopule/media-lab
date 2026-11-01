"""Model-picker time estimates: the measured table, spin-up and refresh from real takes."""
import json

import pytest

from media_lab_core import render_eta

TABLE = render_eta.load_table()


def test_table_holds_the_measured_numbers_with_their_sources():
    engines = TABLE["engines"]
    assert {"ltx25", "h3", "h3-real", "h3-ltx25"} <= set(engines)
    for name, row in engines.items():
        assert row["render_points"] and row["sources"], name
    sol, real = engines["h3"], engines["h3-real"]
    assert sol["render_points"] == [[5.04, 70]] and sol["fixed_seconds"] == 5.04
    assert [5.17, 285] in real["render_points"] and [15.08, 1070] in real["render_points"]
    assert real["spinup_s"] == 430 and sol["spinup_s"] == 380
    assert real["max_seconds"] == 15.08


def test_warm_sol_is_about_seventy_seconds_whatever_length_is_asked():
    for seconds in (3, 5, 12, 20):
        est = render_eta.estimate("h3", seconds, resident=True, table=TABLE)
        assert est["total_s"] == 70 and est["clip_seconds"] == 5.04 and est["fixed_length"]
        assert est["spinup_s"] == 0 and est["text"] == "about 70 s"


def test_cold_engines_include_their_spin_up():
    cold = render_eta.estimate("h3", 5, resident=False, table=TABLE)
    assert cold["total_s"] == 70 + 380 and cold["spinup_s"] == 380
    assert "includes ~6½ min to load Cinematic" in cold["text"]
    real = render_eta.estimate("h3-real", 5.17, resident=False, table=TABLE)
    assert real["render_s"] == 285 and real["spinup_s"] == 430
    assert real["total_s"] == 715 and "to load Real / Long" in real["text"]


def test_real_long_scales_with_length_and_references():
    table = TABLE
    five = render_eta.estimate("h3-real", 5.17, resident=True, table=table)["render_s"]
    ten = render_eta.estimate("h3-real", 10, resident=True, table=table)["render_s"]
    fifteen = render_eta.estimate("h3-real", 15.08, resident=True, table=table)["render_s"]
    assert five == 285 and fifteen == 1070 and five < ten < fifteen
    over = render_eta.estimate("h3-real", 30, resident=True, table=table)
    assert over["clip_seconds"] == 15.08                       # capped to the trained length
    two_max = render_eta.estimate("h3-real", 5.17, resident=True, image_refs=2, detail="max",
                                  table=table)["render_s"]
    assert two_max == 485                                      # measured 488 s
    long_ref = render_eta.estimate("h3-real", 15.08, resident=True, image_refs=1, detail="max",
                                   table=table)["render_s"]
    assert abs(long_ref - 1170) <= 25                          # measured 1170 s
    video = render_eta.estimate("h3-real", 5.17, resident=True, video_refs=1, table=table)["render_s"]
    assert video > five


def test_ltx_interpolates_between_measured_lengths():
    at = lambda s: render_eta.estimate("ltx25", s, resident=True, table=TABLE)["render_s"]
    assert at(5.04) == 170 and at(14.7) == 320
    assert 170 < at(6) < 250 and at(20) == 400


def test_completed_takes_refresh_the_estimate(tmp_path):
    path = tmp_path / "render-timings.json"
    for total in (500, 510, 490):   # real warm 5 s takes ran ~1.75x the table
        assert render_eta.record(path, "h3-real", seconds=124 / 24, total_s=total, table=TABLE)
    est = render_eta.estimate("h3-real", 5.17, resident=True, table=TABLE, timings_path=path)
    assert 495 <= est["render_s"] <= 505 and est["basis"].startswith("measured + 3")
    # cold takes calibrate the spin-up separately
    for spin in (800, 820):
        render_eta.record(path, "h3-real", seconds=5.17, total_s=spin + 285, spinup_s=spin, table=TABLE)
    assert render_eta.estimate("h3-real", 5.17, resident=False, table=TABLE,
                               timings_path=path)["spinup_s"] == 430      # 2 samples: not yet
    render_eta.record(path, "h3-real", seconds=5.17, total_s=1100, spinup_s=810, table=TABLE)
    spin = render_eta.estimate("h3-real", 5.17, resident=False, table=TABLE, timings_path=path)["spinup_s"]
    assert 800 <= spin <= 820


def test_one_odd_take_cannot_run_away_with_the_estimate(tmp_path):
    path = tmp_path / "t.json"
    for total in (70 * 50, 70 * 60, 70 * 40):
        render_eta.record(path, "h3", seconds=5, total_s=total, table=TABLE)
    est = render_eta.estimate("h3", 5, resident=True, table=TABLE, timings_path=path)
    assert est["render_s"] == 140                               # clamped at 2x


def test_bad_rows_are_not_recorded(tmp_path):
    path = tmp_path / "t.json"
    assert render_eta.record(path, "h3", seconds=5, total_s=0, table=TABLE) is None
    assert render_eta.record(path, "h3", seconds=5, total_s=100, spinup_s=200, table=TABLE) is None
    assert render_eta.record(path, "nope", seconds=5, total_s=100, table=TABLE) is None
    assert not path.exists()


def test_history_is_bounded(tmp_path):
    path = tmp_path / "t.json"
    for i in range(render_eta.KEEP_PER_ENGINE + 10):
        render_eta.record(path, "ltx25", seconds=5, total_s=100 + i, table=TABLE)
    assert len(json.loads(path.read_text())["ltx25"]) == render_eta.KEEP_PER_ENGINE


@pytest.mark.parametrize("seconds,text", [(45, "50 s"), (70, "70 s"), (150, "2½ min"),
                                          (285, "5 min"), (715, "12 min"), (1170, "20 min")])
def test_human_times(seconds, text):
    assert render_eta.human(seconds) == text
