from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI = (ROOT / "static/index.html").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")


def test_music_and_screenshot_song_use_auto_exact_second_controls():
    for control in (
        "ssDurAuto", "ssDurSlider", "ssDurVal",
        "mDurAuto", "mDurSlider", "mDurVal",
        "mvDurAuto", "mvDurSlider", "mvDurVal",
    ):
        assert f'id="{control}"' in UI
    assert UI.count('min="20" max="180" step="1"') >= 2
    assert 'id="mvDurSlider" min="1" max="180" step="1"' in UI
    assert "musicDurationControl" in UI
    assert "duration_seconds:automatic?null:Number(slider.value)" in UI
    assert "duration_seconds:(MV.automatic||MV.legacyFull)?null:Number($('#mvDurSlider').value)" in UI
    assert "m_len" not in UI
    assert "ss_len" not in UI
    assert "mv_len" not in UI
    assert 'length: str = "auto"' in APP
    assert "_musicvideo_seconds(" in APP

