#!/usr/bin/env python3
"""One-shot HunyuanVideo-Avatar render inside the pinned Maestro v1.9 image.

Inputs are mounted read-only at /media; model files at /models; output at /work.
The host wrapper owns the canonical Spark GPU lock and exact service restoration.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

APP = Path("/opt/maestro/app/app")
sys.path.insert(0, str(APP))
os.chdir(APP)

# On aarch64, decord2 and ONNX Runtime have an allocator teardown conflict when
# decord is imported first. Maestro's shared utilities import decord before rembg,
# so initialize ONNX/rembg here before any Maestro module can import decord.
import rembg  # noqa: F401
import imageio_ffmpeg
import torch
from PIL import Image
from mmgp import offload, quant_router
from shared.utils import files_locator as fl


def register_quant_handlers() -> None:
    quant_router.unregister_handler(".fp8_quanto_bridge")
    for handler in (
        "shared.qtypes.scaled_fp8",
        "shared.qtypes.nvfp4",
        "shared.qtypes.nunchaku_int4",
        "shared.qtypes.nunchaku_fp4",
        "shared.qtypes.int8_convrot",
        "shared.qtypes.gguf",
    ):
        quant_router.register_handler(handler)


def encode_video(video: torch.Tensor, output: Path, fps: int) -> None:
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    video = video.detach().cpu()
    if video.dtype == torch.uint8:
        pixels = video.permute(1, 2, 3, 0).numpy()
    else:
        pixels = (
            video.float().clamp(-1, 1).add(1).mul(127.5).round().byte()
            .permute(1, 2, 3, 0).numpy()
        )
    frame_count, height, width, channels = pixels.shape
    if channels != 3:
        raise RuntimeError(f"unexpected video channels {channels}")
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "slow", "-crf", "10", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for frame in pixels:
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("Hunyuan Avatar video encode failed")
    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("Hunyuan Avatar produced no encoded video")
    print(
        f"HVA_ENCODE_DONE frames={frame_count} fps={fps} size={width}x{height} "
        f"bytes={output.stat().st_size}", flush=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--width", type=int, default=1120)
    ap.add_argument("--height", type=int, default=640)
    ap.add_argument("--frames", type=int, default=129)
    ap.add_argument("--steps", type=int, default=30)
    args = ap.parse_args()
    if (args.frames - 1) % 4:
        raise SystemExit("Hunyuan Avatar frames must satisfy 4k+1")
    if args.width % 16 or args.height % 16:
        raise SystemExit("Hunyuan Avatar dimensions must be divisible by 16")
    if args.steps != 30:
        raise SystemExit("Hunyuan Avatar canary is pinned to exactly 30 steps")
    for path in (args.image, args.audio):
        if not Path(path).is_file() or Path(path).stat().st_size <= 0:
            raise SystemExit(f"missing Hunyuan Avatar input: {path}")

    print(
        f"HVA_LOAD_START image=media-lab-hunyuan-avatar:v1.9.0-r3 "
        f"model=hunyuan_video_avatar_720_quanto_bf16_int8.safetensors "
        f"seed={args.seed} frames={args.frames} fps=25 size={args.width}x{args.height} "
        f"steps={args.steps}", flush=True,
    )
    started = time.time()
    register_quant_handlers()
    fl.set_checkpoints_paths(["/models"])
    offload.shared_state["_attention"] = "sdpa"

    from models.hyvideo.hunyuan_handler import family_handler

    base = "hunyuan_avatar"
    model_def = family_handler.query_model_def(base, {"architecture": base})
    transformer = "/models/hunyuan_video_avatar_720_quanto_bf16_int8.safetensors"
    text_encoder = (
        "/models/llava-llama-3-8b/"
        "llava-llama-3-8b-v1_1_vlm_quanto_int8.safetensors"
    )
    model, pipe = family_handler.load_model(
        [transformer], model_type=base, base_model_type=base,
        model_def={**model_def, "architecture": base},
        dtype=torch.bfloat16, VAE_dtype=torch.float32,
        text_encoder_filename=text_encoder,
    )
    # Maestro v1.9's audio pipeline reads this cancellation flag but its
    # constructor never initializes it outside the Gradio controller path.
    # The one-shot runner owns cancellation via its process group, so initialize
    # the missing pipeline state explicitly and fail closed on actual signals.
    model.pipeline._interrupt = False
    # The Gradio controller normally installs a DynamicClass cache descriptor on
    # the transformer before generation. This canary intentionally uses the
    # untouched no-cache path, so initialize the field to the controller's exact
    # no-cache value rather than allowing an absent-attribute crash.
    model.pipeline.transformer.cache = None
    offload.profile(
        pipe, profile_no=4, compile=False, quantizeTransformer=False, loras=[],
        perc_reserved_mem_max=.90, vram_safety_coefficient=.80,
        convertWeightsFloatTo=torch.bfloat16,
        budgets={"transformer": 100, "text_encoder": 100,
                 "text_encoder_2": 100, "vae": 100, "wav2vec": 100, "*": 3000},
    )
    print(f"HVA_MODEL_READY load_seconds={time.time()-started:.1f}", flush=True)

    with Image.open(args.image) as im:
        reference = im.convert("RGB").copy()
    render_started = time.time()
    video = model.generate(
        input_prompt=args.prompt,
        input_ref_images=[reference],
        audio_guide=args.audio,
        fps=25,
        width=args.width,
        height=args.height,
        frame_num=args.frames,
        seed=args.seed,
        sampling_steps=args.steps,
        guide_scale=7.5,
        shift=5.0,
        embedded_guidance_scale=6.0,
        cfg_star_switch=False,
        fit_into_canvas=True,
    )
    if video is None:
        raise RuntimeError("Hunyuan Avatar returned no video tensor")
    print(
        f"HVA_INFERENCE_DONE seconds={time.time()-render_started:.1f} "
        f"shape={tuple(video.shape)} dtype={video.dtype}", flush=True,
    )
    encode_video(video, Path(args.output), 25)
    print(f"HVA_DONE total_seconds={time.time()-started:.1f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
