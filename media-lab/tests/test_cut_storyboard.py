import copy
import json
import re
from pathlib import Path

import pytest

from media_lab_core.cut import (
    PROJECT_SCHEMA,
    CutError,
    build_ffmpeg_command,
    build_queue_handoff,
    load_storyboard_project,
    validate_export_request,
    validate_manifest,
)

ROOT = Path(__file__).parents[1]


def _fixture_storyboard(tmp_path: Path) -> Path:
    path = tmp_path / "storyboard.json"
    path.write_text(
        json.dumps(
            {
                "schema": "media_lab.storyboard.v1",
                "project": "prototype",
                "title": "Prototype film",
                "private_internal_only": True,
                "candidate_not_final_until_owner_approves": True,
                "format": {
                    "aspect_ratio": "16:9",
                    "resolution": "1280x704",
                    "fps": 24,
                    "shot_duration_seconds": 5.0,
                    "shot_count": 2,
                    "estimated_total_seconds": 10.0,
                },
                "shots": [
                    {
                        "id": "P01",
                        "kind": "presenter",
                        "narration": "First line.",
                        "visual": "First visual.",
                        "engine_plan": "local",
                        "qa": ["identity"],
                    },
                    {
                        "id": "P02",
                        "kind": "product-ui",
                        "narration": "Second line.",
                        "visual": "Second visual.",
                        "engine_plan": "composite",
                        "qa": ["source fidelity"],
                    },
                ],
                "publication_authorized": False,
            }
        )
    )
    return path


def test_storyboard_normalizes_to_multitrack_candidate_project(tmp_path):
    project = load_storyboard_project(_fixture_storyboard(tmp_path))

    assert project["schema"] == PROJECT_SCHEMA
    assert project["schema_version"] == 2
    assert project["approval"]["state"] == "candidate"
    assert project["approval"]["publication_authorized"] is False
    assert project["settings"] == {
        "width": 1280,
        "height": 704,
        "aspect_ratio": "16:9",
        "fps": 24,
        "timebase": "1/24",
    }
    assert project["duration_seconds"] == 10.0
    assert [track["type"] for track in project["timeline"]["tracks"]] == [
        "video",
        "dialogue",
        "music",
        "captions",
    ]
    assert [clip["id"] for clip in project["timeline"]["tracks"][0]["clips"]] == [
        "clip-P01-main",
        "clip-P02-main",
    ]
    assert project["timeline"]["tracks"][0]["clips"][1]["start_frame"] == 120
    assert project["timeline"]["tracks"][1]["clips"][0]["label"] == "First line."
    assert project["storyboard"]["scenes"][1]["candidate_ids"] == [
        "candidate-P02-a",
        "candidate-P02-b",
    ]


def test_export_range_and_renderer_contract_are_fail_closed(tmp_path):
    project = load_storyboard_project(_fixture_storyboard(tmp_path))
    request = validate_export_request(
        project,
        {
            "range_mode": "selection",
            "range_start_seconds": 2.0,
            "range_end_seconds": 8.5,
            "format": "mp4",
            "quality": "high",
            "include_audio": True,
        },
    )

    assert request["duration_seconds"] == 6.5
    command = build_ffmpeg_command(
        source=Path("/private/source.mp4"),
        output=Path("/private/output.mp4"),
        export_request=request,
    )
    assert command[0] == "ffmpeg"
    assert command[1:5] == ["-nostdin", "-v", "error", "-y"]
    assert command[command.index("-ss") : command.index("-ss") + 2] == ["-ss", "2.000000"]
    assert "-t" in command
    assert "libx264" in command
    assert "aac" in command
    assert "+faststart" in command
    assert "+bitexact" in command
    assert command[-1] == "/private/output.mp4"

    with pytest.raises(CutError, match="inside the project duration"):
        validate_export_request(
            project,
            {
                "range_mode": "selection",
                "range_start_seconds": 9,
                "range_end_seconds": 11,
            },
        )


