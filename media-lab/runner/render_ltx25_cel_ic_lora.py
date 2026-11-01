#!/usr/bin/env python3
"""Fail-closed private validation runner for Baberg's LTX-2.5 cel-character IC-LoRA.

The adapter's model card explicitly says the ordinary IC-LoRA inference pipeline does
not work for this checkpoint and recommends the LTX trainer validation sampler.  This
wrapper uses that sampler without running a training step.  It refuses to continue if
the adapter, config, reference video, split LTX assets, or loaded checkpoint do not
match the pinned contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import torch
import yaml

PINNED_REPO = "Baberg/ltx-2.5-22b-ic-lora-cel-character"
PINNED_REVISION = "aa7dbaff083edb2e7403848b004f3d73d9e01980"
PINNED_LORA_SHA256 = "59a3b4a9f22fef0311686c4052b63f639044ca37f6812c2957fa89f6bfd5d8b2"
PINNED_CONFIG_SHA256 = "6b5fc020c520be434b7644408e1cbea14c65e533f5fcbdf6b0e0a822bf97421d"
EXPECTED_TENSOR_COUNT = 960
EXPECTED_LTX_SOURCE_COMMIT = "400fd31054597515f47125691032c04b1c3ee24e"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def require_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"{label} is missing or empty: {path}")
    return path


def check_adapter(path: Path) -> None:
    got = sha256(path)
    if got != PINNED_LORA_SHA256:
        raise RuntimeError(f"adapter hash mismatch: got {got}, expected {PINNED_LORA_SHA256}")
    with path.open("rb") as handle:
        header_len = struct.unpack("<Q", handle.read(8))[0]
        if not 0 < header_len < 10_000_000:
            raise RuntimeError(f"invalid safetensors header length: {header_len}")
        header = json.loads(handle.read(header_len))
    metadata = header.pop("__metadata__", {})
    if metadata:
        raise RuntimeError(f"unexpected adapter metadata: {metadata}")
    if len(header) != EXPECTED_TENSOR_COUNT:
        raise RuntimeError(f"adapter tensor count mismatch: got {len(header)}, expected {EXPECTED_TENSOR_COUNT}")
    if {entry.get("dtype") for entry in header.values()} != {"BF16"}:
        raise RuntimeError("adapter is not the expected all-BF16 checkpoint")
    if not all(name.startswith("diffusion_model.transformer_blocks.") for name in header):
        raise RuntimeError("adapter contains keys outside the expected LTX transformer LoRA namespace")


def ffprobe(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_frames,duration",
            "-show_entries", "format=duration,size", "-of", "json", str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    if not data.get("streams"):
        raise RuntimeError(f"no decodable video stream in {path}")
    return data


def parse_args() -> argparse.Namespace:
    home = Path.home()
    adapter_root = home / "models" / "Baberg" / f"ltx-2.5-22b-ic-lora-cel-character-{PINNED_REVISION[:8]}"
    base_root = home / "models" / "Lightricks" / "LTX-2.5-fast"
    parser = argparse.ArgumentParser()
    parser.add_argument("--ltx-repo", type=Path, default=home / "runtime" / "ltx25" / "LTX-2")
    parser.add_argument("--adapter", type=Path, default=adapter_root / "ltx25-iclora-cartoon-cum3250.safetensors")
    parser.add_argument("--source-config", type=Path, default=adapter_root / "config.yaml")
    parser.add_argument("--base-root", type=Path, default=base_root)
    parser.add_argument("--reference-video", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--width", type=int, default=1152)
    parser.add_argument("--height", type=int, default=672)
    parser.add_argument("--frames", type=int, default=97)
    parser.add_argument("--fps", type=float, default=24.0)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--preflight-only", action="store_true", help="Validate the pinned adapter and resolved config without loading models")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "WANDB_MODE": "disabled",
            "TOKENIZERS_PARALLELISM": "true",
            "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        }
    )

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; refusing a CPU fallback")
    if args.width % 32 or args.height % 32 or (args.frames - 1) % 8:
        raise RuntimeError("width/height must be divisible by 32 and (frames-1) must be divisible by 8")
    if not (1 <= args.steps <= 100):
        raise RuntimeError("steps must be in [1, 100]")
    if not args.prompt.strip():
        raise RuntimeError("prompt may not be empty")

    repo = args.ltx_repo.expanduser().resolve()
    if not (repo / "packages" / "ltx-trainer").is_dir():
        raise RuntimeError(f"LTX trainer checkout is missing: {repo}")
    live_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=True
    ).stdout.strip()

    adapter = require_file(args.adapter, "adapter")
    source_config = require_file(args.source_config, "pinned source config")
    if sha256(source_config) != PINNED_CONFIG_SHA256:
        raise RuntimeError("source config hash mismatch")
    check_adapter(adapter)
    reference = require_file(args.reference_video, "reference video")
    reference_probe = ffprobe(reference)

    base = args.base_root.expanduser().resolve()
    transformer = require_file(base / "diffusion_models" / "ltx-2.5-22b-dev-transformer-bf16.safetensors", "DEV transformer")
    text_encoder = require_file(base / "text_encoders" / "gemma4-12b-with-proj-ltx-2.5-bf16.safetensors", "Gemma text encoder")
    video_vae = require_file(base / "vae" / "ltx-2.5-video-vae-bf16.safetensors", "video VAE")

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = (args.work_dir or output.parent / f".{output.stem}-work").expanduser().resolve()
    validation_dir = work_dir / "validation"
    dummy_data = work_dir / "unused-preprocessed-data"
    validation_dir.mkdir(parents=True, exist_ok=True)
    dummy_data.mkdir(parents=True, exist_ok=True)
    # Config validation insists that the training strategy's data directories exist
    # even though this wrapper invokes only the validation sampler.
    for name in ("latents", "conditions", "reference_latents"):
        (dummy_data / name).mkdir(parents=True, exist_ok=True)

    config = yaml.safe_load(source_config.read_text())
    config["output_dir"] = str(validation_dir)
    config["seed"] = args.seed
    config["model"].update(
        {
            "model_path": str(transformer),
            "text_encoder_path": str(text_encoder),
            "video_vae_path": str(video_vae),
            "audio_vae_path": None,
            "training_mode": "lora",
            "load_checkpoint": str(adapter),
        }
    )
    config["data"]["preprocessed_data_root"] = str(dummy_data)
    config["optimization"]["steps"] = 1
    config["validation"].update(
        {
            "prompts": [args.prompt.strip()],
            "images": None,
            "reference_videos": [str(reference)],
            "video_dims": [args.width, args.height, args.frames],
            "frame_rate": args.fps,
            "seed": args.seed,
            "inference_steps": args.steps,
            "interval": 999,
            "include_reference_in_output": False,
            "skip_initial_validation": False,
            "generate_video": True,
            "generate_audio": False,
        }
    )
    config.setdefault("wandb", {})["enabled"] = False
    config.setdefault("checkpoints", {})["no_resume"] = True

    generated_config = work_dir / "resolved-config.yaml"
    generated_config.write_text(yaml.safe_dump(config, sort_keys=False))

    sys.path.insert(0, str(repo / "packages" / "ltx-trainer" / "src"))
    from ltx_trainer.config import LtxTrainerConfig
    from ltx_trainer.progress import TrainingProgress
    from ltx_trainer.trainer import LtxvTrainer

    trainer_config = LtxTrainerConfig(**config)
    if Path(trainer_config.model.load_checkpoint).resolve() != adapter:
        raise RuntimeError("resolved config lost the pinned adapter checkpoint")
    if trainer_config.model.training_mode != "lora":
        raise RuntimeError("resolved config is not in LoRA mode")
    if args.preflight_only:
        print(json.dumps({
            "status": "preflight-ok",
            "adapter_revision": PINNED_REVISION,
            "adapter_sha256": PINNED_LORA_SHA256,
            "actual_ltx_source_commit": live_commit,
            "expected_ltx_source_commit_from_model_card": EXPECTED_LTX_SOURCE_COMMIT,
            "reference_probe": reference_probe,
            "resolved_config": str(generated_config),
        }, indent=2, sort_keys=True))
        return 0
    trainer = LtxvTrainer(trainer_config)
    loaded = getattr(trainer, "_loaded_checkpoint_path", None)
    if loaded is None or Path(loaded).resolve() != adapter:
        raise RuntimeError("trainer did not load the pinned adapter; refusing base-model output")

    trainer._global_step = 0
    trainer._transformer.eval()
    progress = TrainingProgress(enabled=False, total_steps=1)
    results = trainer._validation_runner.run(
        transformer=trainer._transformer,
        step=0,
        output_dir=validation_dir,
        device=trainer._accelerator.device,
        progress=progress,
        wandb_run=None,
        work_items=[(0, True)],
    )
    if len(results) != 1:
        raise RuntimeError(f"expected exactly one validation result, got {results}")
    generated = Path(results[0][1]).resolve()
    require_file(generated, "generated video")
    shutil.copy2(generated, output)
    output_probe = ffprobe(output)

    receipt = {
        "status": "ok",
        "adapter_repo": PINNED_REPO,
        "adapter_revision": PINNED_REVISION,
        "adapter_sha256": PINNED_LORA_SHA256,
        "expected_ltx_source_commit_from_model_card": EXPECTED_LTX_SOURCE_COMMIT,
        "actual_ltx_source_commit": live_commit,
        "reference_video": str(reference),
        "reference_probe": reference_probe,
        "prompt": args.prompt.strip(),
        "seed": args.seed,
        "width": args.width,
        "height": args.height,
        "frames": args.frames,
        "fps": args.fps,
        "steps": args.steps,
        "output": str(output),
        "output_sha256": sha256(output),
        "output_probe": output_probe,
        "resolved_config": str(generated_config),
    }
    receipt_path = output.with_suffix(output.suffix + ".json")
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
