import importlib.util
import os
import sys
from pathlib import Path

import pytest

from screenshot_song import (
    EXACT_SONG_MAX_WORDS,
    format_exact_song_lyrics,
    literal_vocal_qa,
    lyric_tokens,
    melodic_delivery_qa,
    plan_exact_song_parts,
    song_first_auto_seconds,
    song_first_lyrics_qa,
    song_first_word_budget,
)


ROOT = Path(__file__).resolve().parent.parent


def _words(text: str, probability: float = 0.99) -> list[dict]:
    return [
        {"word": token, "start": index * 0.4, "probability": probability}
        for index, token in enumerate(text.split())
    ]


def test_song_first_auto_duration_tracks_selected_text_and_scenes():
    short = "word " * 80
    long = "word " * 305
    assert song_first_auto_seconds("word " * 10, block_count=1, adapted=True) == 35
    assert song_first_auto_seconds("word " * 55, block_count=1, adapted=True) == 70
    assert song_first_auto_seconds(short, block_count=3) < song_first_auto_seconds(
        long, block_count=7
    )
    assert song_first_auto_seconds(long, block_count=7) == 155
    assert song_first_auto_seconds("word " * 1000, block_count=16) == 180


def test_song_first_budget_compresses_prose_to_singable_size():
    assert song_first_word_budget("word " * 305) == 146
    assert song_first_word_budget("word " * 40) == 60
    assert song_first_word_budget("word " * 1000) == 180


def test_song_first_lyrics_require_short_lines_and_written_out_hook():
    chorus = "Today today what could go wrong\nI made it to noon so I wrote this song"
    lyrics = f"""[Verse 1]
Wrong name wrong text before the day began
Coffee in my hand and a mystery man

[Chorus]
{chorus}

[Verse 2]
I saved a stranger while my bicycle disappeared
The light turned green and the punchline reappeared

[Chorus]
{chorus}

[Bridge]
Every little disaster finds a rhythm before long

[Outro]
Today today so I wrote this song"""
    report = song_first_lyrics_qa(lyrics)
    assert report["passed"] is True
    assert report["chorus_count"] == 2
    assert report["verse_count"] == 2

    prose = "[Verse 1]\n" + "word " * 70
    rejected = song_first_lyrics_qa(prose)
    assert rejected["passed"] is False
    assert "two written-out choruses" in rejected["reason"]


def test_screenshot_song_duration_is_a_maximum_and_numbers_are_spoken_equivalents():
    target = "I waited 10 minutes across a 6 lane street for 30 minutes"
    heard = "I waited ten minutes across a six lane street for thirty minutes"
    report = literal_vocal_qa(
        target,
        _words(heard),
        requested_duration=180,
        actual_duration=88.5,
        duration_policy="maximum",
    )
    assert report["passed"] is True
    assert report["duration_policy"] == "maximum"


def test_song_delivery_tolerates_scattered_melodic_asr_errors_but_not_leaks():
    words = (
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
        "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
        "xray yankee zulu amber birch cedar dune ember frost grove harbor ivory jasper "
        "kindle lantern meadow nectar orbit pebble quartz river spruce timber valley "
        "willow xenon"
    ).split()
    heard = [word for index, word in enumerate(words) if index not in {5, 17, 29, 41}]
    for index, noise in ((10, "glimmer"), (20, "shimmer"), (30, "singer")):
        heard.insert(index, noise)
    transcript = [{"word": word, "probability": 0.96} for word in heard]
    literal = literal_vocal_qa(" ".join(words), transcript, delivery_policy="literal")
    sung = literal_vocal_qa(" ".join(words), transcript, delivery_policy="song")
    assert literal["passed"] is False
    assert sung["passed"] is True
    assert sung["reason"] == "song-first vocal contract passed"

    leaked = transcript + [
        {"word": word, "probability": 0.99}
        for word in ("system prompt preserve every supplied word")
    ]
    rejected = literal_vocal_qa(" ".join(words), leaked, delivery_policy="song")
    assert rejected["passed"] is False
    assert rejected["max_extra_run"] > rejected["allowed_extra_run"]