def test_queue_handoff_requires_explicit_confirmation_and_stays_candidate(tmp_path):
    project = load_storyboard_project(_fixture_storyboard(tmp_path))
    export_request = validate_export_request(project, {"range_mode": "full"})

    with pytest.raises(CutError, match="explicit confirmation"):
        build_queue_handoff(project, export_request, explicit_confirmation=False)

    handoff = build_queue_handoff(project, export_request, explicit_confirmation=True)
    assert handoff["schema"] == "media_lab.finishing_handoff.v1"
    assert handoff["candidate_not_final"] is True
    assert handoff["publication_authorized"] is False
    assert handoff["queue"] == {
        "kind": "finishing_export",
        "dispatch_state": "not_submitted",
        "gpu_required": False,
    }
    assert handoff["readiness"]["state"] == "needs_media"
    assert handoff["readiness"]["renderable"] is False
    assert handoff["request_id"].startswith("finish-")

    project_with_media = copy.deepcopy(project)
    project_with_media["assets"] = [
        {
            "id": "asset-vx00",
            "kind": "video",
            "role": "source",
            "source": {
                "path": "/media/vx00.mp4",
                "sha256": "a" * 64,
                "immutable": True,
                "exists": True,
            },
            "proxy_ref": "/proxies/vx00-proxy.mp4",
            "reference_metadata": {},
            "audio_metadata": {},
            "subtitle_metadata": {},
            "prompt_metadata": {},
            "approval_metadata": {"state": "candidate"},
        }
    ]
    project_with_media["timeline"]["tracks"][0]["clips"][0]["asset_id"] = "asset-vx00"
    validate_manifest(project_with_media)
    ready = build_queue_handoff(
        project_with_media,
        export_request,
        explicit_confirmation=True,
    )
    assert ready["readiness"]["state"] == "ready"
    assert ready["payload"]["sources"] == ["/media/vx00.mp4"]

    project_with_media["assets"][0]["source"]["path"] = "https://example.com/vx00.mp4"
    with pytest.raises(CutError, match="/media/"):
        build_queue_handoff(
            project_with_media,
            export_request,
            explicit_confirmation=True,
        )


def test_cut_ui_contract_and_entrypoints_exist():
    html = (ROOT / "static/cut.html").read_text()
    css = (ROOT / "static/cut.css").read_text()
    js = (ROOT / "static/cut.js").read_text()
    index = (ROOT / "static/index.html").read_text()
    sw = (ROOT / "static/sw.js").read_text()
    app_source = (ROOT / "app.py").read_text()

    for label in ("Trim", "Split", "Move", "Dissolve", "Captions", "Audio", "Color", "Render",
                  "Approve exact diff", "Studio", "lab-theme"):
        assert label in html or label in js
    for command_name in ("clip.trim", "clip.split", "clip.move", "clip.add", "clip.remove",
                         "transition.set", "caption.add", "caption.edit", "audio.mix", "color.apply",
                         "project.approve", "undo", "redo"):
        assert command_name in js
    assert "/api/cut/projects/" in js
    assert "/commands" in js and "/review/" in js and "/render" in js
    assert "JSON.stringify(body)" in js
    assert js.count("crypto.randomUUID()") >= 2
    # the studio's theme system, verbatim
    assert ':root[data-theme="coagent"]' in css and "--gold:#C99A6A" in css
    assert "Space+Grotesk" in html and "Barlow" in html
    assert "lab-theme" in html
    # studio integration + deploy rule
    assert "cutFromJob(" in index and "✂️ Cut" in index and "/cut" in index
    assert "/static/cut.js" in sw and 'medialab-studio-v21' not in sw
    assert '@app.get("/cut")' in app_source
    assert '@app.post("/api/cut/projects/{project_id}/commands")' in app_source
    assert '@app.post("/api/cut/projects/{project_id}/sparky/commands")' in app_source
    assert '@app.post("/api/cut/projects/{project_id}/render")' in app_source
    # no private hosts in anything Cut ships: no tailnet (CGNAT) address, no
    # MagicDNS name, no service account or laptop hostname
    tailnet_ip = re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")
    for text in (html, css, js, (ROOT / "media_lab_core/cut.py").read_text(),
                 (ROOT / "media_lab_core/cut_cli.py").read_text(), (ROOT / "docs/CUT.md").read_text()):
        assert not tailnet_ip.search(text)
        for bad in (".ts.net", "medialab", "macbook-pro"):
            assert bad not in text
