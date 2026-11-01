import base64
from pathlib import Path

import app as media_app


UI = Path(__file__).parents[1] / "static" / "index.html"


def _fresh_queues(monkeypatch):
    monkeypatch.setattr(media_app, "jobs", {})
    monkeypatch.setattr(media_app, "queue", [])
    monkeypatch.setattr(media_app, "online_queue", [])
    monkeypatch.setattr(media_app, "save_state", lambda: None)


def test_fal_job_routes_to_independent_online_queue(monkeypatch):
    _fresh_queues(monkeypatch)

    job = media_app.submit_job(
        "video",
        {"model": "fal-video", "prompt": "cloud test"},
        extra={"engine": "fal-video"},
    )

    assert job["queue_lane"] == "online"
    assert media_app.online_queue == [job["id"]]
    assert media_app.queue == []


def test_local_job_stays_in_local_queue(monkeypatch):
    _fresh_queues(monkeypatch)

    job = media_app.submit_job(
        "video",
        {"model": "h3", "prompt": "local test"},
        extra={"engine": "h3"},
    )

    assert job["queue_lane"] == "local"
    assert media_app.queue == [job["id"]]
    assert media_app.online_queue == []


def test_h3_max_continuation_payload_uses_local_start_frame_as_data_uri(tmp_path, monkeypatch):
    source = tmp_path / "tail.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\ncontinuation")
    monkeypatch.setattr(media_app, "MEDIA", tmp_path)
    monkeypatch.setattr(
        media_app,
        "fal_config",
        lambda: {
            "enabled": True,
            "api_key": "redacted",
            "models": {"image": "fal-ai/flux/dev", "video": "minimax/h3-max/image-to-video"},
        },
    )
    job = {
        "id": "online123",
        "full_prompt": "Continue seamlessly",
        "request": {
            "source": "/media/tail.png",
            "duration": "12",
            "orientation": "landscape",
            "seed": 42,
        },
    }

    model_id, payload = media_app.fal_video_request(job)

    assert model_id == "minimax/h3-max/image-to-video"
    assert payload["duration"] == 12
    assert payload["resolution"] == "768P"
    assert payload["prompt_expansion_mode"] == "quality"
    assert payload["enable_safety_checker"] is True
    assert payload["image_url"].startswith("data:image/png;base64,")
    assert base64.b64decode(payload["image_url"].split(",", 1)[1]) == source.read_bytes()


def test_h3_max_prompt_only_request_uses_text_endpoint(monkeypatch):
    monkeypatch.setattr(
        media_app,
        "fal_config",
        lambda: {
            "enabled": True,
            "api_key": "redacted",
            "models": {"image": "fal-ai/flux/dev", "video": "minimax/h3-max/image-to-video"},
        },
    )
    job = {
        "id": "online456",
        "full_prompt": "A slow tracking shot",
        "request": {"duration": "5", "orientation": "portrait", "seed": 99},
    }

    model_id, payload = media_app.fal_video_request(job)

    assert model_id == "minimax/h3-max/text-to-video"
    assert payload["aspect_ratio"] == "9:16"
    assert "image_url" not in payload


def test_queue_view_filters_lanes_and_reports_both_counts(monkeypatch):
    monkeypatch.setattr(media_app, "queue", ["local"])
    monkeypatch.setattr(media_app, "online_queue", ["online"])
    monkeypatch.setattr(
        media_app,
        "jobs",
        {
            "local": {
                "id": "local", "kind": "video", "status": "queued", "stage": "queued",
                "ts": 1, "queue_lane": "local", "engine": "h3", "request": {"model": "h3", "prompt": "local"},
            },
            "online": {
                "id": "online", "kind": "video", "status": "queued", "stage": "queued",
                "ts": 2, "queue_lane": "online", "engine": "fal-video", "request": {"model": "fal-video", "prompt": "online"},
            },
            "local-done": {
                "id": "local-done", "kind": "video", "status": "done", "stage": "done",
                "ts": 3, "queue_lane": "local", "request": {"model": "h3", "prompt": "old local"},
            },
            "online-done": {
                "id": "online-done", "kind": "video", "status": "done", "stage": "done",
                "ts": 4, "queue_lane": "online", "request": {"model": "fal-video", "prompt": "old online"},
            },
        },
    )
    monkeypatch.setattr(media_app, "eta_estimate", lambda _job: 1)

    local = media_app.queue_view(lane="local")
    online = media_app.queue_view(lane="online")

    assert [job["id"] for job in local["active"]] == ["local"]
    assert [job["id"] for job in online["active"]] == ["online"]
    assert local["active_counts"] == {"local": 1, "online": 1}
    assert online["history_counts"] == {"local": 1, "online": 1}
    assert all(job["queue_lane"] == "online" for job in online["active"] + online["history"])


def test_queue_drawer_has_local_online_toggle():
    source = UI.read_text()
    assert 'id="queue-local"' in source
    assert 'id="queue-online"' in source
    assert "lane='+QLANE" in source
