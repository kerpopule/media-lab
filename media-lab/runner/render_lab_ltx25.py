#!/usr/bin/env python3
"""One-shot, network-free Maestro v1.8.0 LTX-2.5 distilled default T2V+A runner.

Phase 2 black-and-white baseline. Untouched ltx2_25.json distilled defaults
except the three task-required bindings: exact prompt, 289 frames (12.0 s at
24 fps, 8k+1 contract), and 1280x704 (the closest 64-aligned frame size to
720p that the LTX-2.5 block contract permits; 720 is not divisible by 64).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP = Path('/opt/maestro/app/app')
sys.path.insert(0, str(APP))
os.chdir(APP)

import imageio_ffmpeg
import numpy as np
import soundfile as sf
import torch
from mmgp import offload, quant_router
from shared.utils import files_locator as fl
from models.ltx25.ltx25_handler import family_handler

EXPECTED = {
    APP / 'defaults/ltx2_25.json': '5d6465839e9128a37830fd38df7d9577492cf0651d9ae46991ebf33bd3ff5357',
    APP / 'models/ltx25/ltx25_handler.py': '345476e91de70b7286ebdbeb8ba4e8bc3c5808257bbaecb8cd1bdf382827ce77',
    APP / 'models/ltx2/ltx2.py': '754a092dc5c834dd080d7b6e741876dd1cb0cb538bfcc03154429edb5ced2bfe',
    APP / 'requirements.txt': '4bf19f8d06c9652c2ef69ab0198a6b1d905be44fef6ca01b786accb4555a045f',
}
REQUIRED_TRANSFORMER = 'ltx-2.5-22b-distilled_diffusion_model_int8_convrot.safetensors'
REQUIRED_VERSIONS = {
    'mmgp': '3.7.12',
    'diffusers': '0.36.0',
    'transformers': '4.57.1',
    'tokenizers': '0.22.1',
    'accelerate': '1.12.0',
    'av': '16.1.0',
}
FRAME_NUM = int(os.environ.get('LAB_FRAMES', '121'))   # 8k+1
WIDTH = int(os.environ.get('LAB_WIDTH', '1280'))
HEIGHT = int(os.environ.get('LAB_HEIGHT', '704'))
FPS = 24
assert (FRAME_NUM - 1) % 8 == 0 and WIDTH % 32 == 0 and HEIGHT % 32 == 0


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(8 << 20):
            h.update(block)
    return h.hexdigest()


def register_quant_handlers() -> None:
    quant_router.unregister_handler('.fp8_quanto_bridge')
    for handler in (
        'shared.qtypes.scaled_fp8',
        'shared.qtypes.nvfp4',
        'shared.qtypes.nunchaku_int4',
        'shared.qtypes.nunchaku_fp4',
        'shared.qtypes.int8_convrot',
        'shared.qtypes.gguf',
    ):
        quant_router.register_handler(handler)


def encode_joint_av(video: torch.Tensor, audio, sample_rate: int, output: Path) -> dict:
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    if not ffmpeg.is_file():
        raise FileNotFoundError(ffmpeg)
    video = video.detach().cpu()
    if video.dtype == torch.uint8:
        # LTX2 pipelines return uint8 RGB (0..255) as (C, F, H, W)
        pixels = video.permute(1, 2, 3, 0).numpy()
    else:
        pixels = (
            video.float().clamp(-1, 1).add(1).mul(127.5).round().byte()
            .permute(1, 2, 3, 0).numpy()
        )
    frame_count, height, width, channels = pixels.shape
    if channels != 3:
        raise RuntimeError(f'unexpected video channels {channels}')
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 1:
        audio = np.stack([audio, audio], axis=1)
    if audio.ndim != 2:
        raise RuntimeError(f'unexpected audio shape {audio.shape}')
    if audio.shape[0] in (1, 2) and audio.shape[1] > 2:
        audio = audio.T
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='maestro-ltx25-av-', dir='/tmp') as temp:
        wav = Path(temp) / 'native-model-audio.wav'
        sf.write(wav, audio, sample_rate, subtype='PCM_16')
        cmd = [
            str(ffmpeg), '-hide_banner', '-loglevel', 'info', '-y',
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x{height}',
            '-r', str(FPS), '-i', 'pipe:0', '-i', str(wav),
            '-map', '0:v:0', '-map', '1:a:0',
            '-c:v', 'libx264', '-preset', 'slow', '-crf', '10', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart', str(output),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        assert proc.stdin is not None
        try:
            for frame in pixels:
                proc.stdin.write(frame.tobytes())
        finally:
            proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError('joint A/V ffmpeg encode failed')
    return {
        'frames': int(frame_count), 'width': int(width), 'height': int(height),
        'fps': FPS, 'audio_samples': int(audio.shape[0]),
        'audio_channels': int(audio.shape[1]), 'audio_sampling_rate': int(sample_rate),
        'ffmpeg_executable': str(ffmpeg), 'ffmpeg_sha256': sha(ffmpeg),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--prompt-file', type=Path, required=True)
    parser.add_argument('--models', type=Path, default=Path('/models'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    started = time.time()

    source_hashes = {str(path): sha(path) for path in EXPECTED}
    for path, expected_hash in EXPECTED.items():
        if source_hashes[str(path)] != expected_hash:
            raise RuntimeError(f'Maestro source drift: {path}')
    runtime_versions = {name: metadata.version(name) for name in REQUIRED_VERSIONS}
    if runtime_versions != REQUIRED_VERSIONS:
        raise RuntimeError(f'locked runtime mismatch: {runtime_versions!r}')

    prompt_bytes = args.prompt_file.read_bytes()
    prompt_hash = hashlib.sha256(prompt_bytes).hexdigest()
    prompt = prompt_bytes.decode('utf-8')

    preset = json.loads((APP / 'defaults/ltx2_25.json').read_text())
    if preset['model'].get('ltx2_pipeline') != 'distilled':
        raise RuntimeError('preset pipeline drift')
    model_def = family_handler.query_model_def(
        'ltx2_25', {'architecture': 'ltx2_25', 'ltx2_pipeline': 'distilled'},
    )
    defaults: dict = {}
    family_handler.update_default_settings('ltx2_25', model_def, defaults)
    required_defaults = {
        'num_inference_steps': 8, 'guidance_scale': 1.0,
        'audio_guidance_scale': 1.0, 'alt_guidance_scale': 1.0,
        'alt_scale': 0.0, 'guidance_phases': 1,
        'video_length': 241, 'resolution': '1024x576',
        'image_prompt_type': '', 'audio_prompt_type': '',
        'denoising_strength': 1.0,
    }
    for key, expected_value in required_defaults.items():
        if defaults.get(key) != expected_value:
            # Media Lab tracks Maestro's defaults: follow updates, record them.
            print(f'DEFAULT_FOLLOWED {key}: {expected_value!r} -> {defaults.get(key)!r}', flush=True)

    transformer = args.models / REQUIRED_TRANSFORMER
    text_encoder = args.models / 'gemma4-12b-ltx-v1' / 'gemma4-12b-ltx-v1_int8_convrot.safetensors'
    required_assets = [
        transformer, text_encoder,
        args.models / 'ltx-2.5-22b_video_vae_bf16.safetensors',
        args.models / 'ltx-2.5-22b_audio_vae_bf16.safetensors',
        args.models / 'ltx-2.5-22b_vocoder_bf16.safetensors',
        args.models / 'ltx-2.5-22b_text_embedding_projection_bf16.safetensors',
        args.models / 'ltx-2.5-22b_video_embeddings_connector_int8_convrot.safetensors',
        args.models / 'ltx-2.5-22b_audio_embeddings_connector_int8_convrot.safetensors',
        args.models / 'ltx-2.5-spatial-upscaler-x2-1.0_bf16.safetensors',
        args.models / 'gemma4-12b-ltx-v1' / 'tokenizer.json',
        args.models / 'gemma4-12b-ltx-v1' / 'config.json',
    ]
    for asset in required_assets:
        if not asset.is_file():
            raise FileNotFoundError(asset)

    # Exactly one default random-seed resolution, after all static gates.
    # MAESTRO_LTX25_SEED_OVERRIDE re-encodes a prior candidate after a
    # harness-side encode bug (uint8 range); it must carry the recorded seed.
    override = os.environ.get('MAESTRO_LTX25_SEED_OVERRIDE')
    seed = int(override) if override else secrets.randbelow(1_000_000_000)
    transaction_id = f'maestro-ltx25-bw-{int(time.time())}-seed-{seed}'
    payload = {
        'schema': 'maestro_ltx25_v1.8.0_default_submission.v1',
        'source_commit': '9059c21ed3e73fa53702bf84a948dba1f7f7ce42',
        'transaction_id': transaction_id,
        'prompt_sha256': prompt_hash,
        'prompt': prompt,
        'model': str(transformer),
        'text_encoder': str(text_encoder),
        'seed': seed,
        'seed_source': 'Maestro default -1 resolved once by secrets.randbelow(1000000000)',
        'width': WIDTH, 'height': HEIGHT, 'fps': FPS, 'frame_num': FRAME_NUM,
        'duration_seconds': FRAME_NUM / FPS,
        'sampling_steps': 8, 'guidance_scale': 1.0, 'audio_guidance_scale': 1.0,
        'alt_guidance_scale': 1.0, 'alt_scale': 0.0,
        'pipeline': 'distilled', 'sample_solver': 'euler',
        'image_prompt_type': '', 'video_prompt_type': '', 'audio_prompt_type': '',
        'loras': [], 'adapters': [], 'sliding_window': False,
        'task_bindings_vs_untouched_default': {
            'prompt': 'task-required exact prompt',
            'video_length': f'241 -> {FRAME_NUM} (12 s comparison contract)',
            'resolution': f'1024x576 -> {WIDTH}x{HEIGHT} (720p target, 64-block contract)',
        },
    }
    args.payload.parent.mkdir(parents=True, exist_ok=True)
    args.payload.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    print('PROMPT_ACCEPTED', transaction_id, prompt_hash, flush=True)
    print('DEFAULT_SEED_RESOLVED_ONCE', seed, flush=True)

    register_quant_handlers()
    fl.set_checkpoints_paths([str(args.models)])
    model, pipe = family_handler.load_model(
        [str(transformer)], model_type='ltx2_25', base_model_type='ltx2_25',
        model_def={**model_def, 'architecture': 'ltx2_25', 'ltx2_pipeline': 'distilled'},
        dtype=torch.bfloat16,
        text_encoder_filename=str(text_encoder),
    )
    budgets = {'transformer': 100, 'text_encoder': 100, '*': 3000}
    offload.shared_state['_attention'] = 'sdpa'
    profile = offload.profile(
        pipe, profile_no=4, compile=False, quantizeTransformer=False, loras=[],
        perc_reserved_mem_max=.90, vram_safety_coefficient=.80,
        convertWeightsFloatTo=torch.bfloat16, budgets=budgets,
    )
    print('EXACT_MODEL_LOADS_COMPLETE', flush=True)
    result = model.generate(
        input_prompt=prompt,
        frame_num=FRAME_NUM, height=HEIGHT, width=WIDTH, fps=FPS,
        sampling_steps=8, guide_scale=1.0, audio_cfg_scale=1.0,
        alt_guide_scale=1.0, alt_scale=0.0,
        seed=seed, sample_solver='euler',
        video_prompt_type='',
    )
    if result is None:
        raise RuntimeError('native Maestro LTX-2.5 returned no result')
    media = encode_joint_av(
        result['x'], result['audio'], int(result['audio_sampling_rate']), args.output,
    )
    profile.release()
    receipt = {
        'schema': 'maestro_ltx25_v1.8.0_default_render_receipt.v1',
        'verdict': 'RENDERED_UNREVIEWED', 'transaction_id': transaction_id,
        'seed': seed, 'prompt_sha256': prompt_hash, 'payload_sha256': sha(args.payload),
        'source_hashes': source_hashes, 'runtime_versions': runtime_versions,
        'model_assets': {str(path): {'bytes': path.stat().st_size, 'sha256': sha(path)} for path in required_assets},
        'output': str(args.output), 'output_bytes': args.output.stat().st_size,
        'output_sha256': sha(args.output), 'elapsed_seconds': round(time.time() - started, 3),
        **media,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    print(json.dumps(receipt, indent=2, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
