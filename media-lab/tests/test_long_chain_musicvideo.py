import json

import app as media_app
from starlette.responses import JSONResponse


def test_long_chain_chunks_cover_full_113_second_song():
    chunks = media_app.long_chain_chunks(113.557188, 10.0)
    assert len(chunks) == 12
    assert chunks[:-1] == [10.0] * 11
    assert chunks[-1] == 3.557
    assert abs(sum(chunks) - 113.557) < 0.001


def test_long_chain_frame_grids_are_native_and_not_short():
    h3 = media_app.long_chain_frame_count("h3", 10.0)
    ltx = media_app.long_chain_frame_count("ltx", 10.0)
    assert (h3 - 5) % 17 == 0
    assert (ltx - 1) % 8 == 0
    assert h3 / 24 >= 10.0
    assert ltx / 24 >= 10.0


def test_long_chain_join_settle_adds_rendered_frames():
    assert media_app.long_chain_frame_count("h3", 10.0, True) > media_app.long_chain_frame_count("h3", 10.0)
    assert media_app.long_chain_frame_count("ltx", 10.0, True) > media_app.long_chain_frame_count("ltx", 10.0)


def test_chain_mode_requires_first_frame(tmp_path, monkeypatch):
    (tmp_path / "song.mp3").write_bytes(b"music")
    monkeypatch.setattr(media_app, "MEDIA", tmp_path)
    response = media_app.musicvideo(media_app.MVReq(
        song_id="song", concept="Mia drives in rain", chain=True,
        source="/media/missing.png",
    ))
    assert response.status_code == 400
    assert "first-frame" in json.loads(response.body)["error"]


def test_h3_turbo_is_rejected_for_ltx(tmp_path, monkeypatch):
    (tmp_path / "song.mp3").write_bytes(b"music")
    monkeypatch.setattr(media_app, "MEDIA", tmp_path)
    response = media_app.musicvideo(media_app.MVReq(
        song_id="song", concept="Mia drives in rain",
        engine="ltx25", h3_turbo="v4-8step",
    ))
    assert response.status_code == 400
    assert "requires engine 'h3'" in json.loads(response.body)["error"]


def test_h3_chain_forwards_managed_turbo_and_source(tmp_path, monkeypatch):
    (tmp_path / "song.mp3").write_bytes(b"music")
    (tmp_path / "start.png").write_bytes(b"png")
    monkeypatch.setattr(media_app, "MEDIA", tmp_path)
    captured = {}

    def fake_submit(kind, request, extra=None):
        captured.update(kind=kind, request=request, extra=extra)
        return {"id": "testjob", "kind": kind, "request": request}

    monkeypatch.setattr(media_app, "submit_job", fake_submit)
    monkeypatch.setattr(media_app, "engine_up", lambda _name: False)
    monkeypatch.setattr(media_app, "media_duration", lambda _path: 113.557)
    monkeypatch.setattr(media_app, "eta_estimate", lambda _job: 1)
    response = media_app.musicvideo(media_app.MVReq(
        song_id="song", concept="Mia drives in rain", engine="h3",
        length="full", chain=True, source="/media/start.png",
        segment_seconds=10.0, seed=2026082301, h3_turbo="v4-8step",
        scene_plan_job_id="completed-ltx-job",
    ))
    assert response == {"id": "testjob", "eta_min": 1, "duration_seconds": 113.557}
    assert captured["kind"] == "musicvideo"
    assert captured["request"]["chain"] is True
    assert captured["request"]["source"] == "/media/start.png"
    assert captured["request"]["h3_turbo"] == "v4-8step"


def test_completed_scene_plan_job_bypasses_qwen_and_is_copied_verbatim(monkeypatch):
    exact_scenes = [f"Exact scene {i + 1}" for i in range(12)]
    source_job = {
        "id": "ltx-source",
        "kind": "musicvideo",
        "status": "done",
        "identity": "Exact Mia identity",
        "scenes": [{"text": text} for text in exact_scenes],
    }
    monkeypatch.setitem(media_app.jobs, "ltx-source", source_job)
    monkeypatch.setattr(
        media_app, "qwen_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Qwen must not run")),
    )

    identity, scenes, data = media_app.musicvideo_scene_plan(
        {"scene_plan_job_id": "ltx-source"}, 12, "ignored director prompt", 9999,
    )

    assert identity == "Exact Mia identity"
    assert scenes == exact_scenes
    assert data == {"scene_plan_job_id": "ltx-source"}


