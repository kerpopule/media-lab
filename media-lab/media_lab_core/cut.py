"""Cut — Media Lab's CPU-only timeline editor core.

One versioned editing manifest (``media_lab.editing_project.v2``) per project, kept in
``<root>/cut/projects/<project_id>/``.  Human commands, CLI commands and Sparky proposals
all go through the same deterministic transaction layer (``CutProjectStore.transact``),
which journals every change before it replaces the project file.  Renders are bounded,
shell-free FFmpeg runs with SHA-256 + ffprobe receipts.  The module never touches the
GPU lock, never publishes, and never mutates source media.

Projects are normally built from Media Lab gallery items (``build_gallery_project``);
importing a storyboard snapshot (``import_storyboard_manifest``) is optional.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

_WINDOWS = os.name == "nt"


@contextmanager
def _exclusive_file_lock(handle):
    """Use the OS lock primitive; the lock file remains present across releases."""
    if _WINDOWS:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


AssetResolver = Callable[[str], "Mapping[str, Any] | None"]

PROJECT_SCHEMA = "media_lab.editing_project.v2"
LEGACY_SCHEMA = "media_lab.finishing_project.v1"
HANDOFF_SCHEMA = "media_lab.finishing_handoff.v1"
RECEIPT_SCHEMA = "media_lab.render_receipt.v1"
SCHEMA_VERSION = 2
ALLOWED_FORMATS = {"mp4", "webm"}
ALLOWED_QUALITIES = {"preview", "medium", "high", "master"}
QUALITY_CRF = {"preview": "30", "medium": "26", "high": "20", "master": "16"}
QUALITY_X264_PRESET = {"preview": "veryfast", "medium": "fast", "high": "medium", "master": "slow"}
DEFAULT_STILL_SECONDS = 4.0
DEFAULT_FPS = 24
MAX_FPS = 120
TRANSITION_KINDS = {"dissolve", "fade", "wipeleft", "wiperight", "slideleft", "slideright", "fadeblack", "fadewhite"}
# xfade names for each editor-facing transition kind
XFADE_NAMES = {"dissolve": "fade", "fade": "fade", "wipeleft": "wipeleft", "wiperight": "wiperight",
               "slideleft": "slideleft", "slideright": "slideright", "fadeblack": "fadeblack",
               "fadewhite": "fadewhite"}
# Colour presets: eq parameters are interpolated by intensity; curves presets are on/off.
COLOR_PRESETS = {
    "neutral": {},
    "warm": {"eq": {"saturation": 1.12, "gamma_r": 1.06, "gamma_b": 0.94}},
    "cool": {"eq": {"saturation": 1.06, "gamma_r": 0.95, "gamma_b": 1.07}},
    "punchy": {"eq": {"contrast": 1.18, "saturation": 1.28}},
    "soft": {"eq": {"contrast": 0.9, "brightness": 0.03, "saturation": 0.92}},
    "bw": {"eq": {"saturation": 0.0, "contrast": 1.08}},
    "vintage": {"curves": "vintage"},
    "dramatic": {"curves": "strong_contrast"},
}
COMMANDS = {
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
STATEFUL_STORE_COMMANDS = {"undo", "redo", "restore"}
HUMAN_ONLY_COMMANDS = {"project.approve"}


# A storyboard stays a candidate until its owner approves it. Boards written
# before 2026-09-25 carry the same flag under an older owner-named key, so any
# "candidate_not_final_until_<who>_approves" flag set to true counts.
CANDIDATE_KEY = "candidate_not_final_until_owner_approves"


def candidate_until_approved(value: dict) -> bool:
    if value.get(CANDIDATE_KEY) is True:
        return True
    return any(isinstance(k, str) and k.startswith("candidate_not_final_until_")
               and k.endswith("_approves") and v is True for k, v in value.items())


class CutError(ValueError):
    """A fail-closed validation error safe to show in the UI, CLI and agent receipts."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    if isinstance(value, Path):
        return hashlib.sha256(value.read_bytes()).hexdigest()
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _finite_number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CutError(f"{label} must be a number") from exc
    if not math.isfinite(number) or number < minimum:
        raise CutError(f"{label} must be at least {minimum}")
    return number


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    number = _finite_number(value, label, minimum=minimum)
    if int(number) != number:
        raise CutError(f"{label} must be an integer")
    return int(number)


def _resolution(value: Any) -> tuple[int, int]:
    try:
        width_text, height_text = str(value).lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as exc:
        raise CutError("storyboard resolution must be WIDTHxHEIGHT") from exc
    if width < 16 or height < 16:
        raise CutError("storyboard resolution is too small")
    return width, height


def _stable_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 160 or any(ch in text for ch in "\\/\0"):
        raise CutError(f"{label} is not a valid stable id")
    return text


def _sha256(value: Any, label: str) -> str:
    text = str(value or "").lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise CutError(f"{label} must be a SHA-256 digest")
    return text


def _metadata_block() -> dict[str, Any]:
    return {
        "reference_metadata": {},
        "audio_metadata": {},
        "subtitle_metadata": {},
        "prompt_metadata": {},
        "approval_metadata": {"state": "candidate", "approved": False},
    }


