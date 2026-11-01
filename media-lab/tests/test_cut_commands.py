import copy
import json
from pathlib import Path

import pytest

from media_lab_core.cut import (
    COMMANDS,
    CutProjectStore,
    CutError,
    apply_commands,
    build_demo_transaction,
    import_storyboard_manifest,
    migrate_manifest,
    project_diff,
    validate_manifest,
)


def storyboard_file(tmp_path: Path, *, scenes: int = 8) -> Path:
    path = tmp_path / "storyboard.json"
    shots = []
    for index in range(scenes):
        shots.append(
            {
                "id": f"VX{index:02d}",
                "kind": "presenter" if index % 2 == 0 else "product-ui",
                "narration": f"Narration {index}.",
                "visual": f"Visual {index}.",
                "engine_plan": "local",
                "qa": ["candidate-only"],
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema": "media_lab.storyboard.v1",
                "project": "fixture-project",
                "title": "Fixture project",
                "private_internal_only": True,
                "candidate_not_final_until_owner_approves": True,
                "publication_authorized": False,
                "format": {
                    "resolution": "640x360",
                    "aspect_ratio": "16:9",
                    "fps": 24,
                    "shot_duration_seconds": 2,
                    "shot_count": scenes,
                    "estimated_total_seconds": scenes * 2,
                },
                "shots": shots,
            },
            sort_keys=True,
        )
    )
    return path


def manifest(tmp_path: Path) -> dict:
    return import_storyboard_manifest(storyboard_file(tmp_path))


def command(kind: str, payload: dict | None = None, *, command_id: str | None = None) -> dict:
    return {
        "id": command_id or f"cmd-{kind.replace('.', '-')}",
        "type": kind,
        "payload": payload or {},
    }


def test_manifest_import_is_versioned_complete_stable_and_honest(tmp_path):
    source = storyboard_file(tmp_path)
    first = import_storyboard_manifest(source)
    second = import_storyboard_manifest(source)

    assert first == second
    assert first["schema"] == "media_lab.editing_project.v2"
    assert first["schema_version"] == 2
    assert first["project_id"] == "fixture-project"
    assert [scene["id"] for scene in first["storyboard"]["scenes"][:2]] == ["VX00", "VX01"]
    assert len(first["candidates"]) == 16
    assert first["storyboard"]["scenes"][6]["candidate_ids"] == [
        "candidate-VX06-a",
        "candidate-VX06-b",
    ]
    assert first["storyboard"]["scenes"][6]["active_candidate_id"] == "candidate-VX06-a"
    assert first["assets"] == []
    assert first["media_status"] == {
        "real_media_rendered": False,
        "missing_asset_count": 16,
        "state": "metadata_only",
        "warning": "Real storyboard media is not attached; no real footage has rendered.",
    }
    assert [track["type"] for track in first["timeline"]["tracks"]] == [
        "video",
        "dialogue",
        "music",
        "captions",
    ]
    assert first["originals"]["immutable"] is True
    assert first["version_history"][0]["revision"] == 0
    assert first["render_receipts"] == []
    assert first["delivery"]["status"] == "not_delivered"
    validate_manifest(first)


def test_schema_migration_from_v1_is_deterministic(tmp_path):
    current = manifest(tmp_path)
    old = {
        "schema": "media_lab.finishing_project.v1",
        "project_id": current["project_id"],
        "title": current["title"],
        "source": current["source"],
        "approval": current["approval"],
        "settings": current["settings"],
        "duration_seconds": current["duration_seconds"],
        "tracks": current["timeline"]["tracks"],
    }
    migrated = migrate_manifest(old)
    assert migrated["schema"] == "media_lab.editing_project.v2"
    assert migrated["migration"]["from_schema"] == "media_lab.finishing_project.v1"
    assert migrated == migrate_manifest(old)
    validate_manifest(migrated)


def test_validation_rejects_duplicate_ids_and_mutable_originals(tmp_path):
    project = manifest(tmp_path)
    project["timeline"]["tracks"][0]["clips"].append(
        copy.deepcopy(project["timeline"]["tracks"][0]["clips"][0])
    )
    with pytest.raises(CutError, match="duplicate stable id"):
        validate_manifest(project)

    project = manifest(tmp_path)
    project["originals"]["immutable"] = False
    with pytest.raises(CutError, match="immutable"):
        validate_manifest(project)


def test_all_required_command_names_are_registered():
    assert COMMANDS == {
        "scene.replace",
        "clip.add",
        "clip.remove",
        "clip.trim",
        "clip.split",
        "clip.move",
        "transition.set",
        "transition.remove",
        "caption.generate",
        "caption.add",
        "caption.edit",
        "caption.remove",
        "audio.mix",
        "color.apply",
        "project.approve",
        "render.preview",
        "render.master",
        "undo",
        "redo",
        "diff",
        "restore",
    }


