"""Invariants for the single image model: Qwen-Image-2.1 only, with real upstream pins.

These tests are the guard rail for the 2026-09-20 decision that Qwen-Image-2.1 is the
studio's only image model. They fail if a second image model is quietly added back, if the
engine id stops satisfying the request schema pattern, if the pinned weights stop matching the
published sizes, or if the research-license terms are dropped from the catalog entry.
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

from media_lab_core import image_jobs
from media_lab_core.capacity_planner import Planner
from media_lab_core.image_request import MAX_STEPS, MIN_STEPS
from media_lab_core.model_catalog import load_model_catalog

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
MODEL_ID = "qwen-image-21"
REVISION = "b3179ad355be050328e483a9dfdd9e60cd62adfa"
ENGINE_ID = "qwen-image-21-gpu"
WEIGHTS = CONFIG / "qwen-image-21-weights.json"


def config(name):
    return json.loads((CONFIG / name).read_text())


def test_image_slot_allows_exactly_qwen_image_21():
    policy = config("capacity-policy.json")
    assert policy["slots"]["image"] == {
        "allowed": [MODEL_ID],
        "primary": MODEL_ID,
        "note": ("Qwen-Image-2.1 is the only image model. License is research/evaluation only; "
                 "commercial output needs a separate Qwen license (see CREDITS.md)."),
    }, "the image slot must allow exactly one model"


def test_no_other_image_model_survives_in_the_manifests():
    manifests = config("model-manifests.json")["manifests"]
    image_models = sorted(mid for mid, m in manifests.items() if m["slot"] == "image")
    assert image_models == [MODEL_ID], image_models
    assert "flux" not in manifests and "qwen-image" not in manifests


def test_manifest_entry_is_pinned_and_licensed():
    entry = config("model-manifests.json")["manifests"][MODEL_ID]
    assert entry["immutable_revision"] == REVISION
    assert len(entry["sha256"]) == 64 and int(entry["sha256"], 16) >= 0
    assert entry["license_name"] == "Qwen Research License"
    assert entry["commercial_use_requires_separate_license"] is True
    assert entry["terms_acceptance_required"] is True
    assert entry["measured"] is True, "the envelope must come from the qualification run, not from artifact sizes"


def test_engine_id_satisfies_the_request_schema_pattern():
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ENGINE_ID)
    assert image_jobs.ENGINE == ENGINE_ID


def test_steps_bound_admits_the_published_recipe():
    assert MIN_STEPS <= 40 <= MAX_STEPS, "the published 40-step recipe must be requestable"


def test_pinned_files_match_published_sizes_and_digests():
    pack = json.loads(WEIGHTS.read_text())
    assert pack["immutable_revision"] == REVISION
    assert pack["files"], "the weights manifest must list files"
    total = 0
    for row in pack["files"]:
        assert len(row["sha256"]) == 64, row["path"]
        assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), row["path"]
        assert row["bytes"] > 1_000_000_000, row["path"]
        total += row["bytes"]
    assert round(total / 1024**3, 2) == pack["total_gib"] == 30.84
    # the manifest's pack digest is the canonical identity of exactly these rows
    canon = json.dumps([{"path": r["path"], "bytes": r["bytes"], "sha256": r["sha256"]} for r in pack["files"]],
                       sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canon).hexdigest() == config("model-manifests.json")["manifests"][MODEL_ID]["sha256"]


def test_residency_policy_replaced_the_old_image_companions():
    policy = config("companion-residency-policy.json")
    members = policy["companion_slot"]["members"]
    assert MODEL_ID in members
    assert "qwen-image" not in members and "flux-kontext" not in members
    bounds = policy["measured_or_bounded_gib"][MODEL_ID]
    assert bounds["qualified_with_pplx"] is False
    assert bounds["status"].startswith("per-phase measured"), bounds["status"]
    assert "NOT qualified with the protected primary" in bounds["status"], bounds["status"]


def test_planner_refuses_image_plus_primary_language_after_the_measurement():
    """Measured, not declared: the clean per-phase run took MemAvailable to 69.06 GiB on an idle GB10.

    Warm residency of the protected primary (28 GiB hi) plus this pack (48 GiB hi) is 76 GiB
    against a 69.7 GiB pool, so the planner must refuse the pair. The pack's observed warm-steady
    consumption was 40.7 GiB, which would put the pair within about 1 GiB of the pool, so the
    envelope deliberately keeps the worst observed consumption until a co-residency canary runs.
    """
    planner = Planner(CONFIG / "capacity-budget.json", CONFIG / "model-manifests.json", CONFIG / "capacity-policy.json")
    plan = planner.plan({"language": "qwen", "image": MODEL_ID})
    assert plan["admitted"] is False, plan
    manifests = json.loads((CONFIG / "model-manifests.json").read_text())["manifests"]
    entry = manifests[MODEL_ID]
    assert entry["measured"] is True
    assert entry["measurement"]["state"] == "measured-per-phase"
    assert entry["measurement"]["mem_available_gib_floor"] == 69.06
    assert entry["measurement"]["device_gib_per_process"] == 38.4
    assert entry["phases"]["warm_idle"] == {"lo_gb": 38.0, "hi_gb": 48.0}, entry["phases"]["warm_idle"]
    assert entry["phases"]["active_inference"] == {"lo_gb": 44.0, "hi_gb": 48.0}, entry["phases"]["active_inference"]
    warm = [c for c in plan["checks"] if c["phase"] == "warm_idle"]
    assert warm and warm[0]["required_gb"] == 76.0, warm  # qwen 28 + image 48
    assert "warm-idle-overflow" in {b["kind"] for b in plan["blockers"]}, plan["blockers"]


def test_planner_refuses_the_image_model_in_the_video_slot():
    planner = Planner(CONFIG / "capacity-budget.json", CONFIG / "model-manifests.json", CONFIG / "capacity-policy.json")
    plan = planner.plan({"video": MODEL_ID})
    assert plan["admitted"] is False, plan
    kinds = {b["kind"] for b in plan["blockers"]}
    assert "model-not-allowed-in-slot" in kinds, kinds


def test_catalog_exposes_only_the_new_image_entry():
    catalog = load_model_catalog(ROOT / "config" / "models.example.toml")
    image_ids = sorted(m.id for m in catalog.values() if m.category == "image")
    assert image_ids == [MODEL_ID], image_ids
    row = catalog[MODEL_ID]
    assert row.license_name == "Qwen Research License" and row.license_url
    assert row.status == "qualified" and row.selectable, "a measured per-phase qualification makes the entry selectable"
    assert row.terms_acceptance_required is True, "the research license terms still gate installation"


def test_renderer_uses_the_reference_inference_settings():
    """Static contract: the renderer must run CFG 1 with the prefix KV cache.

    The reference DGX Spark image lab ships and benchmarks these two settings; dropping
    them silently costs throughput with no quality gain, so pin them in source.
    """
    source = (ROOT / "media_lab_core" / "image_render.py").read_text()
    assert "QwenImage21Pipeline" in source
    call = source.split("image = pipe(", 1)[1].split(".images[0]", 1)[0]
    assert "true_cfg_scale=1.0" in call
    assert "use_kv_cache=True" in call
    assert "guidance_scale" not in call, "this pipeline has no guidance_scale parameter"
    assert "torch.compile" not in source, "upstream: the 2.1 transformer breaks under torch.compile"


def test_runtime_lock_pins_the_pipeline_commit_and_versions():
    """The runtime lock must stay in step with the documented runtime.

    The lock is the canonical dependency set for the image host's pinned interpreter;
    if the diffusers commit or the torch/transformers floor drifts from what the
    docs and the installer entry advertise, one of the two is wrong.
    """
    lock = (ROOT / "media_lab_core" / "data" / "qwen-image-21-runtime.requirements.lock").read_text()
    assert "diffusers @ git+https://github.com/huggingface/diffusers@80c7ed262aeffbeb43ef13ae04baeb9b84515a69" in lock
    assert "torch==2.14.0+cu130" in lock
    assert "transformers==5.17.0" in lock
    doc = (ROOT / "docs" / "INDEPENDENT-HOST.md").read_text()
    installs = json.loads((CONFIG / "engine-installs.json").read_text())["image"]
    for surface in (doc, json.dumps(installs)):
        assert "80c7ed262aeffbeb43ef13ae04baeb9b84515a69" in surface, "runtime commit drifted"
    assert "QwenImage21Pipeline" in doc
