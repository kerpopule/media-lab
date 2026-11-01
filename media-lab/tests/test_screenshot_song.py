from pathlib import Path

import pytest
from PIL import Image, ImageDraw
import screenshot_song as screenshot_song_module

from screenshot_song import (
    aligned_starts,
    auto_music_seconds,
    boundary_overlap,
    literal_vocal_qa,
    merge_screenshot_blocks,
    render_screenshot_video,
)


def test_adjacent_overlap_is_removed_but_later_repeat_is_preserved():
    merged = merge_screenshot_blocks([
        ["Are you coming tonight?", "Yes", "Bring the blue canoe."],
        ["Yes", "Bring the blue canoe.", "I will bring two limes."],
        ["That sounds good.", "Yes"],
    ])
    assert merged["screenshots"][1]["overlap_removed"] == 2
    assert merged["blocks"] == [
        "Are you coming tonight?", "Yes", "Bring the blue canoe.",
        "I will bring two limes.", "That sounds good.", "Yes",
    ]


def test_long_boundary_allows_tiny_ocr_difference():
    assert boundary_overlap(
        ["Meet me beside the old lighthouse at seven."],
        ["Meet me beside the old lighthouse at  seven."],
    ) == 1


def test_fully_overlapping_screenshot_is_marked_skipped():
    merged = merge_screenshot_blocks([["One", "Two"], ["One", "Two"]])
    assert merged["screenshots"][1]["included"] is False
    assert merged["screenshots"][1]["unique_text"] == ""


def test_whisper_alignment_uses_first_matching_word_of_each_screenshot():
    texts = ["blue canoe", "bring two limes", "see you tonight"]
    words = [
        {"word": "blue", "start": 1.0}, {"word": "canoe", "start": 1.4},
        {"word": "bring", "start": 4.0}, {"word": "two", "start": 4.3},
        {"word": "limes", "start": 4.6}, {"word": "see", "start": 7.0},
        {"word": "you", "start": 7.2}, {"word": "tonight", "start": 7.5},
    ]
    result = aligned_starts(texts, words, 10.0)
    assert result["method"] == "whisper-word-aligned"
    assert result["starts"] == [0.0, 4.0, 7.0]


def test_auto_music_seconds_is_readable_rounded_and_bounded():
    assert auto_music_seconds("") == 90
    assert auto_music_seconds("one short line") == 30
    text = " ".join(f"word{i}" for i in range(150))
    seconds = auto_music_seconds(text, block_count=5)
    assert seconds % 5 == 0
    assert 105 <= seconds <= 115
    assert auto_music_seconds("word " * 1000, block_count=16) == 180


def _words(text: str) -> list[dict]:
    return [{"word": token, "start": index * 0.4}
            for index, token in enumerate(text.split())]


def test_literal_vocal_qa_passes_one_ordered_verbatim_reading():
    target = "Are you coming tonight bring the blue canoe"
    report = literal_vocal_qa(target, _words(target), 60.0, 59.2)
    assert report["passed"] is True
    assert report["extra_words"] == 0
    assert report["omitted_words"] == 0


def test_literal_vocal_qa_rejects_prompt_leak_tail():
    target = "Are you coming tonight bring the blue canoe"
    leaked = target + " preserve every supplied word do not add sung words system prompt"
    report = literal_vocal_qa(target, _words(leaked), 60.0, 59.2)
    assert report["passed"] is False
    assert report["extra_words"] >= 8
    assert report["max_extra_run"] >= 8


def test_literal_vocal_qa_rejects_missing_passage_and_short_song():
    target = "one two three four five six seven eight nine ten eleven twelve"
    report = literal_vocal_qa(target, _words("one two three four five"), 90.0, 50.0)
    assert report["passed"] is False
    assert report["matched_fraction"] < 0.5
    assert report["duration_ok"] is False


def test_literal_vocal_qa_fails_closed_when_duration_is_unavailable():
    target = "one two three four five six"
    for actual in (None, 0, float("nan")):
        report = literal_vocal_qa(target, _words(target), 60.0, actual)
        assert report["passed"] is False
        assert report["duration_ok"] is False
        assert report["actual_duration"] is None
        assert "could not be measured" in report["reason"]