def _read_storyboard(path: str | Path) -> tuple[Path, dict[str, Any]]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise CutError("configured storyboard is unavailable")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CutError("configured storyboard is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CutError("storyboard root must be an object")
    if value.get("private_internal_only") is not True:
        raise CutError("finishing studio only accepts private/internal storyboards")
    if not candidate_until_approved(value):
        raise CutError("storyboard must remain a candidate until its owner approves it")
    if value.get("publication_authorized") is not False:
        raise CutError("publication authorization must be explicitly false")
    return source, value


def import_storyboard_manifest(path: str | Path) -> dict[str, Any]:
    """Import a deterministic metadata-only v2 editing manifest.

    Candidate records intentionally say ``missing`` until authorized media is attached.
    The source storyboard is hashed and recorded as immutable but never modified.
    """

    source, storyboard = _read_storyboard(path)
    format_data = storyboard.get("format")
    shots = storyboard.get("shots")
    if not isinstance(format_data, dict) or not isinstance(shots, list) or not shots:
        raise CutError("storyboard needs format metadata and at least one shot")
    fps = _integer(format_data.get("fps"), "fps", minimum=1)
    width, height = _resolution(format_data.get("resolution"))
    shot_seconds = _finite_number(
        format_data.get("shot_duration_seconds"), "shot duration", minimum=0.001
    )
    shot_frames = max(1, round(shot_seconds * fps))
    if _integer(format_data.get("shot_count", len(shots)), "shot count", minimum=1) != len(shots):
        raise CutError("declared shot count does not match the storyboard")

    scenes: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    video_clips: list[dict[str, Any]] = []
    dialogue_clips: list[dict[str, Any]] = []
    caption_clips: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(shots):
        if not isinstance(raw, dict):
            raise CutError(f"shot {index + 1} must be an object")
        scene_id = _stable_id(raw.get("id"), f"shot {index + 1} id")
        if scene_id in seen:
            raise CutError("every shot needs a unique id")
        seen.add(scene_id)
        candidate_ids = [f"candidate-{scene_id}-a", f"candidate-{scene_id}-b"]
        start_frame = index * shot_frames
        narration = str(raw.get("narration") or "").strip()
        visual = str(raw.get("visual") or "").strip()
        scene = {
            "id": scene_id,
            "index": index + 1,
            "label": visual or scene_id,
            "kind": str(raw.get("kind") or "video"),
            "narration": narration,
            "visual": visual,
            "engine_plan": str(raw.get("engine_plan") or ""),
            "qa": [str(item) for item in (raw.get("qa") or [])],
            "candidate_ids": candidate_ids,
            "active_candidate_id": candidate_ids[0],
            "source_metadata": {"state": "missing", "asset_id": None},
            "reference_metadata": copy.deepcopy(raw.get("reference_metadata") or {}),
            "audio_metadata": {"narration_text_present": bool(narration), "asset_id": None},
            "subtitle_metadata": {"source": "storyboard narration", "generated": False},
            "prompt_metadata": {
                "visual": visual,
                "engine_plan": str(raw.get("engine_plan") or ""),
            },
            "approval_metadata": {
                "state": "candidate",
                "approved_candidate_id": None,
                "owner_creative_approval": False,
            },
        }
        scenes.append(scene)
        for suffix, label in (("a", "Candidate A"), ("b", "Candidate B")):
            candidates.append(
                {
                    "id": f"candidate-{scene_id}-{suffix}",
                    "scene_id": scene_id,
                    "label": label,
                    "asset_id": None,
                    "media_state": "missing",
                    "source_metadata": {"path": None, "sha256": None, "immutable": True},
                    **_metadata_block(),
                }
            )
        base_clip = {
            "scene_id": scene_id,
            "candidate_id": candidate_ids[0],
            "start_frame": start_frame,
            "duration_frames": shot_frames,
            "trim_in_frame": 0,
            "trim_out_frame": shot_frames,
            "asset_id": None,
            "proxy_ref": None,
            "effects": [],
            "approval_state": "candidate",
        }
        video_clips.append(
            {"id": f"clip-{scene_id}-main", "label": visual or scene_id, **base_clip}
        )
        dialogue_clips.append(
            {
                "id": f"clip-{scene_id}-dialogue",
                "label": narration or "No narration",
                **base_clip,
            }
        )
        caption_clips.append(
            {
                "id": f"clip-{scene_id}-caption",
                "label": narration or "No caption text",
                **base_clip,
            }
        )

    duration_frames = len(shots) * shot_frames
    estimated = format_data.get("estimated_total_seconds")
    if estimated is not None and abs(float(estimated) - duration_frames / fps) > 0.05:
        raise CutError("estimated duration does not match shot timing")
    source_record = {
        "path": str(source),
        "sha256": _digest(source),
        "immutable": True,
        "kind": "storyboard_metadata",
    }
    project = {
        "schema": PROJECT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "project_id": _stable_id(storyboard.get("project") or source.parent.name, "project id"),
        "title": str(storyboard.get("title") or source.stem),
        "revision": 0,
        "source": source_record,
        "originals": {"immutable": True, "inventory": [source_record]},
        "approval": {
            "state": "candidate",
            "candidate_not_final": True,
            "private_internal_only": True,
            "publication_authorized": False,
            "creative_approval": "not_approved",
            "master_render_approved": False,
        },
        "settings": {
            "width": width,
            "height": height,
            "aspect_ratio": str(format_data.get("aspect_ratio") or f"{width}:{height}"),
            "fps": fps,
            "timebase": f"1/{fps}",
        },
        "duration_frames": duration_frames,
        "duration_seconds": round(duration_frames / fps, 6),
        "storyboard": {"scenes": scenes},
        "assets": [],
        "candidates": candidates,
        "timeline": {
            "tracks": [
                {
                    "id": "track-video-main",
                    "name": "Video 1",
                    "type": "video",
                    "clips": video_clips,
                },
                {
                    "id": "track-dialogue-main",
                    "name": "Dialogue",
                    "type": "dialogue",
                    "clips": dialogue_clips,
                },
                {"id": "track-music-main", "name": "Music", "type": "music", "clips": []},
                {
                    "id": "track-captions-main",
                    "name": "Captions",
                    "type": "captions",
                    "clips": caption_clips,
                },
            ],
            "transitions": [],
            "effects": [],
            "captions": {"items": [], "style": "media-lab-readable-v1"},
            "color": {},
            "mix": {
                "dialogue": {"normalize": False, "target_lufs": -16.0, "gain_db": 0.0},
                "music": {"gain_db": -12.0},
            },
        },
        "version_history": [
            {
                "revision": 0,
                "transaction_id": "import",
                "actor": "importer",
                "summary": "Imported immutable storyboard metadata; media remains missing.",
            }
        ],
        "history_state": {"undo_stack": [], "redo_stack": []},
        "pending_transactions": [],
        "render_plans": [],
        "render_receipts": [],
        "delivery": {
            "status": "not_delivered",
            "publication_authorized": False,
            "destinations": [],
        },
        "media_status": {
            "real_media_rendered": False,
            "missing_asset_count": len(candidates),
            "state": "metadata_only",
            "warning": "Real storyboard media is not attached; no real footage has rendered.",
        },
        "provenance": {
            "editor_inspiration": "OpenCut Classic",
            "upstream_url": "https://github.com/OpenCut-app/OpenCut-Classic",
            "upstream_commit": "cf5e79e919144200294fb9fed22a222592a0aeea",
            "upstream_license": "MIT",
            "code_copied": False,
        },
    }
    validate_manifest(project)
    return project


def _legacy_scene_from_clip(clip: Mapping[str, Any], index: int) -> dict[str, Any]:
    scene_id = str(clip.get("scene_id") or clip.get("id") or f"scene-{index + 1}")
    if scene_id.startswith("clip-") and scene_id.endswith("-main"):
        scene_id = scene_id[5:-5]
    candidate_ids = [f"candidate-{scene_id}-a", f"candidate-{scene_id}-b"]
    return {
        "id": scene_id,
        "index": index + 1,
        "label": str(clip.get("label") or scene_id),
        "kind": str(clip.get("kind") or "video"),
        "narration": "",
        "visual": str(clip.get("label") or ""),
        "engine_plan": str(clip.get("engine_plan") or ""),
        "qa": list(clip.get("qa") or []),
        "candidate_ids": candidate_ids,
        "active_candidate_id": candidate_ids[0],
        "source_metadata": {"state": "missing", "asset_id": None},
        "reference_metadata": {},
        "audio_metadata": {},
        "subtitle_metadata": {},
        "prompt_metadata": {},
        "approval_metadata": {"state": "candidate", "approved_candidate_id": None},
    }


def migrate_manifest(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate the bounded v1 prototype schema without mutating its input."""

    value = copy.deepcopy(dict(raw))
    if value.get("schema") == PROJECT_SCHEMA:
        validate_manifest(value)
        return value
    if value.get("schema") != LEGACY_SCHEMA:
        raise CutError("unsupported finishing project schema")
    fps = _integer((value.get("settings") or {}).get("fps", 24), "fps", minimum=1)
    legacy_tracks = copy.deepcopy(value.get("tracks") or [])
    video_track = next(
        (track for track in legacy_tracks if track.get("type") == "video"), {"clips": []}
    )
    scenes = [
        _legacy_scene_from_clip(clip, index)
        for index, clip in enumerate(video_track.get("clips") or [])
    ]
    candidates = []
    for scene in scenes:
        for suffix, label in (("a", "Candidate A"), ("b", "Candidate B")):
            candidates.append(
                {
                    "id": f"candidate-{scene['id']}-{suffix}",
                    "scene_id": scene["id"],
                    "label": label,
                    "asset_id": None,
                    "media_state": "missing",
                    "source_metadata": {"path": None, "sha256": None, "immutable": True},
                    **_metadata_block(),
                }
            )
    source = copy.deepcopy(value.get("source") or {})
    source.setdefault("immutable", True)
    source.setdefault("kind", "storyboard_metadata")
    duration_seconds = _finite_number(value.get("duration_seconds", 0), "duration")
    duration_frames = round(duration_seconds * fps)
    project = {
        "schema": PROJECT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "project_id": _stable_id(value.get("project_id") or "migrated-project", "project id"),
        "title": str(value.get("title") or "Migrated finishing project"),
        "revision": 0,
        "source": source,
        "originals": {"immutable": True, "inventory": [source]},
        "approval": copy.deepcopy(value.get("approval") or {}),
        "settings": copy.deepcopy(value.get("settings") or {}),
        "duration_frames": duration_frames,
        "duration_seconds": round(duration_frames / fps, 6),
        "storyboard": {"scenes": scenes},
        "assets": [],
        "candidates": candidates,
        "timeline": {
            "tracks": legacy_tracks,
            "transitions": [],
            "effects": [],
            "captions": {"items": [], "style": "media-lab-readable-v1"},
            "color": {},
            "mix": {"dialogue": {"normalize": False, "target_lufs": -16.0, "gain_db": 0.0}},
        },
        "version_history": [{"revision": 0, "transaction_id": "migration", "actor": "migrator"}],
        "history_state": {"undo_stack": [], "redo_stack": []},
        "pending_transactions": [],
        "render_plans": [],
        "render_receipts": [],
        "delivery": {
            "status": "not_delivered",
            "publication_authorized": False,
            "destinations": [],
        },
        "media_status": {
            "real_media_rendered": False,
            "missing_asset_count": len(candidates),
            "state": "metadata_only",
            "warning": "Real storyboard media is not attached; no real footage has rendered.",
        },
        "provenance": {
            "editor_inspiration": "OpenCut Classic",
            "upstream_url": "https://github.com/OpenCut-app/OpenCut-Classic",
            "upstream_commit": "cf5e79e919144200294fb9fed22a222592a0aeea",
            "upstream_license": "MIT",
            "code_copied": False,
        },
        "migration": {"from_schema": LEGACY_SCHEMA, "to_schema": PROJECT_SCHEMA},
    }
    project["approval"].update(
        {
            "state": "candidate",
            "candidate_not_final": True,
            "private_internal_only": True,
            "publication_authorized": False,
            "creative_approval": "not_approved",
            "master_render_approved": False,
        }
    )
    validate_manifest(project)
    return project


def _all_clips(project: Mapping[str, Any]):
    for track in (project.get("timeline") or {}).get("tracks") or []:
        for clip in track.get("clips") or []:
            yield track, clip


def _find_by_id(items: Sequence[Mapping[str, Any]], item_id: str, label: str) -> Mapping[str, Any]:
    result = next((item for item in items if item.get("id") == item_id), None)
    if result is None:
        raise CutError(f"unknown {label} id: {item_id}")
    return result


def _find_clip(project: Mapping[str, Any], clip_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for track, clip in _all_clips(project):
        if clip.get("id") == clip_id:
            return track, clip
    raise CutError(f"unknown clip id: {clip_id}")


def validate_manifest(project: Mapping[str, Any]) -> None:
    if project.get("schema") != PROJECT_SCHEMA or project.get("schema_version") != SCHEMA_VERSION:
        raise CutError("unsupported editing project schema")
    _stable_id(project.get("project_id"), "project id")
    if _integer(project.get("revision"), "revision") < 0:
        raise CutError("revision must be non-negative")
    approval = project.get("approval")
    if not isinstance(approval, Mapping):
        raise CutError("approval metadata is required")
    if (
        approval.get("candidate_not_final") is not True
        or approval.get("publication_authorized") is not False
    ):
        raise CutError("project must remain a private unpublished candidate")
    originals = project.get("originals")
    if not isinstance(originals, Mapping) or originals.get("immutable") is not True:
        raise CutError("originals must be immutable")
    source_record = project.get("source")
    if not isinstance(source_record, Mapping) or source_record.get("immutable") is not True:
        raise CutError("import source must be immutable")
    source_sha = _sha256(source_record.get("sha256"), "import source hash")
    inventory = originals.get("inventory")
    if not isinstance(inventory, list) or not inventory or inventory[0] != source_record:
        raise CutError("original inventory must bind the import source exactly")
    source_ref = str(source_record.get("path") or "")
    source_path = Path(source_ref).expanduser()
    # Gallery imports carry a logical inventory reference, not a filesystem
    # name. Large scene lists can exceed a filesystem's filename length.
    gallery_reference = source_record.get("kind") == "gallery_import" and source_ref.startswith("gallery:")
    if not gallery_reference and source_path.is_file() and _digest(source_path) != source_sha:
        raise CutError("immutable import source bytes changed")
    timeline = project.get("timeline")
    storyboard = project.get("storyboard")
    if not isinstance(timeline, Mapping) or not isinstance(storyboard, Mapping):
        raise CutError("storyboard and timeline are required")

    seen: set[str] = set()
    groups: list[tuple[str, Sequence[Mapping[str, Any]]]] = [
        ("scene", storyboard.get("scenes") or []),
        ("asset", project.get("assets") or []),
        ("candidate", project.get("candidates") or []),
        ("track", timeline.get("tracks") or []),
        ("transition", timeline.get("transitions") or []),
        ("caption", (timeline.get("captions") or {}).get("items") or []),
    ]
    for track in timeline.get("tracks") or []:
        groups.append(("clip", track.get("clips") or []))
    for label, items in groups:
        if not isinstance(items, list):
            raise CutError(f"{label} records must be a list")
        for item in items:
            if not isinstance(item, Mapping):
                raise CutError(f"{label} record must be an object")
            item_id = _stable_id(item.get("id"), f"{label} id")
            if item_id in seen:
                raise CutError(f"duplicate stable id: {item_id}")
            seen.add(item_id)

    scene_ids = {scene["id"] for scene in storyboard.get("scenes") or []}
    candidate_ids = {candidate["id"] for candidate in project.get("candidates") or []}
    asset_ids = {asset["id"] for asset in project.get("assets") or []}
    track_ids = {track["id"] for track in timeline.get("tracks") or []}
    clip_ids = {clip["id"] for _, clip in _all_clips(project)}
    for scene in storyboard.get("scenes") or []:
        if set(scene.get("candidate_ids") or []) - candidate_ids:
            raise CutError("scene references an unknown candidate")
        if scene.get("active_candidate_id") not in scene.get("candidate_ids", []):
            raise CutError("active candidate must belong to its scene")
    for candidate in project.get("candidates") or []:
        if candidate.get("scene_id") not in scene_ids:
            raise CutError("candidate references an unknown scene")
        if candidate.get("asset_id") is not None and candidate.get("asset_id") not in asset_ids:
            raise CutError("candidate references an unknown asset")
    for asset in project.get("assets") or []:
        source = asset.get("source")
        if not isinstance(source, Mapping) or source.get("immutable") is not True:
            raise CutError("asset original source must be immutable")
        source_ref = str(source.get("path") or "")
        _media_relative(source_ref)
        _sha256(source.get("sha256"), "asset source hash")
        proxy = asset.get("proxy_ref")
        if proxy is not None:
            proxy_text = str(proxy)
            if not proxy_text.startswith("/proxies/") or Path(proxy_text).name != proxy_text[9:]:
                raise CutError("proxy reference must be basename-only /proxies/<file>")
    for _, clip in _all_clips(project):
        if clip.get("scene_id") not in scene_ids:
            raise CutError("clip references an unknown scene")
        if clip.get("candidate_id") not in candidate_ids:
            raise CutError("clip references an unknown candidate")
        if clip.get("asset_id") is not None and clip.get("asset_id") not in asset_ids:
            raise CutError("clip references an unknown asset")
        _integer(clip.get("start_frame"), "clip start frame")
        duration = _integer(clip.get("duration_frames"), "clip duration frames", minimum=1)
        trim_in = _integer(clip.get("trim_in_frame"), "clip trim in")
        trim_out = _integer(clip.get("trim_out_frame"), "clip trim out", minimum=1)
        if trim_in >= trim_out or trim_out - trim_in != duration:
            raise CutError("clip trim range must equal its duration")
    for transition in timeline.get("transitions") or []:
        if (
            transition.get("from_clip_id") not in clip_ids
            or transition.get("to_clip_id") not in clip_ids
        ):
            raise CutError("transition references an unknown clip")
    for plan in project.get("render_plans") or []:
        if plan.get("mode") == "master" and plan.get("explicit_approval") is not True:
            raise CutError("master render requires explicit approval")
    if not track_ids:
        raise CutError("at least one timeline track is required")


def _set_duration(project: dict[str, Any]) -> None:
    end = max(
        (clip["start_frame"] + clip["duration_frames"] for _, clip in _all_clips(project)),
        default=0,
    )
    project["duration_frames"] = end
    project["duration_seconds"] = round(end / project["settings"]["fps"], 6)


def _apply_one(
    project: dict[str, Any],
    command: Mapping[str, Any],
    actor: str,
    asset_resolver: AssetResolver | None = None,
) -> dict[str, Any]:
    command_id = _stable_id(command.get("id"), "command id")
    kind = str(command.get("type") or "")
    payload = command.get("payload") or {}
    if kind not in COMMANDS:
        raise CutError(f"unsupported command: {kind}")
    if not isinstance(payload, Mapping):
        raise CutError("command payload must be an object")
    if kind in STATEFUL_STORE_COMMANDS:
        raise CutError(f"{kind} requires the persistent transaction store")
    if kind in HUMAN_ONLY_COMMANDS and actor == "sparky":
        raise CutError(f"{kind} can only be applied by a person")
    if kind == "diff":
        return {"command_id": command_id, "type": kind, "diff": []}

    if kind == "project.approve":
        approved = payload.get("master_render_approved")
        if approved not in (True, False):
            raise CutError("master_render_approved must be true or false")
        project["approval"]["master_render_approved"] = approved
        project["approval"]["creative_approval"] = "approved" if approved else "not_approved"
        return {"command_id": command_id, "type": kind, "master_render_approved": approved}

    if kind == "clip.add":
        if asset_resolver is None:
            raise CutError("clip.add needs a gallery asset resolver")
        job_id = _stable_id(payload.get("job_id"), "job id")
        asset = asset_resolver(job_id)
        if not isinstance(asset, Mapping):
            raise CutError(f"unknown gallery item: {job_id}")
        fps = int(project["settings"]["fps"])
        still_seconds = _finite_number(
            payload.get("duration_seconds", DEFAULT_STILL_SECONDS), "duration seconds", minimum=0.04
        )
        record = _gallery_asset_record(asset)
        existing = next((a for a in project["assets"] if a["id"] == record["id"]), None)
        if existing is None:
            project["assets"].append(record)
        scene, candidate, clip = _gallery_clip_records(project, record, fps, still_seconds)
        wanted_track = payload.get("track_id")
        track_id = _stable_id(wanted_track, "track id") if wanted_track else clip.pop("_track_id")
        clip.pop("_track_id", None)
        track = _find_by_id(project["timeline"]["tracks"], track_id, "track")
        if record["kind"] == "music" and track["type"] == "video":
            raise CutError("a song cannot go on a video track")
        if record["kind"] != "music" and track["type"] != "video":
            raise CutError("a picture or video must go on a video track")
        if "start_frame" in payload and payload["start_frame"] is not None:
            clip["start_frame"] = _integer(payload["start_frame"], "start frame")
        else:
            clip["start_frame"] = max(
                (c["start_frame"] + c["duration_frames"] for c in track["clips"]), default=0
            )
        if payload.get("clip_id"):
            clip["id"] = _stable_id(payload["clip_id"], "clip id")
        if any(c["id"] == clip["id"] for _, c in _all_clips(project)):
            raise CutError(f"clip id already exists: {clip['id']}")
        if all(s["id"] != scene["id"] for s in project["storyboard"]["scenes"]):
            project["storyboard"]["scenes"].append(scene)
            project["candidates"].append(candidate)
        track["clips"].append(clip)
        track["clips"].sort(key=lambda item: (item["start_frame"], item["id"]))
        project["media_status"]["missing_asset_count"] = sum(
            1 for a in project["assets"] if not (a.get("source") or {}).get("exists", True)
        )
        _set_duration(project)
        return {"command_id": command_id, "type": kind, "clip_id": clip["id"], "asset_id": record["id"]}

    if kind == "clip.remove":
        clip_id = _stable_id(payload.get("clip_id"), "clip id")
        track, clip = _find_clip(project, clip_id)
        track["clips"].remove(clip)
        project["timeline"]["transitions"] = [
            t for t in project["timeline"]["transitions"]
            if clip_id not in (t.get("from_clip_id"), t.get("to_clip_id"))
        ]
        project["timeline"]["color"].pop(clip_id, None)
        _set_duration(project)
        return {"command_id": command_id, "type": kind, "clip_id": clip_id}

    if kind == "transition.remove":
        from_id = _stable_id(payload.get("from_clip_id"), "from clip id")
        to_id = _stable_id(payload.get("to_clip_id"), "to clip id")
        transition_id = f"transition-{from_id}-{to_id}"
        items = project["timeline"]["transitions"]
        if all(item["id"] != transition_id for item in items):
            raise CutError("no transition between those clips")
        project["timeline"]["transitions"] = [item for item in items if item["id"] != transition_id]
        return {"command_id": command_id, "type": kind, "transition_id": transition_id}

    if kind == "caption.add":
        text = str(payload.get("text") or "").strip()
        if not text:
            raise CutError("caption text cannot be empty")
        start = _integer(payload.get("start_frame"), "caption start")
        end = _integer(payload.get("end_frame"), "caption end", minimum=1)
        if start >= end:
            raise CutError("caption range is invalid")
        items = project["timeline"]["captions"]["items"]
        caption_id = _stable_id(payload.get("caption_id") or f"caption-{start}-{_digest(text)[:8]}", "caption id")
        if any(item["id"] == caption_id for item in items):
            raise CutError("caption id already exists")
        items.append({
            "id": caption_id, "scene_id": None, "start_frame": start, "end_frame": end,
            "text": text, "source": "typed", "approval_state": "needs_review",
        })
        items.sort(key=lambda item: (item["start_frame"], item["id"]))
        return {"command_id": command_id, "type": kind, "caption_id": caption_id}

    if kind == "caption.remove":
        caption_id = _stable_id(payload.get("caption_id"), "caption id")
        items = project["timeline"]["captions"]["items"]
        _find_by_id(items, caption_id, "caption")
        project["timeline"]["captions"]["items"] = [i for i in items if i["id"] != caption_id]
        return {"command_id": command_id, "type": kind, "caption_id": caption_id}

    if kind == "scene.replace":
        scene_id = _stable_id(payload.get("scene_id"), "scene id")
        candidate_id = _stable_id(payload.get("candidate_id"), "candidate id")
        scene = _find_by_id(project["storyboard"]["scenes"], scene_id, "scene")
        if candidate_id not in scene["candidate_ids"]:
            raise CutError("replacement candidate does not belong to the scene")
        scene["active_candidate_id"] = candidate_id
        for _, clip in _all_clips(project):
            if clip.get("scene_id") == scene_id:
                clip["candidate_id"] = candidate_id
        return {
            "command_id": command_id,
            "type": kind,
            "scene_id": scene_id,
            "candidate_id": candidate_id,
        }

    if kind == "clip.trim":
        clip_id = _stable_id(payload.get("clip_id"), "clip id")
        _, clip = _find_clip(project, clip_id)
        trim_in = _integer(payload.get("trim_in_frames"), "trim in frames")
        trim_out = _integer(payload.get("trim_out_frames"), "trim out frames", minimum=1)
        source_limit = int(
            clip.get("source_duration_frames") or max(clip["trim_out_frame"], trim_out)
        )
        if trim_in >= trim_out or trim_out > source_limit:
            raise CutError("trim range is outside the source")
        clip["trim_in_frame"] = trim_in
        clip["trim_out_frame"] = trim_out
        clip["duration_frames"] = trim_out - trim_in
        _set_duration(project)
        return {"command_id": command_id, "type": kind, "clip_id": clip_id}

    if kind == "clip.split":
        clip_id = _stable_id(payload.get("clip_id"), "clip id")
        track, clip = _find_clip(project, clip_id)
        at_frame = _integer(payload.get("at_frame"), "split frame", minimum=1)
        clip_end = clip["start_frame"] + clip["duration_frames"]
        if at_frame <= clip["start_frame"] or at_frame >= clip_end:
            raise CutError("split frame must be an absolute timeline frame inside the clip")
        offset = at_frame - clip["start_frame"]
        new_id = _stable_id(
            payload.get("new_clip_id") or f"{clip_id}-split-{at_frame}", "new clip id"
        )
        if any(existing.get("id") == new_id for _, existing in _all_clips(project)):
            raise CutError("split clip id already exists")
        original_out = clip["trim_out_frame"]
        new_clip = copy.deepcopy(clip)
        new_clip["id"] = new_id
        new_clip["start_frame"] = clip["start_frame"] + offset
        new_clip["trim_in_frame"] = clip["trim_in_frame"] + offset
        new_clip["trim_out_frame"] = original_out
        new_clip["duration_frames"] = original_out - new_clip["trim_in_frame"]
        clip["trim_out_frame"] = clip["trim_in_frame"] + offset
        clip["duration_frames"] = offset
        index = track["clips"].index(clip)
        track["clips"].insert(index + 1, new_clip)
        return {"command_id": command_id, "type": kind, "clip_id": clip_id, "new_clip_id": new_id}

    if kind == "clip.move":
        clip_id = _stable_id(payload.get("clip_id"), "clip id")
        track_id = _stable_id(payload.get("track_id"), "track id")
        source_track, clip = _find_clip(project, clip_id)
        target_track = _find_by_id(project["timeline"]["tracks"], track_id, "track")
        start = _integer(payload.get("start_frame"), "start frame")
        if source_track is not target_track:
            source_track["clips"].remove(clip)
            target_track["clips"].append(clip)
        clip["start_frame"] = start
        target_track["clips"].sort(key=lambda item: (item["start_frame"], item["id"]))
        _set_duration(project)
        return {"command_id": command_id, "type": kind, "clip_id": clip_id, "track_id": track_id}

    if kind == "transition.set":
        from_id = _stable_id(payload.get("from_clip_id"), "from clip id")
        to_id = _stable_id(payload.get("to_clip_id"), "to clip id")
        from_track, from_clip = _find_clip(project, from_id)
        to_track, to_clip = _find_clip(project, to_id)
        frames = _integer(payload.get("duration_frames"), "transition duration", minimum=1)
        if from_track is not to_track or from_track.get("type") != "video":
            raise CutError("a transition joins two clips on the same video track")
        if from_clip["start_frame"] + from_clip["duration_frames"] != to_clip["start_frame"]:
            raise CutError("a transition needs two touching clips (no gap, no overlap)")
        if frames >= min(from_clip["duration_frames"], to_clip["duration_frames"]):
            raise CutError("transition is longer than one of its clips")
        transition_kind = str(payload.get("kind") or "dissolve")
        if transition_kind not in TRANSITION_KINDS:
            raise CutError(f"unsupported transition: {transition_kind}")
        transition_id = f"transition-{from_id}-{to_id}"
        transition = {
            "id": transition_id,
            "from_clip_id": from_id,
            "to_clip_id": to_id,
            "kind": transition_kind,
            "duration_frames": frames,
        }
        items = project["timeline"]["transitions"]
        existing = next((item for item in items if item["id"] == transition_id), None)
        if existing:
            existing.update(transition)
        else:
            items.append(transition)
        return {"command_id": command_id, "type": kind, "transition_id": transition_id}

    if kind == "caption.generate":
        scene_id = _stable_id(payload.get("scene_id"), "scene id")
        scene = _find_by_id(project["storyboard"]["scenes"], scene_id, "scene")
        _, video = _find_clip(project, f"clip-{scene_id}-main")
        caption_id = f"caption-{scene_id}"
        text = str(payload.get("text") or scene.get("narration") or scene.get("label") or "").strip()
        caption = {
            "id": caption_id,
            "scene_id": scene_id,
            "start_frame": video["start_frame"],
            "end_frame": video["start_frame"] + video["duration_frames"],
            "text": text,
            "source": "typed" if payload.get("text") else "storyboard_narration_regenerated",
            "approval_state": "needs_review",
        }
        items = project["timeline"]["captions"]["items"]
        existing = next((item for item in items if item["id"] == caption_id), None)
        if existing:
            existing.update(caption)
        else:
            items.append(caption)
        return {"command_id": command_id, "type": kind, "caption_id": caption_id}

    if kind == "caption.edit":
        caption_id = _stable_id(payload.get("caption_id"), "caption id")
        caption = _find_by_id(project["timeline"]["captions"]["items"], caption_id, "caption")
        text = str(payload.get("text") or "").strip()
        if not text:
            raise CutError("caption text cannot be empty")
        caption["text"] = text
        if "start_frame" in payload:
            caption["start_frame"] = _integer(payload["start_frame"], "caption start")
        if "end_frame" in payload:
            caption["end_frame"] = _integer(payload["end_frame"], "caption end", minimum=1)
        if caption["start_frame"] >= caption["end_frame"]:
            raise CutError("caption range is invalid")
        caption["approval_state"] = "needs_review"
        return {"command_id": command_id, "type": kind, "caption_id": caption_id}

    if kind == "audio.mix":
        target = str(payload.get("target") or "dialogue")
        if target == "clip":
            clip_id = _stable_id(payload.get("clip_id"), "clip id")
            _, clip = _find_clip(project, clip_id)
            audio = clip.setdefault("audio", {"linked": True, "gain_db": 0.0, "muted": False})
            if "gain_db" in payload:
                gain = float(payload["gain_db"])
                if not -60 <= gain <= 24:
                    raise CutError("gain is outside the safe range")
                audio["gain_db"] = gain
            if "muted" in payload:
                audio["muted"] = bool(payload["muted"])
            return {"command_id": command_id, "type": kind, "target": target, "clip_id": clip_id}
        if target not in {"dialogue", "music", "master"}:
            raise CutError("unsupported audio mix target")
        mix = project["timeline"]["mix"].setdefault(target, {})
        if "normalize" in payload:
            mix["normalize"] = bool(payload["normalize"])
        if "target_lufs" in payload:
            target_lufs = float(payload["target_lufs"])
            if not -70 <= target_lufs <= 0:
                raise CutError("target LUFS is outside the safe range")
            mix["target_lufs"] = target_lufs
        if "gain_db" in payload:
            gain = float(payload["gain_db"])
            if not -60 <= gain <= 24:
                raise CutError("gain is outside the safe range")
            mix["gain_db"] = gain
        return {"command_id": command_id, "type": kind, "target": target}

    if kind == "color.apply":
        clip_id = _stable_id(payload.get("clip_id"), "clip id")
        _find_clip(project, clip_id)
        intensity = float(payload.get("intensity", 1.0))
        if not 0 <= intensity <= 1:
            raise CutError("color intensity must be between 0 and 1")
        preset = str(payload.get("preset") or "neutral")
        if preset not in COLOR_PRESETS:
            raise CutError(f"unsupported color preset: {preset}")
        project["timeline"]["color"][clip_id] = {"preset": preset, "intensity": intensity}
        return {"command_id": command_id, "type": kind, "clip_id": clip_id}

    if kind in {"render.preview", "render.master"}:
        mode = kind.split(".", 1)[1]
        explicit = payload.get("explicit_approval") is True
        if mode == "master" and (
            project["approval"].get("master_render_approved") is not True or not explicit
        ):
            raise CutError(
                "master render requires project and explicit command approval"
            )
        render_request = dict(payload)
        render_request.setdefault("quality", "master" if mode == "master" else "preview")
        settings = validate_export_request(project, render_request)
        settings.pop("candidate_not_final", None)
        plan = {
            "id": f"render-plan-{_digest({'mode': mode, 'settings': settings, 'revision': project['revision']})[:16]}",
            "mode": mode,
            "settings": settings,
            "explicit_approval": explicit,
            "generated_fixture_only": bool(payload.get("generated_fixture_only", False)),
            "execution_state": "planned_not_executed",
            "candidate_not_final": True,
            "publication_authorized": False,
            "cpu_only": True,
        }
        project["render_plans"].append(plan)
        return {"command_id": command_id, "type": kind, "render_plan_id": plan["id"]}

    raise CutError(f"command not implemented: {kind}")


def apply_commands(
    project: Mapping[str, Any],
    commands: Sequence[Mapping[str, Any]],
    *,
    actor: str,
    asset_resolver: AssetResolver | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply a command batch to a clone, validating the whole result atomically.

    ``asset_resolver`` turns a gallery ``job_id`` into an asset record for ``clip.add``;
    without one, ``clip.add`` fails closed.
    """

    if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)) or not commands:
        raise CutError("transaction needs at least one command")
    validate_manifest(project)
    next_project = copy.deepcopy(dict(project))
    original_assets = copy.deepcopy(next_project.get("assets") or [])
    outputs = []
    seen_ids: set[str] = set()
    for raw in commands:
        if not isinstance(raw, Mapping):
            raise CutError("every command must be an object")
        command_id = _stable_id(raw.get("id"), "command id")
        if command_id in seen_ids:
            raise CutError("duplicate command id in transaction")
        seen_ids.add(command_id)
        outputs.append(_apply_one(next_project, raw, actor, asset_resolver))
    new_assets = next_project.get("assets") or []
    if new_assets[: len(original_assets)] != original_assets:
        raise CutError("commands may not mutate immutable asset originals")
    next_project["revision"] = int(project["revision"]) + 1
    next_project["version_history"].append(
        {
            "revision": next_project["revision"],
            "transaction_id": "unpersisted",
            "actor": actor,
            "command_ids": sorted(seen_ids),
            "command_types": [str(item.get("type")) for item in commands],
        }
    )
    validate_manifest(next_project)
    return next_project, outputs


