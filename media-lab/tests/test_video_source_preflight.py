from pathlib import Path

import pytest

import app as media_app


def test_video_source_preflight_rejects_missing_root_media(tmp_path, monkeypatch):
    monkeypatch.setattr(media_app, "MEDIA", tmp_path)

    with pytest.raises(ValueError, match="not available in Media Lab root media"):
        media_app.normalize_video_source({
            "source": "/media/nested/missing.png",
            "prompt": "test",
        })


def test_video_source_preflight_canonicalizes_existing_basename(tmp_path, monkeypatch):
    source = tmp_path / "anchor.png"
    source.write_bytes(b"not decoded here; renderer owns image decoding")
    monkeypatch.setattr(media_app, "MEDIA", tmp_path)

    request = media_app.normalize_video_source({
        "source": "/media/nested/anchor.png?cache=old",
        "prompt": "test",
    })

    assert request["source"] == "/media/anchor.png"


def test_video_source_preflight_rejects_unsupported_extension(tmp_path, monkeypatch):
    source = tmp_path / "anchor.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(media_app, "MEDIA", tmp_path)

    with pytest.raises(ValueError, match="requires a PNG, JPG, JPEG, or WEBP"):
        media_app.normalize_video_source({"source": source.name})