def test_incomplete_scene_plan_job_fails_loudly(monkeypatch):
    monkeypatch.setitem(media_app.jobs, "unfinished", {
        "kind": "musicvideo", "status": "running", "identity": "Mia", "scenes": [],
    })

    try:
        media_app.musicvideo_scene_plan(
            {"scene_plan_job_id": "unfinished"}, 12, "ignored", 9999,
        )
    except RuntimeError as exc:
        assert "is not complete" in str(exc)
    else:
        raise AssertionError("incomplete source job must fail loudly")


def test_music_duration_supports_auto_exact_seconds_and_legacy_minutes():
    lyrics = " ".join(f"word{i}" for i in range(80))
    auto = media_app._music_seconds({
        "length": "auto", "lyrics": lyrics,
        "screenshots": [{"text": "a"}, {"text": "b"}],
    })
    assert 60 <= auto <= 70
    assert media_app._music_seconds({"length": "custom", "duration_seconds": 73}) == 73
    assert media_app._music_seconds({"length": "1"}) == 60
    assert media_app._music_seconds({"length": "3"}) == 180


def test_music_duration_rejects_out_of_range_seconds():
    try:
        media_app._music_seconds({"length": "custom", "duration_seconds": 181})
    except ValueError as exc:
        assert "between 20 and 180" in str(exc)
    else:
        raise AssertionError("out-of-range music duration must fail")


def test_musicvideo_duration_defaults_to_auto_and_supports_exact_seconds_and_legacy():
    assert media_app.MVReq(song_id="song", concept="concept").length == "auto"
    assert media_app._musicvideo_seconds({"length": "auto"}, 113.557) == 113.557
    assert media_app._musicvideo_seconds({"length": "auto"}, 240) == 180
    assert media_app._musicvideo_seconds(
        {"length": "custom", "duration_seconds": 73}, 113.557) == 73
    assert media_app._musicvideo_seconds({"length": "24"}, 113.557) == 24
    assert media_app._musicvideo_seconds({"length": "full"}, 240) == 240
    assert media_app._musicvideo_seconds(
        {"length": "custom", "duration_seconds": 180}, 80) == 80


def test_non_chain_frame_grid_rounds_up_instead_of_shortening_exact_duration():
    assert media_app.non_chain_frame_count(6.0) == 145
    assert media_app.non_chain_frame_count(6.0) / 24.0 >= 6.0
    elapsed = 0.0
    for _ in range(30):
        requested = min(6.0, 180.0 - elapsed)
        if requested <= 0.5:
            break
        elapsed += media_app.non_chain_frame_count(requested) / 24.0
    assert 180.0 <= elapsed <= 180.5


def test_brief_and_job_api_preserve_duration_and_screenshot_results(monkeypatch):
    assert media_app.brief_request({"length": "custom", "duration_seconds": 73}) == {
        "length": "custom", "duration_seconds": 73,
    }
    job = {"id": "screen123", "kind": "screenshotsong", "status": "done",
           "stage": "done", "request": {"duration_seconds": 73},
           "url": "/media/screen123.mp3", "song_url": "/media/screen123.mp3",
           "video_url": "/media/screen123-screens.mp4",
           "video_poster": "/media/screen123-screens.jpg",
           "timing": {"starts": [0.0, 20.0]}, "screenshot_cues": [{"start": 0.0}]}
    monkeypatch.setattr(media_app, "jobs", {job["id"]: job})
    lean = media_app.job(job["id"])
    assert isinstance(lean, dict)
    assert "request" not in lean
    response = media_app.job(job["id"], full=1)
    assert isinstance(response, dict)
    assert response["request"] == job["request"]
    for key in ("song_url", "video_url", "video_poster", "timing", "screenshot_cues"):
        assert response[key] == job[key]


def test_screenshot_song_rejects_oversized_review_text_without_truncating(tmp_path, monkeypatch):
    source = tmp_path / "ss_abcdef123456_01.png"
    source.write_bytes(b"image")
    monkeypatch.setattr(media_app, "media_path", lambda _source: source)
    response = media_app.screenshot_song_make(media_app.ScreenshotSongReq(
        manifest_id="abcdef123456",
        screenshots=[media_app.ScreenshotSongFrame(
            source=f"/media/{source.name}", text="x" * 4001)],
    ))
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert b"Nothing was truncated" in response.body