def project_diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Return an exact deterministic JSON-pointer-like diff."""

    ignored = {"version_history", "history_state", "pending_transactions"}
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        result: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            if not path and key in ignored:
                continue
            child = f"{path}/{key}"
            if key not in before:
                result.append({"op": "add", "path": child, "after": copy.deepcopy(after[key])})
            elif key not in after:
                result.append({"op": "remove", "path": child, "before": copy.deepcopy(before[key])})
            else:
                result.extend(project_diff(before[key], after[key], child))
        return result
    if isinstance(before, list) and isinstance(after, list):
        if before == after:
            return []
        return [
            {
                "op": "replace",
                "path": path or "/",
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
            }
        ]
    if before != after:
        return [
            {
                "op": "replace",
                "path": path or "/",
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
            }
        ]
    return []


def build_demo_transaction(project: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build the reviewable Sparky scene-7 demonstration transaction."""

    scenes = (project.get("storyboard") or {}).get("scenes") or []
    if len(scenes) < 7:
        raise CutError("Sparky demonstration requires at least seven scenes")
    scene = scenes[6]
    previous = scenes[5]
    fps = int(project["settings"]["fps"])
    dialogue_id = f"clip-{scene['id']}-dialogue"
    _, dialogue = _find_clip(project, dialogue_id)
    trim_in = min(4, dialogue["duration_frames"] - 1)
    return [
        {
            "id": "demo-scene-replace",
            "type": "scene.replace",
            "payload": {"scene_id": scene["id"], "candidate_id": scene["candidate_ids"][1]},
        },
        {
            "id": "demo-dissolve",
            "type": "transition.set",
            "payload": {
                "from_clip_id": f"clip-{previous['id']}-main",
                "to_clip_id": f"clip-{scene['id']}-main",
                "kind": "dissolve",
                "duration_frames": 12,
            },
        },
        {
            "id": "demo-pre-vocal-trim",
            "type": "clip.trim",
            "payload": {
                "clip_id": dialogue_id,
                "trim_in_frames": trim_in,
                "trim_out_frames": dialogue["trim_out_frame"],
            },
        },
        {
            "id": "demo-dialogue-normalize",
            "type": "audio.mix",
            "payload": {"target": "dialogue", "normalize": True, "target_lufs": -16.0},
        },
        {
            "id": "demo-caption-regenerate",
            "type": "caption.generate",
            "payload": {"scene_id": scene["id"]},
        },
        {
            "id": "demo-preview",
            "type": "render.preview",
            "payload": {"format": "mp4", "quality": "preview", "include_audio": True, "fps": fps},
        },
    ]


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        # Windows does not support opening a directory through os.open for fsync.
        # The file is flushed before replacement; POSIX also syncs the directory.
        if not _WINDOWS:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class CutProjectStore:
    """Atomic project store with an append-only, crash-recoverable JSONL journal."""

    def __init__(
        self,
        path: str | Path,
        initial_manifest: Mapping[str, Any] | None = None,
        *,
        asset_resolver: AssetResolver | None = None,
    ):
        self.path = Path(path).expanduser().resolve()
        self.asset_resolver = asset_resolver
        # A project directory keeps `project.json` + `journal.jsonl`; a bare file keeps the
        # prototype's sidecar naming so older stores still open.
        if self.path.name == "project.json":
            self.journal_path = self.path.with_name("journal.jsonl")
            self.lock_path = self.path.with_name(".project.lock")
        else:
            self.journal_path = self.path.with_suffix(self.path.suffix + ".journal.jsonl")
            self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with self._locked():
            if not self.path.exists():
                if initial_manifest is None:
                    raise CutError("finishing project store is not initialized")
                validate_manifest(initial_manifest)
                _atomic_write(self.path, dict(initial_manifest))
                self._append_journal(
                    {
                        "transaction_id": "__initial__",
                        "status": "initial",
                        "base_revision": 0,
                        "revision": 0,
                        "project_after": copy.deepcopy(dict(initial_manifest)),
                        "result": {"status": "initial", "revision": 0},
                    }
                )

    @contextmanager
    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            with _exclusive_file_lock(handle):
                yield

    def load(self) -> dict[str, Any]:
        with self._locked():
            return self._load_unlocked()

    def _latest_applied(self) -> dict[str, Any] | None:
        records = self._journal()
        candidates = [
            record
            for record in records
            if record.get("project_after") and record.get("status") in {"initial", "applied"}
        ]
        if not candidates:
            return None
        return max(
            candidates, key=lambda record: (int(record.get("revision", 0)), records.index(record))
        )

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            validate_manifest(value)
        except (OSError, json.JSONDecodeError, CutError):
            value = None
        latest = self._latest_applied()
        if latest is None and value is None:
            raise CutError("stored finishing project is invalid and unrecoverable")
        if latest is not None and (
            value is None or int(latest.get("revision", 0)) > int(value.get("revision", -1))
        ):
            value = copy.deepcopy(latest["project_after"])
            validate_manifest(value)
            _atomic_write(self.path, value)
        if value is None:
            raise CutError("stored finishing project is invalid")
        return value

    def _journal(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        records = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def _append_journal(self, record: Mapping[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _supersede_stale_proposals(self, current_revision: int) -> None:
        latest: dict[str, dict[str, Any]] = {}
        for record in self._journal():
            transaction_id = str(record.get("transaction_id") or "")
            if transaction_id:
                latest[transaction_id] = record
        for proposal in latest.values():
            if (
                proposal.get("status") != "proposed"
                or proposal.get("base_revision") == current_revision
            ):
                continue
            result = copy.deepcopy(proposal.get("result") or {})
            result.update(
                {
                    "status": "superseded",
                    "revision": current_revision,
                    "reason": "project revision advanced before human review",
                }
            )
            self._append_journal({**proposal, "status": "superseded", "result": result})

    def _existing(self, transaction_id: str, request_sha256: str) -> dict[str, Any] | None:
        for record in reversed(self._journal()):
            if record.get("transaction_id") == transaction_id:
                if record.get("request_sha256") != request_sha256:
                    raise CutError("transaction id collision")
                return copy.deepcopy(record.get("result"))
        return None

    def _normal_transaction(
        self,
        project: dict[str, Any],
        commands: Sequence[Mapping[str, Any]],
        actor: str,
        transaction_id: str,
        proposed: bool,
        request_sha256: str,
    ) -> dict[str, Any]:
        after, outputs = apply_commands(
            project, commands, actor=actor, asset_resolver=self.asset_resolver
        )
        after["version_history"][-1]["transaction_id"] = transaction_id
        diff = project_diff(project, after)
        result = {
            "status": "proposed" if proposed else "applied",
            "transaction_id": transaction_id,
            "base_revision": project["revision"],
            "revision": project["revision"] if proposed else after["revision"],
            "commands": copy.deepcopy(list(commands)),
            "outputs": outputs,
            "diff": diff,
            "proposed_project_sha256": _digest(after),
            "candidate_not_final": True,
        }
        record = {
            "transaction_id": transaction_id,
            "request_sha256": request_sha256,
            "status": result["status"],
            "actor": actor,
            "base_revision": project["revision"],
            "revision": result["revision"],
            "commands": copy.deepcopy(list(commands)),
            "project_before": project,
            "project_after": after,
            "result": result,
        }
        if proposed:
            self._append_journal(record)
            return result
        after["history_state"]["undo_stack"].append(transaction_id)
        after["history_state"]["redo_stack"] = []
        record["project_after"] = after
        self._append_journal(record)
        _atomic_write(self.path, after)
        self._supersede_stale_proposals(after["revision"])
        return result

    def _snapshot_for_transaction(self, transaction_id: str) -> dict[str, Any]:
        for record in reversed(self._journal()):
            if record.get("transaction_id") == transaction_id and record.get("status") == "applied":
                return copy.deepcopy(record)
        raise CutError("history transaction is unavailable")

    def _history_command(
        self,
        project: dict[str, Any],
        command: Mapping[str, Any],
        actor: str,
        transaction_id: str,
        request_sha256: str,
    ) -> dict[str, Any]:
        kind = command["type"]
        payload = command.get("payload") or {}
        state = copy.deepcopy(project["history_state"])
        if kind == "undo":
            if not state["undo_stack"]:
                raise CutError("nothing to undo")
            target_id = state["undo_stack"].pop()
            target = self._snapshot_for_transaction(target_id)
            after = copy.deepcopy(target["project_before"])
            state["redo_stack"].append(target_id)
        elif kind == "redo":
            if not state["redo_stack"]:
                raise CutError("nothing to redo")
            target_id = state["redo_stack"].pop()
            target = self._snapshot_for_transaction(target_id)
            after = copy.deepcopy(target["project_after"])
            state["undo_stack"].append(target_id)
        else:
            wanted = _integer(payload.get("revision"), "restore revision")
            after = None
            if wanted == 0:
                initial = next(
                    (record for record in self._journal() if record.get("status") == "initial"),
                    None,
                )
                after = copy.deepcopy(initial.get("project_after")) if initial else None
            if after is None:
                for record in reversed(self._journal()):
                    if (
                        record.get("status") == "applied"
                        and record.get("revision") == wanted
                        and record.get("project_after")
                    ):
                        after = copy.deepcopy(record["project_after"])
                        break
            if after is None:
                raise CutError("restore revision is unavailable")
            state["undo_stack"] = []
            state["redo_stack"] = []
            target_id = f"revision-{wanted}"
        after["revision"] = project["revision"] + 1
        after["history_state"] = state
        after["version_history"] = copy.deepcopy(project["version_history"])
        after["version_history"].append(
            {
                "revision": after["revision"],
                "transaction_id": transaction_id,
                "actor": actor,
                "command_ids": [command["id"]],
                "command_types": [kind],
                "target": target_id,
            }
        )
        validate_manifest(after)
        result = {
            "status": "applied",
            "transaction_id": transaction_id,
            "base_revision": project["revision"],
            "revision": after["revision"],
            "commands": [copy.deepcopy(dict(command))],
            "outputs": [{"command_id": command["id"], "type": kind, "target": target_id}],
            "diff": project_diff(project, after),
            "candidate_not_final": True,
        }
        self._append_journal(
            {
                "transaction_id": transaction_id,
                "request_sha256": request_sha256,
                "status": "applied",
                "actor": actor,
                "base_revision": project["revision"],
                "revision": after["revision"],
                "commands": [copy.deepcopy(dict(command))],
                "project_before": project,
                "project_after": after,
                "result": result,
            }
        )
        _atomic_write(self.path, after)
        self._supersede_stale_proposals(after["revision"])
        return result

    def transact(
        self,
        commands: Sequence[Mapping[str, Any]],
        *,
        actor: str,
        transaction_id: str,
        expected_revision: int,
        proposed: bool = False,
    ) -> dict[str, Any]:
        transaction_id = _stable_id(transaction_id, "transaction id")
        request_sha256 = _digest(
            {
                "actor": actor,
                "commands": copy.deepcopy(list(commands)),
                "expected_revision": expected_revision,
                "proposed": proposed,
            }
        )
        with self._locked():
            project = self._load_unlocked()
            existing = self._existing(transaction_id, request_sha256)
            if existing is not None:
                return existing
            if project["revision"] != expected_revision:
                raise CutError(
                    f"revision conflict: expected {expected_revision}, current {project['revision']}"
                )
            if actor not in {"human", "sparky", "importer", "test", "cli", "agent"}:
                raise CutError("unsupported transaction actor")
            if actor == "sparky" and proposed is not True:
                raise CutError("Sparky transactions must be proposed for human review")
            if len(commands) == 1 and commands[0].get("type") in STATEFUL_STORE_COMMANDS:
                if proposed:
                    raise CutError("history commands cannot be proposed")
                return self._history_command(
                    project, commands[0], actor, transaction_id, request_sha256
                )
            if any(command.get("type") in STATEFUL_STORE_COMMANDS for command in commands):
                raise CutError("history commands must be standalone")
            return self._normal_transaction(
                project, commands, actor, transaction_id, proposed, request_sha256
            )

    def review(self, transaction_id: str, *, approve: bool, reviewer: str) -> dict[str, Any]:
        transaction_id = _stable_id(transaction_id, "transaction id")
        with self._locked():
            records = [
                record for record in self._journal() if record.get("transaction_id") == transaction_id
            ]
            if not records or records[-1].get("status") != "proposed":
                raise CutError("pending transaction is unavailable")
            proposal = records[-1]
            current = self._load_unlocked()
            if current["revision"] != proposal["base_revision"]:
                self._supersede_stale_proposals(current["revision"])
                raise CutError("revision conflict while reviewing proposal")
            if not approve:
                result = {
                    "status": "rejected",
                    "transaction_id": transaction_id,
                    "revision": current["revision"],
                    "reviewer": reviewer,
                }
                self._append_journal({**proposal, "status": "rejected", "result": result})
                return result
            after = copy.deepcopy(proposal["project_after"])
            after["history_state"]["undo_stack"].append(transaction_id)
            after["history_state"]["redo_stack"] = []
            result = copy.deepcopy(proposal["result"])
            result.update(
                {"status": "applied", "revision": after["revision"], "reviewer": reviewer}
            )
            self._append_journal(
                {**proposal, "status": "applied", "project_after": after, "result": result}
            )
            _atomic_write(self.path, after)
            self._supersede_stale_proposals(after["revision"])
            return result

    def pending(self) -> list[dict[str, Any]]:
        with self._locked():
            current_revision = self._load_unlocked()["revision"]
            latest: dict[str, dict[str, Any]] = {}
            for record in self._journal():
                txid = str(record.get("transaction_id") or "")
                latest[txid] = record
            return [
                copy.deepcopy(record["result"])
                for record in latest.values()
                if record.get("status") == "proposed"
                and record.get("base_revision") == current_revision
            ]

    def recover(self) -> dict[str, Any]:
        with self._locked():
            latest = self._latest_applied()
            if latest is None:
                raise CutError("no valid journal snapshot is available")
            value = copy.deepcopy(latest["project_after"])
            validate_manifest(value)
            _atomic_write(self.path, value)
            for temporary in self.path.parent.glob(f"{self.path.name}.tmp.*"):
                temporary.unlink(missing_ok=True)
            return {
                "status": "recovered",
                "revision": value["revision"],
                "transaction_id": latest["transaction_id"],
            }


# Compatibility name retained for the first prototype API and tests.
def load_storyboard_project(path: str | Path) -> dict[str, Any]:
    return import_storyboard_manifest(path)


def validate_export_request(
    project: Mapping[str, Any], raw_request: Mapping[str, Any] | None
) -> dict[str, Any]:
    if project.get("schema") == LEGACY_SCHEMA:
        project = migrate_manifest(project)
    validate_manifest(project)
    duration = _finite_number(project.get("duration_seconds"), "project duration", minimum=0.001)
    request = dict(raw_request or {})
    range_mode = str(request.get("range_mode") or "full")
    if range_mode not in {"full", "selection"}:
        raise CutError("export range must be full or selection")
    if range_mode == "full":
        start, end = 0.0, duration
    else:
        start = _finite_number(request.get("range_start_seconds"), "range start")
        end = _finite_number(request.get("range_end_seconds"), "range end", minimum=0.001)
        if start >= end or end > duration:
            raise CutError("export range must be inside the project duration")
    export_format = str(request.get("format") or "mp4").lower()
    quality = str(request.get("quality") or "high").lower()
    if export_format not in ALLOWED_FORMATS or quality not in ALLOWED_QUALITIES:
        raise CutError("unsupported export settings")
    return {
        "range_mode": range_mode,
        "range_start_seconds": round(start, 6),
        "range_end_seconds": round(end, 6),
        "duration_seconds": round(end - start, 6),
        "format": export_format,
        "quality": quality,
        "include_audio": bool(request.get("include_audio", True)),
        "burn_captions": bool(request.get("burn_captions", True)),
        "candidate_not_final": True,
    }


def build_ffmpeg_command(
    *, source: str | Path, output: str | Path, export_request: Mapping[str, Any]
) -> list[str]:
    source_path, output_path = Path(source), Path(output)
    if source_path.resolve() == output_path.resolve():
        raise CutError("source and output must be different files")
    request_format = str(export_request.get("format") or "")
    if request_format not in ALLOWED_FORMATS:
        raise CutError("unsupported export format")
    if output_path.suffix.lower() != f".{request_format}":
        raise CutError("output extension must match export format")
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-fflags", "+bitexact"]
    start = float(export_request.get("range_start_seconds", 0.0))
    if start > 0:
        command.extend(["-ss", f"{start:.6f}"])
    command.extend(["-i", str(source_path)])
    if export_request.get("range_mode") == "selection":
        command.extend(["-t", f"{float(export_request['duration_seconds']):.6f}"])
    command.extend(["-map", "0:v:0"])
    if export_request.get("include_audio", True):
        command.extend(["-map", "0:a:0?"])
    else:
        command.append("-an")
    crf = QUALITY_CRF[str(export_request.get("quality") or "high")]
    if request_format == "mp4":
        command.extend(
            [
                "-c:v",
                "libx264",
                "-crf",
                crf,
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "1",
                "-flags:v",
                "+bitexact",
                "-map_metadata",
                "-1",
            ]
        )
        if export_request.get("include_audio", True):
            command.extend(["-c:a", "aac", "-b:a", "192k", "-flags:a", "+bitexact"])
        command.extend(
            ["-movflags", "+faststart", "-metadata", "creation_time=1970-01-01T00:00:00Z"]
        )
    else:
        command.extend(
            ["-c:v", "libvpx-vp9", "-crf", crf, "-b:v", "0", "-threads", "1", "-map_metadata", "-1"]
        )
        if export_request.get("include_audio", True):
            command.extend(["-c:a", "libopus", "-b:a", "160k"])
    command.append(str(output_path))
    return command


def probe_media(path: str | Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels,duration,nb_frames",
            "-of",
            "json",
            str(Path(path)),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise CutError(f"ffprobe failed: {(result.stderr or '').strip()[-500:]}")
    data = json.loads(result.stdout)
    # File size is intentionally excluded from normalized equality; SHA covers exact bytes.
    data.get("format", {}).pop("size", None)
    return data


def render_single_source_export(
    *,
    source: str | Path,
    output: str | Path,
    export_request: Mapping[str, Any],
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    source_path, output_path = Path(source).resolve(), Path(output).resolve()
    if not source_path.is_file():
        raise CutError("export source is unavailable")
    if source_path == output_path:
        raise CutError("source and output must be different files")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(
        f".{output_path.stem}.{os.getpid()}.partial{output_path.suffix.lower()}"
    )
    temporary_output.unlink(missing_ok=True)
    command = build_ffmpeg_command(
        source=source_path, output=temporary_output, export_request=export_request
    )
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
        if (
            result.returncode != 0
            or not temporary_output.is_file()
            or temporary_output.stat().st_size == 0
        ):
            detail = (result.stderr or "ffmpeg produced no artifact").strip()[-600:]
            raise CutError(f"ffmpeg export failed: {detail}")
        probe = probe_media(temporary_output)
        streams = probe.get("streams") or []
        if not any(stream.get("codec_type") == "video" for stream in streams):
            raise CutError("ffmpeg candidate failed decode/probe qualification")
        size = temporary_output.stat().st_size
        digest = _digest(temporary_output)
        temporary_output.replace(output_path)
    except subprocess.TimeoutExpired as exc:
        temporary_output.unlink(missing_ok=True)
        raise CutError("ffmpeg export timed out") from exc
    except Exception:
        temporary_output.unlink(missing_ok=True)
        raise
    return {
        "schema": "media_lab.render_receipt.v1",
        "status": "candidate",
        "candidate_not_final": True,
        "publication_authorized": False,
        "path": str(output_path),
        "bytes": size,
        "sha256": digest,
        "command": command,
        "exit_code": result.returncode,
        "ffprobe": probe,
        "cpu_only": True,
    }


def build_queue_handoff(
    project: Mapping[str, Any],
    export_request: Mapping[str, Any],
    *,
    explicit_confirmation: bool,
) -> dict[str, Any]:
    if explicit_confirmation is not True:
        raise CutError("explicit confirmation is required before queue handoff")
    normalized = validate_export_request(project, export_request)
    source_refs = []
    for _, clip in _all_clips(project):
        asset_id = clip.get("asset_id")
        if not asset_id:
            continue
        asset = _find_by_id(project.get("assets") or [], asset_id, "asset")
        source_ref = str((asset.get("source") or {}).get("path") or "")
        if not source_ref.startswith("/media/") or Path(source_ref).name != source_ref[7:]:
            raise CutError(
                "queue handoff accepts only basename-only /media/ source references"
            )
        if source_ref not in source_refs:
            source_refs.append(source_ref)
    payload = {
        "project_id": project.get("project_id"),
        "revision": project.get("revision"),
        "source_sha256": (project.get("source") or {}).get("sha256"),
        "sources": source_refs,
        "export": normalized,
        "timeline": project.get("timeline"),
        "approval": project.get("approval"),
    }
    return {
        "schema": HANDOFF_SCHEMA,
        "request_id": f"finish-{_digest(payload)[:16]}",
        "candidate_not_final": True,
        "publication_authorized": False,
        "readiness": {
            "state": "ready" if source_refs else "needs_media",
            "renderable": bool(source_refs),
            "source_count": len(source_refs),
            "warning": None
            if source_refs
            else "Real storyboard media is not attached; no real footage has rendered.",
        },
        "queue": {
            "kind": "finishing_export",
            "dispatch_state": "not_submitted",
            "gpu_required": False,
        },
        "payload": payload,
    }


# ======================================================================================
# Gallery projects — the default way a Cut project is born
# ======================================================================================

def _even(value: Any, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    if number < 16:
        return fallback
    return number - (number % 2)


def _media_relative(path: Any) -> str:
    """Validate a portable library path without flattening nested folders."""
    text = str(path or "")
    if not text.startswith("/media/"):
        raise CutError("asset source must be a /media/ path")
    relative = text[7:]
    if (not relative or any(c in relative for c in "\\%?#:")
            or any(ord(c) < 32 for c in relative)
            or any(part in ("", ".", "..") or part.startswith(".") for part in relative.split("/"))):
        raise CutError("asset source has an unsafe media path")
    return relative


def _gallery_asset_record(item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a resolver's gallery item into an immutable asset record."""

    job_id = _stable_id(item.get("job_id") or item.get("id"), "gallery job id")
    kind = str(item.get("kind") or "").lower()
    if kind not in {"video", "image", "music"}:
        raise CutError(f"gallery item {job_id} is not a video, picture or song")
    name = _media_relative(item.get("path") or item.get("url"))
    poster = item.get("poster")
    poster_ref = f"/media/{_media_relative(poster)}" if poster else None
    duration = item.get("duration_seconds")
    if kind != "image":
        duration = _finite_number(duration, "source duration", minimum=0.04)
    return {
        "id": f"asset-{job_id}",
        "kind": kind,
        "role": "gallery_source",
        "job_id": job_id,
        "title": str(item.get("title") or job_id),
        "prompt": str(item.get("prompt") or ""),
        "source": {
            "path": f"/media/{name}",
            "sha256": _sha256(item.get("sha256"), "gallery item hash"),
            "immutable": True,
            "exists": bool(item.get("exists", True)),
        },
        "poster": poster_ref,
        "proxy_ref": None,
        "duration_seconds": round(float(duration), 6) if duration is not None else None,
        "width": _even(item.get("width"), 0) or None,
        "height": _even(item.get("height"), 0) or None,
        "fps": float(item["fps"]) if item.get("fps") else None,
        "has_audio": bool(item.get("has_audio", False)),
        "reference_metadata": {},
        "audio_metadata": {"has_audio": bool(item.get("has_audio", False))},
        "subtitle_metadata": {},
        "prompt_metadata": {"prompt": str(item.get("prompt") or "")},
        "approval_metadata": {"state": "candidate"},
    }


def _gallery_clip_records(
    project: Mapping[str, Any], asset: Mapping[str, Any], fps: int, still_seconds: float
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    job_id = asset["job_id"]
    scene_id = f"scene-{job_id}"
    candidate_id = f"candidate-{job_id}-a"
    label = asset.get("title") or job_id
    scene = {
        "id": scene_id,
        "index": len(project["storyboard"]["scenes"]) + 1,
        "label": label,
        "kind": asset["kind"],
        "narration": "",
        "visual": asset.get("prompt") or label,
        "engine_plan": "",
        "qa": [],
        "candidate_ids": [candidate_id],
        "active_candidate_id": candidate_id,
        "source_metadata": {"state": "ready", "asset_id": asset["id"]},
        "reference_metadata": {},
        "audio_metadata": {"asset_id": asset["id"] if asset.get("has_audio") else None},
        "subtitle_metadata": {"source": "typed", "generated": False},
        "prompt_metadata": {"prompt": asset.get("prompt") or "", "job_id": job_id},
        "approval_metadata": {"state": "candidate", "approved_candidate_id": None},
    }
    candidate = {
        "id": candidate_id,
        "scene_id": scene_id,
        "label": "Gallery source",
        "asset_id": asset["id"],
        "media_state": "ready" if asset["source"].get("exists", True) else "missing",
        "source_metadata": {
            "path": asset["source"]["path"],
            "sha256": asset["source"]["sha256"],
            "immutable": True,
        },
        **_metadata_block(),
    }
    if asset["kind"] == "image":
        frames = max(1, round(still_seconds * fps))
        source_frames = None
    else:
        frames = max(1, round(float(asset["duration_seconds"]) * fps))
        source_frames = frames
    suffix = "music" if asset["kind"] == "music" else "main"
    base_id = f"clip-{job_id}-{suffix}"
    clip_id, counter = base_id, 1
    existing = {clip["id"] for _, clip in _all_clips(project)}
    while clip_id in existing:
        counter += 1
        clip_id = f"{base_id}-{counter}"
    tracks = project["timeline"]["tracks"]
    track_type = "music" if asset["kind"] == "music" else "video"
    track = next((t for t in tracks if t["type"] == track_type), None)
    if track is None:
        raise CutError(f"project has no {track_type} track")
    clip = {
        "id": clip_id,
        "label": label,
        "scene_id": scene_id,
        "candidate_id": candidate_id,
        "start_frame": 0,
        "duration_frames": frames,
        "trim_in_frame": 0,
        "trim_out_frame": frames,
        "asset_id": asset["id"],
        "proxy_ref": None,
        "effects": [],
        "approval_state": "candidate",
        "media_kind": asset["kind"],
        "source_duration_frames": source_frames,
        "audio": {"linked": bool(asset.get("has_audio")), "gain_db": 0.0, "muted": False},
        "_track_id": track["id"],
    }
    return scene, candidate, clip


def build_gallery_project(
    project_id: str,
    title: str,
    items: Sequence[Mapping[str, Any]],
    *,
    still_seconds: float = DEFAULT_STILL_SECONDS,
    fps: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Build a v2 editing manifest whose clips are Media Lab gallery items.

    Every video or picture lands back-to-back on V1 (pictures as ``still_seconds``
    stills); every song lands on the Music track. Assets retain portable
    ``/media/<folder>/<file>`` references and hashes proving what was cut.
    """

    project_id = _stable_id(project_id, "project id")
    if not items:
        raise CutError("a project needs at least one gallery item")
    assets = [_gallery_asset_record(item) for item in items]
    visual = [a for a in assets if a["kind"] in {"video", "image"}]
    video = [a for a in assets if a["kind"] == "video"]
    if fps is None:
        source_fps = next((a["fps"] for a in video if a.get("fps")), None)
        fps = round(source_fps) if source_fps else DEFAULT_FPS
    fps = max(1, min(MAX_FPS, _integer(fps, "fps", minimum=1)))
    first = next((a for a in visual if a.get("width") and a.get("height")), None)
    width = _even(width, first["width"] if first else 1280)
    height = _even(height, first["height"] if first else 720)
    project: dict[str, Any] = {
        "schema": PROJECT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "title": str(title or "Untitled cut").strip()[:160] or "Untitled cut",
        "revision": 0,
        "source": {},
        "originals": {"immutable": True, "inventory": []},
        "approval": {
            "state": "candidate",
            "candidate_not_final": True,
            "private_internal_only": True,
            "publication_authorized": False,
            "creative_approval": "not_approved",
            "master_render_approved": False,
        },
        "settings": {
            "width": width,
            "height": height,
            "aspect_ratio": f"{width}:{height}",
            "fps": fps,
            "timebase": f"1/{fps}",
            "still_seconds": float(still_seconds),
        },
        "duration_frames": 0,
        "duration_seconds": 0.0,
        "storyboard": {"scenes": []},
        "assets": [],
        "candidates": [],
        "timeline": {
            "tracks": [
                {"id": "track-video-main", "name": "V1", "type": "video", "clips": []},
                {"id": "track-music-main", "name": "Music", "type": "music", "clips": []},
                {"id": "track-captions-main", "name": "Captions", "type": "captions", "clips": []},
            ],
            "transitions": [],
            "effects": [],
            "captions": {"items": [], "style": "media-lab-readable-v1"},
            "color": {},
            "mix": {
                "dialogue": {"normalize": False, "target_lufs": -16.0, "gain_db": 0.0},
                "music": {"gain_db": -12.0},
                "master": {"gain_db": 0.0},
            },
        },
        "version_history": [],
        "history_state": {"undo_stack": [], "redo_stack": []},
        "pending_transactions": [],
        "render_plans": [],
        "render_receipts": [],
        "delivery": {"status": "not_delivered", "publication_authorized": False, "destinations": []},
        "media_status": {},
        "provenance": {
            "editor_inspiration": "OpenCut Classic",
            "upstream_url": "https://github.com/OpenCut-app/OpenCut-Classic",
            "upstream_commit": "cf5e79e919144200294fb9fed22a222592a0aeea",
            "upstream_license": "MIT",
            "code_copied": False,
            "project_source": "gallery",
        },
    }
    seen_assets: set[str] = set()
    for asset in assets:
        if asset["id"] not in seen_assets:
            project["assets"].append(asset)
            seen_assets.add(asset["id"])
        scene, candidate, clip = _gallery_clip_records(project, asset, fps, still_seconds)
        track_id = clip.pop("_track_id")
        track = _find_by_id(project["timeline"]["tracks"], track_id, "track")
        clip["start_frame"] = max(
            (c["start_frame"] + c["duration_frames"] for c in track["clips"]), default=0
        )
        if all(s["id"] != scene["id"] for s in project["storyboard"]["scenes"]):
            project["storyboard"]["scenes"].append(scene)
            project["candidates"].append(candidate)
        track["clips"].append(clip)
    source_record = {
        "path": "gallery:" + ",".join(a["job_id"] for a in project["assets"]),
        "sha256": _digest([a["source"]["sha256"] for a in project["assets"]]),
        "immutable": True,
        "kind": "gallery_import",
        "job_ids": [a["job_id"] for a in project["assets"]],
    }
    project["source"] = source_record
    project["originals"]["inventory"] = [source_record]
    missing = sum(1 for a in project["assets"] if not a["source"].get("exists", True))
    project["media_status"] = {
        "real_media_rendered": False,
        "missing_asset_count": missing,
        "state": "ready" if not missing else "missing_media",
        "warning": None if not missing else "Some source files are missing from the gallery.",
    }
    project["version_history"] = [
        {
            "revision": 0,
            "transaction_id": "import",
            "actor": "importer",
            "summary": f"Imported {len(project['assets'])} gallery item(s).",
        }
    ]
    _set_duration(project)
    validate_manifest(project)
    return project


def probe_gallery_file(path: str | Path, *, media_root: str | Path | None = None) -> dict[str, Any]:
    """ffprobe a real media file into the resolver shape used by ``build_gallery_project``."""

    source = Path(path)
    if not source.is_file():
        raise CutError(f"media file is missing: {source.name}")
    suffix = source.suffix.lower()
    kind = {"mp4": "video", "mov": "video", "webm": "video", "m4v": "video", "mkv": "video",
            "png": "image", "jpg": "image", "jpeg": "image", "webp": "image",
            "mp3": "music", "wav": "music", "m4a": "music", "flac": "music", "aac": "music",
            "ogg": "music"}.get(suffix.lstrip("."), "")
    if not kind:
        raise CutError(f"unsupported media type: {suffix}")
    probe = probe_media(source)
    relative = source.resolve().relative_to(Path(media_root).resolve()).as_posix() if media_root is not None else source.name
    _media_relative(f"/media/{relative}")
    info: dict[str, Any] = {"path": f"/media/{relative}", "kind": kind, "exists": True,
                            "sha256": _digest(source), "has_audio": False}
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") == "video" and "width" not in info:
            info["width"], info["height"] = stream.get("width"), stream.get("height")
            rate = str(stream.get("r_frame_rate") or "0/1")
            try:
                num, den = rate.split("/")
                info["fps"] = float(num) / float(den) if float(den) else None
            except (TypeError, ValueError, ZeroDivisionError):
                info["fps"] = None
        elif stream.get("codec_type") == "audio":
            info["has_audio"] = True
    try:
        info["duration_seconds"] = float((probe.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        info["duration_seconds"] = None
    if kind == "image":
        info["duration_seconds"] = None
        info["has_audio"] = False
    return info


# ======================================================================================
# Multiple projects on disk
# ======================================================================================

def projects_dir(root: str | Path) -> Path:
    return Path(root).expanduser() / "cut" / "projects"


def project_dir(root: str | Path, project_id: str) -> Path:
    project_id = _stable_id(project_id, "project id")
    if project_id != Path(project_id).name or project_id.startswith("."):
        raise CutError("project id is not a valid directory name")
    return projects_dir(root) / project_id


def new_project_id() -> str:
    import uuid

    return f"cut-{uuid.uuid4().hex[:10]}"


def project_summary(project: Mapping[str, Any]) -> dict[str, Any]:
    tracks = (project.get("timeline") or {}).get("tracks") or []
    video = next((t for t in tracks if t.get("type") == "video"), {"clips": []})
    assets = {a["id"]: a for a in project.get("assets") or []}
    first_clip = next(iter(sorted(video.get("clips") or [], key=lambda c: c["start_frame"])), None)
    poster = None
    if first_clip and first_clip.get("asset_id") in assets:
        asset = assets[first_clip["asset_id"]]
        poster = asset.get("poster") or (asset["source"]["path"] if asset["kind"] == "image" else None)
    return {
        "project_id": project.get("project_id"),
        "title": project.get("title"),
        "revision": project.get("revision"),
        "duration_seconds": project.get("duration_seconds"),
        "clip_count": sum(len(t.get("clips") or []) for t in tracks),
        "asset_count": len(assets),
        "poster": poster,
        "media_state": (project.get("media_status") or {}).get("state"),
        "master_render_approved": (project.get("approval") or {}).get("master_render_approved", False),
        "project_source": (project.get("provenance") or {}).get("project_source", "storyboard"),
    }


def list_projects(root: str | Path) -> list[dict[str, Any]]:
    base = projects_dir(root)
    if not base.is_dir():
        return []
    rows = []
    for directory in sorted(base.iterdir()):
        manifest = directory / "project.json"
        if not manifest.is_file():
            continue
        try:
            project = CutProjectStore(manifest).load()
        except CutError:
            rows.append({"project_id": directory.name, "title": directory.name, "error": "unreadable"})
            continue
        summary = project_summary(project)
        summary["updated_at"] = int(manifest.stat().st_mtime)
        rows.append(summary)
    rows.sort(key=lambda row: row.get("updated_at", 0), reverse=True)
    return rows


def create_project(
    root: str | Path, manifest: Mapping[str, Any], *, asset_resolver: AssetResolver | None = None
) -> CutProjectStore:
    directory = project_dir(root, manifest["project_id"])
    if (directory / "project.json").exists():
        raise CutError(f"project already exists: {manifest['project_id']}")
    directory.mkdir(parents=True, exist_ok=True)
    return CutProjectStore(directory / "project.json", initial_manifest=manifest,
                           asset_resolver=asset_resolver)


def open_project(
    root: str | Path, project_id: str, *, asset_resolver: AssetResolver | None = None
) -> CutProjectStore:
    manifest = project_dir(root, project_id) / "project.json"
    if not manifest.is_file():
        raise CutError(f"unknown project: {project_id}")
    return CutProjectStore(manifest, asset_resolver=asset_resolver)


# ======================================================================================
# Whole-timeline FFmpeg render
# ======================================================================================

def _frames_to_seconds(frames: int, fps: int) -> float:
    return round(frames / fps, 6)


def _video_sequence(project: Mapping[str, Any]) -> list[dict[str, Any]]:
    """V1 as an ordered list of clip/gap items.  Overlapping clips fail closed."""

    tracks = project["timeline"]["tracks"]
    video = next((t for t in tracks if t.get("type") == "video"), None)
    if video is None or not video.get("clips"):
        raise CutError("the video track is empty — add a clip before rendering")
    items: list[dict[str, Any]] = []
    cursor = 0
    for clip in sorted(video["clips"], key=lambda c: (c["start_frame"], c["id"])):
        if clip["start_frame"] < cursor:
            raise CutError(f"clips overlap on V1 at frame {clip['start_frame']} ({clip['id']})")
        if clip["start_frame"] > cursor:
            items.append({"kind": "gap", "start_frame": cursor,
                          "duration_frames": clip["start_frame"] - cursor})
        items.append({"kind": "clip", "clip": clip, "start_frame": clip["start_frame"],
                      "duration_frames": clip["duration_frames"]})
        cursor = clip["start_frame"] + clip["duration_frames"]
    return items


def _transition_map(project: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    """Transition keyed by the index of the item it leads INTO.  Needs touching clips."""

    by_pair = {(t["from_clip_id"], t["to_clip_id"]): t for t in project["timeline"]["transitions"]}
    result: dict[int, dict[str, Any]] = {}
    for index in range(1, len(items)):
        previous, current = items[index - 1], items[index]
        if previous["kind"] != "clip" or current["kind"] != "clip":
            continue
        transition = by_pair.get((previous["clip"]["id"], current["clip"]["id"]))
        if not transition:
            continue
        limit = min(previous["duration_frames"], current["duration_frames"])
        if transition["duration_frames"] >= limit:
            raise CutError(
                f"transition {transition['id']} is longer than one of its clips"
            )
        result[index] = transition
    for transition in project["timeline"]["transitions"]:
        if not any(t["id"] == transition["id"] for t in result.values()):
            raise CutError(
                f"transition {transition['id']} needs two touching clips on V1"
            )
    return result


def timeline_to_render_frame(project: Mapping[str, Any], frame: int) -> int:
    """Map a timeline frame to the rendered frame (dissolves overlap clips, so the
    finished video is shorter than the timeline by each transition's length)."""

    items = _video_sequence(project)
    transitions = _transition_map(project, items)
    shrink = 0
    for index, transition in transitions.items():
        if items[index]["start_frame"] <= frame:
            shrink += int(transition["duration_frames"])
    return max(0, int(frame) - shrink)


def render_duration_frames(project: Mapping[str, Any]) -> int:
    items = _video_sequence(project)
    transitions = _transition_map(project, items)
    total = sum(int(item["duration_frames"]) for item in items)
    return total - sum(int(t["duration_frames"]) for t in transitions.values())


def _color_filter(spec: Mapping[str, Any] | None) -> str:
    if not spec:
        return ""
    preset = COLOR_PRESETS.get(str(spec.get("preset") or "neutral"), {})
    intensity = float(spec.get("intensity", 1.0))
    if not preset or intensity <= 0:
        return ""
    if "curves" in preset:
        return f",curves=preset={preset['curves']}"
    parts = []
    for key, target in preset["eq"].items():
        base = 0.0 if key == "brightness" else 1.0
        value = base + (target - base) * intensity
        parts.append(f"{key}={value:.4f}")
    return ",eq=" + ":".join(parts)


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def caption_cues(project: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Caption items in rendered seconds, sorted, empty text dropped."""

    fps = int(project["settings"]["fps"])
    cues = []
    for item in sorted(project["timeline"]["captions"]["items"], key=lambda i: (i["start_frame"], i["id"])):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start = timeline_to_render_frame(project, int(item["start_frame"]))
        end = timeline_to_render_frame(project, int(item["end_frame"]))
        if end <= start:
            continue
        cues.append({"id": item["id"], "text": text,
                     "start_seconds": _frames_to_seconds(start, fps),
                     "end_seconds": _frames_to_seconds(end, fps)})
    return cues


def write_srt(project: Mapping[str, Any], path: str | Path) -> list[dict[str, Any]]:
    cues = caption_cues(project)
    lines = []
    for number, cue in enumerate(cues, start=1):
        lines.append(str(number))
        lines.append(f"{_srt_timestamp(cue['start_seconds'])} --> {_srt_timestamp(cue['end_seconds'])}")
        lines.append(cue["text"])
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return cues


def _caption_image(text: str, width: int, height: int, path: Path) -> bool:
    """Render one readable caption card (media-lab-readable-v1) with Pillow's bundled
    font so the bytes do not depend on system fonts.  Returns False without Pillow."""

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    size = max(14, round(height * 0.052))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    max_width = int(width * 0.86)
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    lines = [line for line in lines if line] or [text]
    line_height = int(size * 1.28)
    pad_x, pad_y = int(size * 0.7), int(size * 0.42)
    text_width = max(int(draw.textlength(line, font=font)) for line in lines)
    box_w = min(width, text_width + pad_x * 2)
    box_h = line_height * len(lines) + pad_y * 2
    image = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)
    canvas.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=int(size * 0.5),
                             fill=(10, 7, 5, 170))
    y = pad_y
    for line in lines:
        line_w = int(draw.textlength(line, font=font))
        x = (box_w - line_w) // 2
        canvas.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 200))
        canvas.text((x, y), line, font=font, fill=(255, 255, 255, 250))
        y += line_height
    image.save(path, format="PNG", optimize=False)
    return True


def _asset_file(media_dir: Path, asset: Mapping[str, Any]) -> Path:
    name = _media_relative((asset.get("source") or {}).get("path"))
    root = media_dir.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise CutError("asset source escapes the media folder")
    if not path.is_file():
        raise CutError(f"missing media: /media/{name} ({asset.get('title') or asset.get('id')})")
    return path


def _audio_norm(gain_db: float) -> str:
    return (f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
            f",volume={gain_db:.3f}dB")


def plan_timeline_render(
    project: Mapping[str, Any],
    *,
    media_dir: str | Path,
    output: str | Path,
    export_request: Mapping[str, Any],
    work_dir: str | Path,
) -> dict[str, Any]:
    """Turn the whole timeline into one shell-free ffmpeg argv plus its sidecars.

    Returns ``{"command": [...], "expected_seconds": float, "captions": str, "sources": [...]}``.
    Sidecar files (caption cards, captions.srt) are written into ``work_dir``.
    """

    validate_manifest(project)
    media_dir, output_path, work = Path(media_dir), Path(output), Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    request_format = str(export_request.get("format") or "mp4")
    if request_format not in ALLOWED_FORMATS:
        raise CutError("unsupported export format")
    if output_path.suffix.lower() != f".{request_format}":
        raise CutError("output extension must match export format")
    quality = str(export_request.get("quality") or "preview")
    if quality not in ALLOWED_QUALITIES:
        raise CutError("unsupported export quality")
    include_audio = bool(export_request.get("include_audio", True))
    settings = project["settings"]
    fps, width, height = int(settings["fps"]), int(settings["width"]), int(settings["height"])
    assets = {a["id"]: a for a in project.get("assets") or []}
    color = project["timeline"].get("color") or {}
    mix = project["timeline"].get("mix") or {}
    items = _video_sequence(project)
    transitions = _transition_map(project, items)

    inputs: list[str] = []
    filters: list[str] = []
    sources: list[str] = []
    input_count = 0

    def add_input(args: list[str]) -> int:
        nonlocal input_count
        inputs.extend(args)
        input_count += 1
        return input_count - 1

    video_labels: list[str] = []
    audio_labels: list[str] = []
    for index, item in enumerate(items):
        duration_frames = int(item["duration_frames"])
        duration = _frames_to_seconds(duration_frames, fps)
        if item["kind"] == "gap":
            filters.append(f"color=c=black:s={width}x{height}:r={fps}:d={duration:.6f},format=yuv420p[v{index}]")
            if include_audio:
                filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a{index}]")
            video_labels.append(f"[v{index}]")
            audio_labels.append(f"[a{index}]")
            continue
        clip = item["clip"]
        asset = assets.get(clip.get("asset_id") or "")
        if asset is None:
            raise CutError(f"clip {clip['id']} has no media attached")
        path = _asset_file(media_dir, asset)
        sources.append(asset["source"]["path"])
        trim_in = _frames_to_seconds(int(clip["trim_in_frame"]), fps)
        fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p")
        grade = _color_filter(color.get(clip["id"]))
        if asset["kind"] == "image":
            source_index = add_input(["-loop", "1", "-framerate", str(fps), "-t", f"{duration:.6f}", "-i", str(path)])
            filters.append(f"[{source_index}:v]{fit},trim=end_frame={duration_frames},setpts=PTS-STARTPTS{grade}[v{index}]")
            if include_audio:
                filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a{index}]")
        else:
            seek = ["-ss", f"{trim_in:.6f}"] if trim_in > 0 else []
            # Decode one guard frame beyond the requested duration. Fractional
            # source timestamps can otherwise make input-scoped -t underfeed
            # the exact frame trim below after fps conversion.
            decode_duration = duration + (1.0 / fps)
            source_index = add_input(seek + ["-t", f"{decode_duration:.6f}", "-i", str(path)])
            filters.append(f"[{source_index}:v]{fit},trim=end_frame={duration_frames},setpts=PTS-STARTPTS{grade}[v{index}]")
            if include_audio:
                audio = clip.get("audio") or {}
                if asset.get("has_audio") and not audio.get("muted"):
                    gain = float(audio.get("gain_db", 0.0))
                    filters.append(
                        f"[{source_index}:a]{_audio_norm(gain)},atrim=duration={duration:.6f},"
                        f"asetpts=PTS-STARTPTS,apad=whole_dur={duration:.6f}[a{index}]"
                    )
                else:
                    filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a{index}]")
        video_labels.append(f"[v{index}]")
        audio_labels.append(f"[a{index}]")

    # join V1 left to right: xfade/acrossfade where a transition sits, concat otherwise
    current_v, current_a = video_labels[0], audio_labels[0]
    current_len = _frames_to_seconds(int(items[0]["duration_frames"]), fps)
    for index in range(1, len(items)):
        duration = _frames_to_seconds(int(items[index]["duration_frames"]), fps)
        transition = transitions.get(index)
        if transition:
            fade = _frames_to_seconds(int(transition["duration_frames"]), fps)
            name = XFADE_NAMES[transition["kind"]]
            filters.append(f"{current_v}{video_labels[index]}xfade=transition={name}:duration={fade:.6f}:offset={current_len - fade:.6f}[vx{index}]")
            if include_audio:
                filters.append(f"{current_a}{audio_labels[index]}acrossfade=d={fade:.6f}:c1=tri:c2=tri[ax{index}]")
            current_len = current_len + duration - fade
        else:
            if include_audio:
                filters.append(f"{current_v}{current_a}{video_labels[index]}{audio_labels[index]}concat=n=2:v=1:a=1[vx{index}][ax{index}]")
            else:
                filters.append(f"{current_v}{video_labels[index]}concat=n=2:v=1:a=0[vx{index}]")
            current_len += duration
        current_v, current_a = f"[vx{index}]", f"[ax{index}]"
    total_seconds = round(current_len, 6)

    # audio buses: clip sound (dialogue) → music mixed on top → master
    if include_audio:
        dialogue = mix.get("dialogue") or {}
        bus = f"{current_a}volume={float(dialogue.get('gain_db', 0.0)):.3f}dB"
        if dialogue.get("normalize"):
            bus += f",loudnorm=I={float(dialogue.get('target_lufs', -16.0)):.1f}:TP=-1.5:LRA=11"
        filters.append(f"{bus}[abus]")
        current_a = "[abus]"
        music_track = next((t for t in project["timeline"]["tracks"] if t.get("type") == "music"), None)
        music_labels = []
        for clip in sorted((music_track or {}).get("clips") or [], key=lambda c: (c["start_frame"], c["id"])):
            asset = assets.get(clip.get("asset_id") or "")
            if asset is None:
                raise CutError(f"music clip {clip['id']} has no media attached")
            path = _asset_file(media_dir, asset)
            sources.append(asset["source"]["path"])
            trim_in = _frames_to_seconds(int(clip["trim_in_frame"]), fps)
            duration = _frames_to_seconds(int(clip["duration_frames"]), fps)
            start = _frames_to_seconds(timeline_to_render_frame(project, int(clip["start_frame"])), fps)
            gain = float((mix.get("music") or {}).get("gain_db", 0.0)) + float((clip.get("audio") or {}).get("gain_db", 0.0))
            if (clip.get("audio") or {}).get("muted"):
                continue
            seek = ["-ss", f"{trim_in:.6f}"] if trim_in > 0 else []
            source_index = add_input(seek + ["-t", f"{duration:.6f}", "-i", str(path)])
            label = f"[am{source_index}]"
            delay = int(round(start * 1000))
            filters.append(
                f"[{source_index}:a]{_audio_norm(gain)},atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,"
                f"adelay={delay}|{delay}{label}"
            )
            music_labels.append(label)
        if music_labels:
            filters.append(f"{current_a}{''.join(music_labels)}amix=inputs={len(music_labels) + 1}:duration=first:normalize=0[amixed]")
            current_a = "[amixed]"
        master_gain = float((mix.get("master") or {}).get("gain_db", 0.0))
        filters.append(f"{current_a}volume={master_gain:.3f}dB,atrim=duration={total_seconds:.6f}[amaster]")
        current_a = "[amaster]"

    # captions: burned as readable cards when Pillow is here, else embedded as soft mov_text
    captions_mode = "none"
    cues = write_srt(project, work / "captions.srt")
    burn = bool(export_request.get("burn_captions", True)) and cues
    if burn:
        for number, cue in enumerate(cues):
            card = work / f"caption-{number:03d}.png"
            if not _caption_image(cue["text"], width, height, card):
                burn = False
                break
            source_index = add_input(["-i", str(card)])
            margin = int(height * 0.06)
            filters.append(
                f"{current_v}[{source_index}:v]overlay=x=(W-w)/2:y=H-h-{margin}:eof_action=repeat:"
                f"enable='between(t,{cue['start_seconds']:.3f},{cue['end_seconds']:.3f})'[vc{number}]"
            )
            current_v = f"[vc{number}]"
        captions_mode = "burned" if burn else "embedded"
    elif cues:
        captions_mode = "embedded"

    # Input seeking and timestamp rounding can leave a frame short or long on
    # some clips even after fps normalization. Extend the last decoded frame
    # indefinitely, then trim the complete composition to the timeline's exact
    # integer frame contract. This makes both underflow and overflow converge on
    # the manifest count instead of allowing a frozen short tail or lost frame.
    full_frame_count = render_duration_frames(project)
    filters.append(
        f"{current_v}tpad=stop_mode=clone:stop=-1,"
        f"trim=end_frame={full_frame_count},setpts=N/({fps}*TB)[vframes]"
    )
    current_v = "[vframes]"

    # optional range
    range_mode = str(export_request.get("range_mode") or "full")
    start = float(export_request.get("range_start_seconds", 0.0) or 0.0)
    end = float(export_request.get("range_end_seconds", total_seconds) or total_seconds)
    if range_mode == "selection":
        if start >= end or end > total_seconds + 0.001:
            raise CutError("export range must be inside the rendered duration")
        filters.append(f"{current_v}trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[vrange]")
        current_v = "[vrange]"
        if include_audio:
            filters.append(f"{current_a}atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[arange]")
            current_a = "[arange]"
        expected = round(end - start, 6)
    else:
        expected = total_seconds

    # FFmpeg requires every input before output-scoped options such as -map
    # and codecs. Register soft captions like any other input, including when
    # Pillow is unavailable and a requested burn-in falls back to subtitles.
    subtitle_index = None
    if request_format == "mp4" and captions_mode == "embedded":
        subtitle_index = add_input(["-i", str(work / "captions.srt")])

    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-fflags", "+bitexact", *inputs,
               "-filter_complex", ";".join(filters), "-map", current_v]
    if include_audio:
        command.extend(["-map", current_a])
    crf = QUALITY_CRF[quality]
    if request_format == "mp4":
        command.extend(["-c:v", "libx264", "-preset", QUALITY_X264_PRESET[quality], "-crf", crf,
                        "-pix_fmt", "yuv420p", "-r", str(fps), "-threads", "1", "-flags:v", "+bitexact",
                        "-map_metadata", "-1"])
        if include_audio:
            command.extend(["-c:a", "aac", "-b:a", "192k", "-flags:a", "+bitexact"])
        else:
            command.append("-an")
        if subtitle_index is not None:
            command.extend(["-map", f"{subtitle_index}:s", "-c:s", "mov_text"])
        command.extend(["-movflags", "+faststart", "-metadata", "creation_time=1970-01-01T00:00:00Z"])
    else:
        command.extend(["-c:v", "libvpx-vp9", "-crf", crf, "-b:v", "0", "-r", str(fps), "-threads", "1",
                        "-map_metadata", "-1"])
        if include_audio:
            command.extend(["-c:a", "libopus", "-b:a", "160k"])
        else:
            command.append("-an")
        if captions_mode == "embedded":
            captions_mode = "sidecar"  # webm: keep the .srt next to the file
    command.append(str(output_path))
    return {
        "command": command,
        "expected_seconds": expected,
        "captions": captions_mode,
        "caption_count": len(cues),
        "sources": sorted(set(sources)),
        "filter_count": len(filters),
        "input_count": input_count,
    }


def render_timeline(
    project: Mapping[str, Any],
    *,
    media_dir: str | Path,
    output: str | Path,
    export_request: Mapping[str, Any],
    work_dir: str | Path,
    timeout_seconds: int = 3600,
    progress: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Execute the whole-timeline plan and return a receipt with SHA-256 + ffprobe."""

    import threading

    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(
        f".{output_path.stem}.{os.getpid()}.partial{output_path.suffix.lower()}"
    )
    temporary_output.unlink(missing_ok=True)
    plan = plan_timeline_render(project, media_dir=media_dir, output=temporary_output,
                                export_request=export_request, work_dir=work_dir)
    command = list(plan["command"])
    expected = max(0.001, float(plan["expected_seconds"]))
    run_command = command[:-1] + ["-progress", "pipe:1", "-nostats", command[-1]]
    if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise CutError("render timeout must be a positive number")
    process = None
    readers: list[threading.Thread] = []
    stderr_tail = [""]
    reader_errors: list[Exception] = []
    try:
        process = subprocess.Popen(run_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)

        def _stderr() -> None:
            assert process.stderr is not None
            try:
                while chunk := process.stderr.read(4096):
                    stderr_tail[0] = (stderr_tail[0] + chunk)[-65536:]
            except Exception as error:
                reader_errors.append(error)
                process.kill()

        def _stdout() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    if progress and line.startswith("out_time_us="):
                        try:
                            done = int(line.split("=", 1)[1].strip()) / 1_000_000
                        except ValueError:
                            continue
                        progress(max(0.0, min(0.99, done / expected)))
            except Exception as error:
                reader_errors.append(error)
                process.kill()

        # Drain both pipes concurrently; enforce the deadline from process start,
        # not after stdout reaches EOF. No-progress and noisy processes both expire.
        readers = [threading.Thread(target=target, daemon=True) for target in (_stderr, _stdout)]
        for reader in readers:
            reader.start()
        exit_code = process.wait(timeout=timeout_seconds)
        for reader in readers:
            reader.join(timeout=5)
        if reader_errors:
            raise CutError("render progress reporting failed") from reader_errors[0]
        if exit_code != 0 or not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            detail = (stderr_tail[0] or "ffmpeg produced no artifact").strip()[-600:]
            raise CutError(f"ffmpeg render failed: {detail}")
        probe = probe_media(temporary_output)
        if not any(stream.get("codec_type") == "video" for stream in probe.get("streams") or []):
            raise CutError("render failed decode/probe qualification")
        size = temporary_output.stat().st_size
        digest = _digest(temporary_output)
        temporary_output.replace(output_path)
    except subprocess.TimeoutExpired as exc:
        raise CutError("ffmpeg render timed out") from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for reader in readers:
                reader.join(timeout=5)
            # Do not close a stream beneath a still-running callback/reader.
            if not any(reader.is_alive() for reader in readers):
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
        temporary_output.unlink(missing_ok=True)
    if progress:
        progress(1.0)
    final_command = command[:-1] + [str(output_path)]
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "candidate",
        "candidate_not_final": True,
        "publication_authorized": False,
        "path": str(output_path),
        "bytes": size,
        "sha256": digest,
        "command": final_command,
        "exit_code": exit_code,
        "ffprobe": probe,
        "cpu_only": True,
        "quality": str(export_request.get("quality") or "preview"),
        "format": str(export_request.get("format") or "mp4"),
        "expected_seconds": plan["expected_seconds"],
        "captions": plan["captions"],
        "caption_count": plan["caption_count"],
        "sources": plan["sources"],
        "project_id": project.get("project_id"),
        "project_revision": project.get("revision"),
        "project_sha256": _digest(project),
    }


# Compatibility names for the prototype's tests and scripts.
FinishingStudioError = CutError
FinishingProjectStore = CutProjectStore