def test_literal_vocal_qa_accepts_spoken_number_equivalents():
    target = "Ten minutes later cross a 6 lane street after 3 years and 30 minutes"
    heard = "10 minutes later cross a six lane street after three years and thirty minutes"
    report = literal_vocal_qa(target, _words(heard), 60.0, 59.2)
    assert report["passed"] is True
    assert report["omitted_words"] == 0
    assert report["extra_words"] == 0


def test_literal_vocal_qa_treats_music_duration_as_a_ceiling_when_requested():
    target = "one complete ordered literal pass"
    report = literal_vocal_qa(
        target, _words(target), 180.0, 88.529, duration_policy="maximum")
    assert report["passed"] is True
    assert report["duration_ok"] is True
    assert report["duration_policy"] == "maximum"


def test_literal_vocal_qa_ignores_only_low_confidence_asr_tail():
    target = "one complete ordered literal pass"
    words = _words(target)
    words.extend([
        {"word": "phantom", "start": 5.0, "probability": 0.2},
        {"word": "tail", "start": 5.2, "probability": 0.3},
    ])
    report = literal_vocal_qa(
        target, words, 60.0, 40.0, duration_policy="maximum")
    assert report["passed"] is True
    assert report["extra_words"] == 0
    assert report["ignored_low_confidence_tail"] == ["phantom", "tail"]


def test_literal_vocal_qa_still_rejects_confident_unapproved_tail():
    target = "one complete ordered literal pass"
    words = _words(target)
    words.extend([
        {"word": "repeat", "start": 5.0, "probability": 0.99},
        {"word": "all", "start": 5.2, "probability": 0.99},
        {"word": "the", "start": 5.4, "probability": 0.99},
        {"word": "lyrics", "start": 5.6, "probability": 0.99},
    ])
    report = literal_vocal_qa(
        target, words, 60.0, 59.0, duration_policy="maximum")
    assert report["passed"] is False
    assert report["max_extra_run"] == 4


def _shot(path: Path, title: str, lines: list[str], color: str) -> None:
    image = Image.new("RGB", (720, 1280), color)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((45, 70, 675, 1210), radius=34, fill="#f7f7f7")
    draw.text((85, 120), title, fill="#222222")
    y = 220
    for line in lines:
        draw.rounded_rectangle((80, y - 18, 640, y + 68), radius=24, fill="#d9fdd3")
        draw.text((110, y), line, fill="#111111")
        y += 130
    image.save(path)


def test_real_ffmpeg_screenshot_video_assembly(tmp_path):
    shots = []
    for index, (title, lines, color) in enumerate([
        ("Part 1", ["Blue canoe", "Meet at seven"], "#24334a"),
        ("Part 2", ["Meet at seven", "Bring two limes"], "#4a3024"),
        ("Part 3", ["See you tonight"], "#294a35"),
    ]):
        path = tmp_path / f"shot-{index}.png"
        _shot(path, title, lines, color)
        shots.append(path)
    audio = tmp_path / "song.wav"
    import subprocess
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
        "sine=frequency=220:duration=6", "-c:a", "pcm_s16le", str(audio),
    ], check=True)
    output, poster = tmp_path / "screenshots.mp4", tmp_path / "poster.jpg"
    receipt = render_screenshot_video(
        shots, [0.0, 2.0, 4.0], 6.0, audio, output, poster,
        orientation="portrait", motion=True, workdir=tmp_path / "work",
    )
    assert output.stat().st_size > 10_000
    assert poster.stat().st_size > 1_000
    assert receipt["width"] == 1080
    assert len(receipt["cues"]) == 3
    assert sorted(path.name for path in (tmp_path / "work").iterdir()) == ["receipt.json"]


def test_failed_screenshot_assembly_removes_partial_public_outputs(tmp_path, monkeypatch):
    image = tmp_path / "shot.png"
    image.write_bytes(b"image")
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    output = tmp_path / "public.mp4"
    poster = tmp_path / "public.jpg"

    def fail_after_partial(_command, timeout=1800):
        output.write_bytes(b"partial")
        poster.write_bytes(b"partial")
        raise RuntimeError("mux failed")

    monkeypatch.setattr(screenshot_song_module, "_run", fail_after_partial)
    with pytest.raises(RuntimeError, match="mux failed"):
        render_screenshot_video([image], [0.0], 6.0, audio, output, poster,
                                workdir=tmp_path / "work")
    assert not output.exists()
    assert not poster.exists()