def test_musicvideo_endpoint_resolves_auto_before_queueing(tmp_path, monkeypatch):
    (tmp_path / "song.mp3").write_bytes(b"music")
    monkeypatch.setattr(media_app, "MEDIA", tmp_path)
    monkeypatch.setattr(media_app, "media_duration", lambda _path: 147.25)
    monkeypatch.setattr(media_app, "engine_up", lambda _name: False)
    monkeypatch.setattr(media_app, "eta_estimate", lambda _job: 9)
    captured = {}

    def fake_submit(kind, request, extra=None):
        captured.update(kind=kind, request=request, extra=extra)
        return {"id": "mvauto123", "kind": kind, "request": request}

    monkeypatch.setattr(media_app, "submit_job", fake_submit)
    response = media_app.musicvideo(media_app.MVReq(
        song_id="song", concept="Follow every lyric", length="auto"))

    assert response == {"id": "mvauto123", "eta_min": 9, "duration_seconds": 147.25}
    assert captured["request"]["length"] == "auto"
    assert captured["request"]["duration_seconds"] == 147.25


def test_literal_music_worker_rejects_bad_take_then_stages_clean_repair(tmp_path, monkeypatch):
    import builtins
    from contextlib import nullcontext
    from types import SimpleNamespace

    jobs_dir = tmp_path / "jobs"
    media_dir = tmp_path / "media"
    comfy_dir = tmp_path / "comfy"
    (comfy_dir / "output/music3-lab").mkdir(parents=True)
    jobs_dir.mkdir(); media_dir.mkdir()
    monkeypatch.setattr(media_app, "JOBS_DIR", jobs_dir)
    monkeypatch.setattr(media_app, "MEDIA", media_dir)
    monkeypatch.setattr(media_app, "COMFY_MUSIC_DIR", comfy_dir)
    monkeypatch.setattr(media_app, "ensure_engine", lambda *_args: "up")
    monkeypatch.setattr(media_app, "gpu_operation", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(media_app, "gpu_render_ready", lambda *_args: object())
    monkeypatch.setattr(media_app, "touch_engine", lambda *_args: None)
    monkeypatch.setattr(media_app, "media_duration", lambda _path: 60.0)
    monkeypatch.setattr(media_app.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(media_app, "qwen_json", lambda *_args: {
        "caption": "Global Metadata: Test.\n\nVocal Details: Clear.\n\nArrangement: Sparse.",
        "lyrics": "malicious generated replacement words",
    })

    renders = {"count": 0}
    def fake_comfy(_port, payload, **_kwargs):
        renders["count"] += 1
        prefix = payload["35"]["inputs"]["filename_prefix"].split("/", 1)[1]
        (comfy_dir / "output/music3-lab" / f"{prefix}_00001.flac").write_bytes(b"flac")
    monkeypatch.setattr(media_app, "comfy_run", fake_comfy)

    target = "Are you coming tonight bring the blue canoe"
    transcripts = iter([
        {"words": [{"word": w} for w in (target +
            " preserve every supplied word do not add system prompt text").split()]},
        {"words": [{"word": w} for w in target.split()]},
    ])
    monkeypatch.setattr(media_app, "_screenshot_song_transcript", lambda _path: next(transcripts))

    def fake_run(command, **_kwargs):
        output = command[-1]
        if isinstance(output, str):
            path = media_app.Path(output)
            if path.suffix in (".mp3", ".png"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"encoded-audio" if path.suffix == ".mp3" else b"wave")
        return SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(media_app.subprocess, "run", fake_run)

    published = []
    monkeypatch.setattr(media_app, "gallery_add", lambda *args, **kwargs: published.append(args))
    real_open = builtins.open
    lock_path = tmp_path / "inference.lock"
    def fake_open(path, *args, **kwargs):
        if str(path) == "/run/user/1000/media-lab-inference.lock":
            return real_open(lock_path, *args, **kwargs)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", fake_open)

    job = {"id": "literalqa123", "kind": "screenshotsong", "status": "running",
           "request": {"vibe": "bongo reading", "lyrics": target,
                       "literal_lyrics": True, "duration_seconds": 60}}
    media_app.run_music(job, finalize=False)

    assert renders["count"] == 2
    assert job["status"] == "running"
    assert job["stage"] == "aligning lyrics"
    assert job["director_qa"]["approved_attempt"] == 2
    assert [row["passed"] for row in job["director_qa"]["attempts"]] == [False, True]
    assert published == []
    assert (media_dir / "literalqa123.mp3").is_file()
    assert "malicious" not in job["lyrics"]
    assert not list((comfy_dir / "output/music3-lab").glob("LAB_literalqa123_a*.flac"))
    assert not list((jobs_dir / "literalqa123").glob("candidate-attempt-*.mp3"))