def test_exact_delivery_retries_two_substituted_words():
    target = " ".join(f"word{i}" for i in range(99))
    heard = target.split()
    heard[0] = "day"
    heard[70] = "girls"
    report = literal_vocal_qa(
        target,
        [{"word": word, "probability": 0.99} for word in heard],
        requested_duration=110,
        actual_duration=41,
        duration_policy="maximum",
        delivery_policy="exact",
    )
    assert report["passed"] is False
    assert report["omitted_words"] == 2
    assert report["extra_words"] == 2
    assert report["allowed_omissions"] == 1
    assert report["allowed_extras"] == 1


def test_exact_delivery_allows_one_isolated_asr_uncertainty():
    target = " ".join(f"word{i}" for i in range(99))
    heard = target.split()
    heard[0] = "day"
    report = literal_vocal_qa(
        target,
        [{"word": word, "probability": 0.99} for word in heard],
        requested_duration=110,
        actual_duration=41,
        duration_policy="maximum",
        delivery_policy="exact",
    )
    assert report["passed"] is True
    assert report["reason"] == "exact lyric contract passed"


def test_exact_lyrics_are_reflowed_into_short_melodic_lines_without_word_changes():
    source = (
        "Today my phone showed a strange message meant for someone else. "
        "I read every word twice and the wrong name still glowed on screen. "
        "Then I called my friend and heard the answer through the wall."
    )
    formatted = format_exact_song_lyrics(source)
    lyric_lines = [line for line in formatted.splitlines()
                   if line and not line.startswith("[")]
    assert lyric_tokens(" ".join(lyric_lines)) == lyric_tokens(source)
    assert all(len(lyric_tokens(line)) <= 7 for line in lyric_lines)
    assert formatted.count("[Verse") >= 2


def test_melodic_delivery_rejects_user_rejected_recitation_metrics():
    rejected = melodic_delivery_qa({
        "pitch_iqr_semitones": 5.425,
        "pitch_p90_p10_semitones": 14.987,
        "analyzed_seconds": 40.96,
    })
    assert rejected["passed"] is False
    assert "recitation" in rejected["reason"]

    melodic = melodic_delivery_qa({
        "pitch_iqr_semitones": 11.359,
        "pitch_p90_p10_semitones": 19.031,
        "analyzed_seconds": 58.793,
    })
    assert melodic["passed"] is True


def test_low_confidence_whisper_tail_is_ignored_but_confident_leak_is_not():
    target = "today the bicycle disappeared"
    low_tail = _words(target) + _words("phantom ending words", probability=0.2)
    accepted = literal_vocal_qa(
        target, low_tail, requested_duration=90, actual_duration=60,
        duration_policy="maximum",
    )
    assert accepted["passed"] is True
    assert accepted["ignored_low_confidence_tail"] == ["phantom", "ending", "words"]

    confident = _words(target + " system prompt words leaked loudly")
    rejected = literal_vocal_qa(
        target, confident, requested_duration=90, actual_duration=60,
        duration_policy="maximum",
    )
    assert rejected["passed"] is False


def test_exact_planner_balances_normal_screenshots_without_rewriting():
    weights = [55, 39, 52, 29, 39, 49, 44]
    frames = [
        {"source": f"/media/scene-{index}.png",
         "text": " ".join(f"s{index}w{word}" for word in range(count))}
        for index, count in enumerate(weights, start=1)
    ]
    parts = plan_exact_song_parts(frames)
    assert [part["word_count"] for part in parts] == weights
    assert all(part["word_count"] <= EXACT_SONG_MAX_WORDS for part in parts)
    assert [row["source"] for part in parts for row in part["frames"]] == [
        row["source"] for row in frames
    ]
    assert [token for part in parts for token in lyric_tokens(part["lyrics"])] == \
        lyric_tokens("\n\n".join(row["text"] for row in frames))


def test_exact_planner_splits_one_oversized_screenshot_at_word_boundaries():
    text = " ".join(f"word{index}" for index in range(225))
    parts = plan_exact_song_parts([{"source": "/media/one.png", "text": text}])
    rows = [row for part in parts for row in part["frames"]]
    assert len(parts) == 4
    assert all(part["word_count"] <= EXACT_SONG_MAX_WORDS for part in parts)
    assert {row["source"] for row in rows} == {"/media/one.png"}
    assert [token for part in parts for token in lyric_tokens(part["lyrics"])] == \
        lyric_tokens(text)