def test_atomic_command_batch_covers_timeline_caption_audio_color_and_preview(tmp_path):
    project = manifest(tmp_path)
    before = copy.deepcopy(project)
    clip_id = "clip-VX00-main"
    batch = [
        command("scene.replace", {"scene_id": "VX06", "candidate_id": "candidate-VX06-b"}),
        command("clip.trim", {"clip_id": clip_id, "trim_in_frames": 6, "trim_out_frames": 42}),
        command("clip.split", {"clip_id": "clip-VX01-main", "at_frame": 72}),
        command(
            "clip.move",
            {"clip_id": "clip-VX02-main", "track_id": "track-video-main", "start_frame": 100},
        ),
        command(
            "transition.set",
            {
                "from_clip_id": "clip-VX05-main",
                "to_clip_id": "clip-VX06-main",
                "kind": "dissolve",
                "duration_frames": 12,
            },
        ),
        command("caption.generate", {"scene_id": "VX06"}),
        command("caption.edit", {"caption_id": "caption-VX06", "text": "Regenerated caption."}),
        command("audio.mix", {"target": "dialogue", "normalize": True, "target_lufs": -16.0}),
        command("color.apply", {"clip_id": clip_id, "preset": "neutral", "intensity": 0.5}),
        command("render.preview", {"format": "mp4", "quality": "preview"}),
    ]
    after, outputs = apply_commands(project, batch, actor="human")

    assert project == before
    assert after["revision"] == 1
    assert after["storyboard"]["scenes"][6]["active_candidate_id"] == "candidate-VX06-b"
    assert after["timeline"]["transitions"][0]["duration_frames"] == 12
    assert after["timeline"]["captions"]["items"][-1]["text"] == "Regenerated caption."
    assert after["timeline"]["mix"]["dialogue"]["normalize"] is True
    assert after["timeline"]["color"][clip_id]["preset"] == "neutral"
    assert after["render_plans"][-1]["mode"] == "preview"
    assert len(outputs) == len(batch)
    validate_manifest(after)


def test_master_requires_project_approval_and_explicit_command_approval(tmp_path):
    project = manifest(tmp_path)
    with pytest.raises(CutError, match="master render requires"):
        apply_commands(
            project,
            [command("render.master", {"explicit_approval": True})],
            actor="human",
        )

    project["approval"]["master_render_approved"] = True
    with pytest.raises(CutError, match="master render requires"):
        apply_commands(project, [command("render.master")], actor="human")

    approved, _ = apply_commands(
        project,
        [command("render.master", {"explicit_approval": True, "generated_fixture_only": True})],
        actor="human",
    )
    assert approved["render_plans"][-1]["mode"] == "master"
    assert approved["render_plans"][-1]["execution_state"] == "planned_not_executed"


def test_store_persists_shared_state_idempotency_conflicts_and_pending_review(tmp_path):
    store_path = tmp_path / "project.json"
    store = CutProjectStore(store_path, initial_manifest=manifest(tmp_path))
    result = store.transact(
        [
            command(
                "clip.trim",
                {"clip_id": "clip-VX00-main", "trim_in_frames": 4, "trim_out_frames": 44},
            )
        ],
        actor="human",
        transaction_id="tx-human-1",
        expected_revision=0,
    )
    assert result["status"] == "applied"
    assert result["revision"] == 1
    assert CutProjectStore(store_path).load()["revision"] == 1
    assert (
        store.transact(
            [
                command(
                    "clip.trim",
                    {"clip_id": "clip-VX00-main", "trim_in_frames": 4, "trim_out_frames": 44},
                )
            ],
            actor="human",
            transaction_id="tx-human-1",
            expected_revision=0,
        )
        == result
    )

    with pytest.raises(CutError, match="transaction id collision"):
        store.transact(
            [command("diff")],
            actor="human",
            transaction_id="tx-human-1",
            expected_revision=0,
        )

    with pytest.raises(CutError, match="revision conflict"):
        store.transact(
            [
                command(
                    "clip.move",
                    {"clip_id": "clip-VX01-main", "track_id": "track-video-main", "start_frame": 9},
                )
            ],
            actor="human",
            transaction_id="tx-conflict",
            expected_revision=0,
        )

    proposed = store.transact(
        build_demo_transaction(store.load()),
        actor="sparky",
        transaction_id="tx-sparky-demo",
        expected_revision=1,
        proposed=True,
    )
    assert proposed["status"] == "proposed"
    assert proposed["revision"] == 1
    assert proposed["diff"]
    assert store.load()["storyboard"]["scenes"][6]["active_candidate_id"] == "candidate-VX06-a"

    reviewed = store.review("tx-sparky-demo", approve=True, reviewer="human")
    assert reviewed["status"] == "applied"
    shared = CutProjectStore(store_path).load()
    assert shared["revision"] == 2
    assert shared["storyboard"]["scenes"][6]["active_candidate_id"] == "candidate-VX06-b"
    assert shared["timeline"]["transitions"][-1]["duration_frames"] == 12
    assert shared["timeline"]["mix"]["dialogue"]["normalize"] is True


def test_intervening_edit_supersedes_stale_sparky_proposal(tmp_path):
    store = CutProjectStore(tmp_path / "project.json", initial_manifest=manifest(tmp_path))
    proposal_commands = [
        command(
            "scene.replace",
            {"scene_id": "VX00", "candidate_id": "candidate-VX00-b"},
        )
    ]
    proposed = store.transact(
        proposal_commands,
        actor="sparky",
        transaction_id="stale-proposal",
        expected_revision=0,
        proposed=True,
    )
    assert proposed["status"] == "proposed"

    store.transact(
        [command("diff")],
        actor="human",
        transaction_id="intervening-human-edit",
        expected_revision=0,
    )

    assert store.pending() == []
    with pytest.raises(CutError, match="pending transaction is unavailable"):
        store.review("stale-proposal", approve=True, reviewer="human")
    retried = store.transact(
        proposal_commands,
        actor="sparky",
        transaction_id="stale-proposal",
        expected_revision=0,
        proposed=True,
    )
    assert retried["status"] == "superseded"
    assert retried["revision"] == 1


def test_restore_cannot_apply_an_unreviewed_sparky_snapshot(tmp_path):
    store = CutProjectStore(tmp_path / "project.json", initial_manifest=manifest(tmp_path))
    store.transact(
        [command("diff")],
        actor="human",
        transaction_id="baseline",
        expected_revision=0,
    )
    store.transact(
        [
            command(
                "scene.replace",
                {"scene_id": "VX00", "candidate_id": "candidate-VX00-b"},
            )
        ],
        actor="sparky",
        transaction_id="pending-proposal",
        expected_revision=1,
        proposed=True,
    )

    restored = store.transact(
        [command("restore", {"revision": 1})],
        actor="human",
        transaction_id="restore-pending",
        expected_revision=1,
    )
    assert restored["status"] == "applied"
    assert store.load()["storyboard"]["scenes"][0]["active_candidate_id"] == "candidate-VX00-a"
    assert store.pending() == []
    retried = store.transact(
        [
            command(
                "scene.replace",
                {"scene_id": "VX00", "candidate_id": "candidate-VX00-b"},
            )
        ],
        actor="sparky",
        transaction_id="pending-proposal",
        expected_revision=1,
        proposed=True,
    )
    assert retried["status"] == "superseded"


def test_undo_redo_diff_restore_and_crash_recovery(tmp_path):
    store_path = tmp_path / "project.json"
    store = CutProjectStore(store_path, initial_manifest=manifest(tmp_path))
    original = store.load()
    store.transact(
        [
            command(
                "clip.move",
                {"clip_id": "clip-VX01-main", "track_id": "track-video-main", "start_frame": 77},
            )
        ],
        actor="human",
        transaction_id="tx-move",
        expected_revision=0,
    )
    moved = store.load()
    assert project_diff(original, moved)

    undone = store.transact(
        [command("undo")], actor="human", transaction_id="tx-undo", expected_revision=1
    )
    assert undone["status"] == "applied"
    assert store.load()["timeline"]["tracks"][0]["clips"][1]["start_frame"] == 48
    store.transact([command("redo")], actor="human", transaction_id="tx-redo", expected_revision=2)
    assert store.load()["timeline"]["tracks"][0]["clips"][1]["start_frame"] == 77
    store.transact(
        [command("restore", {"revision": 0})],
        actor="human",
        transaction_id="tx-restore",
        expected_revision=3,
    )
    assert store.load()["timeline"]["tracks"][0]["clips"][1]["start_frame"] == 48

    store_path.with_name("project.json.tmp").write_text("{crashed")
    recovered = CutProjectStore(store_path).recover()
    assert recovered["status"] == "recovered"
    assert recovered["revision"] == 4
    assert CutProjectStore(store_path).load()["revision"] == 4


def test_proxy_references_allowed_but_original_source_is_immutable(tmp_path):
    project = manifest(tmp_path)
    project["assets"] = [
        {
            "id": "asset-generated-fixture",
            "kind": "video",
            "role": "synthetic_fixture",
            "source": {
                "path": "/media/fixture.mp4",
                "sha256": "a" * 64,
                "immutable": True,
                "exists": True,
            },
            "proxy_ref": "/proxies/fixture-proxy.mp4",
            "reference_metadata": {},
            "audio_metadata": {},
            "subtitle_metadata": {},
            "prompt_metadata": {},
            "approval_metadata": {"state": "generated_fixture"},
        }
    ]
    validate_manifest(project)
    before = copy.deepcopy(project["assets"])
    after, _ = apply_commands(
        project,
        [
            command(
                "color.apply", {"clip_id": "clip-VX00-main", "preset": "warm", "intensity": 0.25}
            )
        ],
        actor="human",
    )
    assert after["assets"] == before

    broken = copy.deepcopy(project)
    broken["assets"][0]["proxy_ref"] = "../../secret.mp4"
    with pytest.raises(CutError, match="proxy reference"):
        validate_manifest(broken)