def test_app_and_ui_wire_exact_auto_fit_as_the_default():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    ui = (ROOT / "static/index.html").read_text(encoding="utf-8")
    for contract in (
        'screenshot_song_mode: str = "exact"',
        '"source_material": part_lyrics',
        'duration_policy="maximum"',
        "plan_exact_song_parts(",
        'submit_job("screenshotsong"',
    ):
        assert contract in app
    for contract in (
        'id="ss_mode"',
        'data-v="exact"',
        'data-v="verbatim"',
        'class="ss_include"',
        "exactAutoSeconds",
        'id="ss_auto_plan"',
        "SS.jobs",
    ):
        assert contract in ui
    assert "selects the strongest moments" not in ui


@pytest.fixture(scope="module")
def media_app(tmp_path_factory):
    home = tmp_path_factory.mktemp("song-first-api-home")
    root = home / "media-lab-simple"
    root.mkdir()
    for name in ("static", "config"):
        (root / name).symlink_to(ROOT / name)
    old = {key: os.environ.get(key) for key in
           ("HOME", "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS")}
    os.environ["HOME"] = str(home)
    os.environ["MEDIA_LAB_DISABLE_BACKGROUND_WORKERS"] = "1"
    spec = importlib.util.spec_from_file_location("song_first_test_app", ROOT / "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["song_first_test_app"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module


def _prepare_endpoint(media_app, tmp_path, monkeypatch, captured):
    manifest = "abcdef123456"
    source = tmp_path / f"ss_{manifest}_01.png"
    source.write_bytes(b"image")
    review_root = tmp_path / "reviews"
    (review_root / manifest).mkdir(parents=True)
    monkeypatch.setattr(media_app, "SCREENSHOT_SONGS_DIR", review_root)
    boards_file = tmp_path / "storyboards.json"
    chars_file = tmp_path / "characters.json"
    chars_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(media_app, "BOARDS_FILE", boards_file)
    monkeypatch.setattr(media_app, "CHARS_FILE", chars_file)
    monkeypatch.setattr(media_app, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(media_app, "jobs", {})
    monkeypatch.setattr(media_app, "queue", [])
    monkeypatch.setattr(
        media_app, "media_path",
        lambda value: source if Path(str(value)).name == source.name else None,
    )
    monkeypatch.setattr(media_app, "engine_up", lambda _name: False)
    monkeypatch.setattr(media_app, "eta_estimate", lambda _job: 7)

    def submit(kind, request, extra=None):
        jobs = captured.setdefault("jobs", [])
        job_id = f"exactsong{len(jobs) + 1}"
        job = {"id": job_id, "kind": kind, "request": request,
               "extra": extra}
        jobs.append(job)
        media_app.jobs[job_id] = job
        return job

    monkeypatch.setattr(media_app, "submit_job", submit)
    return manifest, source


def test_endpoint_defaults_to_exact_multi_job_and_keeps_auto_unresolved(
        media_app, tmp_path, monkeypatch):
    captured = {}
    manifest, source = _prepare_endpoint(media_app, tmp_path, monkeypatch, captured)
    result = media_app.screenshot_song_make(media_app.ScreenshotSongReq(
        manifest_id=manifest,
        screenshots=[media_app.ScreenshotSongFrame(
            source=f"/media/{source.name}",
            text="A long strange day became a bright funny story " * 20,
        )],
    ))
    assert result["id"] == "exactsong1"
    assert result["automatic"] is True
    assert result["parts"] == 3
    assert result["ids"] == ["exactsong1", "exactsong2", "exactsong3"]
    jobs = captured["jobs"]
    assert all(row["kind"] == "screenshotsong" for row in jobs)
    assert all(row["request"]["screenshot_song_mode"] == "exact" for row in jobs)
    assert all(row["request"]["literal_lyrics"] is True for row in jobs)
    assert all(row["request"]["lyrics"] == row["request"]["source_material"]
               for row in jobs)
    assert all(row["request"]["duration_seconds"] is None for row in jobs)
    assert all(row["request"]["auto_duration_estimate"] is not None for row in jobs)
    original = lyric_tokens("A long strange day became a bright funny story " * 20)
    planned = [token for row in jobs for token in lyric_tokens(row["request"]["lyrics"])]
    assert planned == original
    assert [row["request"]["part_index"] for row in jobs] == [1, 2, 3]
    assert {row["request"]["part_count"] for row in jobs} == {3}


def test_legacy_song_request_maps_to_exact_without_adaptation(
        media_app, tmp_path, monkeypatch):
    captured = {}
    manifest, source = _prepare_endpoint(media_app, tmp_path, monkeypatch, captured)
    text = "These are the exact reviewed words and must never be paraphrased."
    media_app.screenshot_song_make(media_app.ScreenshotSongReq(
        manifest_id=manifest,
        screenshot_song_mode="song",
        screenshots=[media_app.ScreenshotSongFrame(
            source=f"/media/{source.name}", text=text,
        )],
    ))
    request = captured["jobs"][0]["request"]
    assert request["screenshot_song_mode"] == "exact"
    assert request["lyrics"] == text
    assert request["source_material"] == text
    assert request["literal_lyrics"] is True


def test_multiframe_exact_series_gets_one_group_storyboard_before_render(
        media_app, tmp_path, monkeypatch):
    import json

    captured = {}
    manifest, first = _prepare_endpoint(media_app, tmp_path, monkeypatch, captured)
    second = tmp_path / f"ss_{manifest}_02.png"
    second.write_bytes(b"image-two")
    sources = {first.name: first, second.name: second}
    monkeypatch.setattr(
        media_app, "media_path",
        lambda value: sources.get(Path(str(value)).name),
    )
    result = media_app.screenshot_song_make(media_app.ScreenshotSongReq(
        manifest_id=manifest,
        screenshots=[
            media_app.ScreenshotSongFrame(
                source=f"/media/{first.name}", text="First exact scene with a melody."),
            media_app.ScreenshotSongFrame(
                source=f"/media/{second.name}", text="Second exact scene with a chorus."),
        ],
    ))

    assert result["board_id"]
    assert {row["board_id"] for row in result["jobs"]} == {result["board_id"]}
    assert {job["board_id"] for job in captured["jobs"]} == {result["board_id"]}
    boards = json.loads((tmp_path / "storyboards.json").read_text(encoding="utf-8"))
    assert len(boards) == 1
    board = boards[0]
    assert board["id"] == result["board_id"]
    assert board["source_kind"] == "screenshotseries"
    assert board["source_job_id"].startswith(f"{manifest}-")
    assert board["song_id"] == result["id"]
    assert [beat["description"] for beat in board["beats"]] == [
        "First exact scene with a melody.",
        "Second exact scene with a chorus.",
    ]


def test_endpoint_preserves_every_word_mode_and_manual_override(
        media_app, tmp_path, monkeypatch):
    captured = {}
    manifest, source = _prepare_endpoint(media_app, tmp_path, monkeypatch, captured)
    text = "Sing every reviewed word once and in order."
    result = media_app.screenshot_song_make(media_app.ScreenshotSongReq(
        manifest_id=manifest,
        screenshot_song_mode="verbatim",
        length="custom",
        duration_seconds=75,
        screenshots=[media_app.ScreenshotSongFrame(
            source=f"/media/{source.name}", text=text,
        )],
    ))
    assert result["duration_seconds"] == 75
    assert result["automatic"] is False
    request = captured["jobs"][0]["request"]
    assert request["lyrics"] == text
    assert request["literal_lyrics"] is True
    assert request["duration_seconds"] == 75


def test_exact_worker_preserves_words_resolves_auto_and_runs_director_qa(
        media_app, tmp_path, monkeypatch):
    import builtins
    from contextlib import nullcontext
    from types import SimpleNamespace

    jobs_dir = tmp_path / "jobs"
    media_dir = tmp_path / "media"
    comfy_dir = tmp_path / "comfy"
    (comfy_dir / "output/music3-lab").mkdir(parents=True)
    jobs_dir.mkdir()
    media_dir.mkdir()
    monkeypatch.setattr(media_app, "JOBS_DIR", jobs_dir)
    monkeypatch.setattr(media_app, "MEDIA", media_dir)
    monkeypatch.setattr(media_app, "COMFY_MUSIC_DIR", comfy_dir)
    monkeypatch.setattr(media_app, "ensure_engine", lambda *_args: "up")
    monkeypatch.setattr(media_app, "gpu_operation", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(media_app, "gpu_render_ready", lambda *_args: object())
    monkeypatch.setattr(media_app, "touch_engine", lambda *_args: None)
    monkeypatch.setattr(media_app.fcntl, "flock", lambda *_args: None)

    exact = (
        "Today my phone showed a strange message meant for someone else. "
        "I read every word twice and the wrong name still glowed on screen. "
        "Then I called my friend and heard the answer through the wall."
    )
    tagged = f"[Verse 1]\n{exact}"
    caption = (
        "Global Metadata: Bright melodic synth pop.\n\n"
        "Vocal Details: Clearly sung male lead.\n\n"
        "Arrangement: Instrumental opening, repeated chorus, musical outro."
    )
    monkeypatch.setattr(media_app, "qwen_json", lambda *_args: {
        "caption": caption, "lyrics": tagged,
    })

    payloads = []

    def fake_comfy(_port, payload, **_kwargs):
        payloads.append(payload)
        prefix = payload["35"]["inputs"]["filename_prefix"].split("/", 1)[1]
        (comfy_dir / "output/music3-lab" / f"{prefix}_00001.flac").write_bytes(b"flac")

    monkeypatch.setattr(media_app, "comfy_run", fake_comfy)
    target = media_app._literal_words(exact)
    monkeypatch.setattr(media_app, "_screenshot_song_transcript", lambda _path: {
        "duration": 52.0,
        "words": [{"word": word, "probability": 0.99}
                  for word in target.split()],
    })
    monkeypatch.setattr(media_app, "_screenshot_song_melody_report", lambda _path: {
        "analyzed_seconds": 52.0,
        "pitch_iqr_semitones": 12.0,
        "pitch_p90_p10_semitones": 20.0,
    })
    monkeypatch.setattr(media_app, "media_duration", lambda _path: 52.0)

    def fake_run(command, **_kwargs):
        output = command[-1]
        if isinstance(output, str):
            path = Path(output)
            if path.suffix in (".mp3", ".png"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"encoded" if path.suffix == ".mp3" else b"wave")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(media_app.subprocess, "run", fake_run)
    lock_path = tmp_path / "inference.lock"
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/run/user/1000/media-lab-inference.lock":
            return real_open(lock_path, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    job = {
        "id": "songfirstworker1",
        "kind": "screenshotsong",
        "status": "running",
        "request": {
            "vibe": "catchy synth pop",
            "length": "auto",
            "duration_seconds": None,
            "screenshot_song_mode": "exact",
            "source_material": exact,
            "lyrics": exact,
            "literal_lyrics": True,
            "screenshots": [{"source": "/media/one.png", "text": exact}],
        },
    }
    media_app.run_music(job, finalize=False)

    expected = media_app.song_first_auto_seconds(exact, adapted=True)
    assert payloads[0]["13"]["inputs"]["max_duration"] == expected
    assert media_app.lyric_tokens(media_app._literal_words(job["lyrics"])) == media_app.lyric_tokens(exact)
    assert job["request"]["duration_seconds"] == expected
    assert job["request"]["auto_duration_resolved"] is True
    assert job["director_qa"]["passed"] is True
    assert job["director_qa"]["policy"] == "exact-song-asr-v1"
    assert job["director_qa"]["attempts"][0]["melodic_delivery"]["passed"] is True
    assert job["stage"] == "aligning lyrics"


def _storyboard_test_state(media_app, tmp_path, monkeypatch):
    boards_file = tmp_path / "storyboards.json"
    chars_file = tmp_path / "characters.json"
    chars_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(media_app, "BOARDS_FILE", boards_file)
    monkeypatch.setattr(media_app, "CHARS_FILE", chars_file)
    monkeypatch.setattr(media_app, "jobs", {})
    return boards_file


def test_multiscene_screenshot_song_auto_creates_one_idempotent_storyboard(
        media_app, tmp_path, monkeypatch):
    import json

    boards_file = _storyboard_test_state(media_app, tmp_path, monkeypatch)
    job = {
        "id": "shot123",
        "kind": "screenshotsong",
        "status": "done",
        "video_url": "/media/shot123-screens.mp4",
        "request": {
            "vibe": "catchy bright synth pop",
            "orientation": "square",
            "screenshots": [
                {"source": "/media/scene-1.png", "text": "First exact line"},
                {"source": "/media/scene-2.png", "text": "Second exact line"},
            ],
        },
        "screenshot_cues": [
            {"start": 0.0, "end": 4.25},
            {"start": 4.25, "end": 9.5},
        ],
    }
    child = {"id": "shot123-screens", "kind": "screenshotmusicvideo",
             "status": "done", "url": job["video_url"]}
    media_app.jobs.update({job["id"]: job, child["id"]: child})

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        board_ids = list(pool.map(
            lambda _index: media_app.ensure_multi_scene_storyboard(job), range(16)
        ))
    first = board_ids[0]
    boards = json.loads(boards_file.read_text(encoding="utf-8"))

    assert set(board_ids) == {first}
    assert len(boards) == 1
    board = boards[0]
    assert board["id"] == first
    assert board["source_job_id"] == "shot123"
    assert board["source_kind"] == "screenshotsong"
    assert board["auto_created"] is True
    assert board["orientation"] == "square"
    assert board["final_url"] == job["video_url"]
    assert [beat["description"] for beat in board["beats"]] == [
        "First exact line", "Second exact line"
    ]
    assert [beat["still_url"] for beat in board["beats"]] == [
        "/media/scene-1.png", "/media/scene-2.png"
    ]
    assert [beat["source_duration"] for beat in board["beats"]] == [4.25, 5.25]
    assert job["board_id"] == first
    assert child["board_id"] == first


def test_any_completed_generic_multishot_job_auto_creates_storyboard(
        media_app, tmp_path, monkeypatch):
    import json

    boards_file = _storyboard_test_state(media_app, tmp_path, monkeypatch)
    job = {
        "id": "multishot456",
        "kind": "musicvideo",
        "status": "done",
        "url": "/media/multishot456.mp4",
        "identity": "Same performer and wardrobe in every scene.",
        "request": {"concept": "A complete multi-shot performance",
                    "orientation": "portrait", "song_id": "song789"},
        "scenes": [
            {"i": 0, "url": "/media/multishot456_scene0.mp4",
             "poster": "/media/multishot456_scene0.jpg",
             "text": "Opening performance", "seconds": 6.2},
            {"i": 1, "url": "/media/multishot456_scene1.mp4",
             "poster": "/media/multishot456_scene1.jpg",
             "text": "Closing performance", "seconds": 7.1},
        ],
    }
    media_app.jobs[job["id"]] = job

    board_id = media_app.ensure_multi_scene_storyboard(job)
    board = json.loads(boards_file.read_text(encoding="utf-8"))[0]

    assert board_id == board["id"]
    assert board["source_kind"] == "musicvideo"
    assert board["song_id"] == "song789"
    assert board["final_url"] == job["url"]
    assert [beat["clip_url"] for beat in board["beats"]] == [
        "/media/multishot456_scene0.mp4",
        "/media/multishot456_scene1.mp4",
    ]
    assert [beat["source_duration"] for beat in board["beats"]] == [6.2, 7.1]


def test_single_scene_job_does_not_create_storyboard(
        media_app, tmp_path, monkeypatch):
    boards_file = _storyboard_test_state(media_app, tmp_path, monkeypatch)
    job = {
        "id": "single789", "kind": "musicvideo", "status": "done",
        "request": {"concept": "One shot"},
        "scenes": [{"text": "Only scene", "seconds": 5.0}],
    }
    media_app.jobs[job["id"]] = job

    assert media_app.ensure_multi_scene_storyboard(job) is None
    assert not boards_file.exists()
    assert "board_id" not in job


def test_storyboard_invariant_is_wired_to_completion_and_shared_ui():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    ui = (ROOT / "static/index.html").read_text(encoding="utf-8")

    assert 'if j["status"] == "done":\n            try:\n                ensure_multi_scene_storyboard(j)' in app
    assert '@app.get("/api/storyboards")' in app
    assert "j.board_id&&j.kind!=='storyboard'" in ui
    assert '"board_id": j.get("board_id")' in app
    assert "x.board_id?" in ui
    assert "🎞 Open storyboard" in ui
