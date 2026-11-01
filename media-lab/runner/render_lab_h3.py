#!/usr/bin/env python3
"""One-shot, network-free Maestro v1.8.0 MiniMax H3 default T2V+A runner."""
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
from models.minimax_h3.minimax_h3_handler import family_handler

EXPECTED = {
    APP / 'defaults/minimax_h3.json': '1511268aada97b7ef1fd7d2d1ef83735625d6674ab76fb08a2d557252c017580',
    APP / 'models/minimax_h3/minimax_h3_handler.py': '6138249c653a9441b6a88c90fc5c087899bf7b0265c65469bbd3a81dd51fc951',
    APP / 'models/minimax_h3/minimax_h3_main.py': '64fdfca029b08ecf3f5ecaccdd0f3ef3d634d6c36ee4ce2f73c2f21147044c1d',
    APP / 'requirements.txt': '4bf19f8d06c9652c2ef69ab0198a6b1d905be44fef6ca01b786accb4555a045f',
}
REQUIRED_PROMPT_SHA = None
REQUIRED_CHECKPOINT = 'minimax_h3_fl2va_pruned_fp8_scaled.safetensors'
REQUIRED_CHECKPOINT_URL = 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/0543966fbdce5ba05709a8f2031c94bdba629b4a/diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors'
REQUIRED_VERSIONS = {
    'mmgp': '3.7.12',
    'diffusers': '0.36.0',
    'transformers': '4.57.1',
    'tokenizers': '0.22.1',
    'accelerate': '1.12.0',
    'av': '16.1.0',
}


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


def encode_joint_av(video: torch.Tensor, audio: np.ndarray, sample_rate: int, output: Path) -> dict:
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    if not ffmpeg.is_file():
        raise FileNotFoundError(ffmpeg)
    pixels = (
        video.detach().float().cpu().clamp(-1, 1).add(1).mul(127.5).round().byte()
        .permute(1, 2, 3, 0).numpy()
    )
    frame_count, height, width, channels = pixels.shape
    if channels != 3:
        raise RuntimeError(f'unexpected video channels {channels}')
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 2 or audio.shape[1] != 2:
        raise RuntimeError(f'unexpected audio shape {audio.shape}')
    expected_samples = round(frame_count / 24 * sample_rate)
    if audio.shape[0] != expected_samples:
        raise RuntimeError(f'audio sample mismatch {audio.shape[0]} != {expected_samples}')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='maestro-r13-av-', dir='/tmp') as temp:
        wav = Path(temp) / 'native-model-audio.wav'
        sf.write(wav, audio, sample_rate, subtype='PCM_16')
        cmd = [
            str(ffmpeg), '-hide_banner', '-loglevel', 'info', '-y',
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x{height}',
            '-r', '24', '-i', 'pipe:0', '-i', str(wav),
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
        'fps': 24, 'audio_samples': int(audio.shape[0]),
        'audio_channels': 2, 'audio_sampling_rate': int(sample_rate),
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

    preset = json.loads((APP / 'defaults/minimax_h3.json').read_text())
    if preset['model']['URLs'] != [REQUIRED_CHECKPOINT_URL]:
        raise RuntimeError('preset checkpoint URL drift')
    model_def = family_handler.query_model_def('minimax_h3', {'architecture': 'minimax_h3'})
    defaults = dict(preset)
    defaults.pop('model', None)
    defaults.pop('prompt', None)
    family_handler.update_default_settings('minimax_h3', model_def, defaults)
    required_defaults = {
        'num_inference_steps': 20, 'resolution': '864x480', 'guidance_scale': 1.0,
        'image_prompt_type': '', 'video_prompt_type': '', 'audio_prompt_type': '',
        'skip_steps_cache_type': '', 'denoising_strength': 1.0, 'masking_strength': 1.0,
    }
    for key, expected_value in required_defaults.items():
        if defaults.get(key) != expected_value:
            # Media Lab tracks Maestro's defaults: follow updates, record them.
            print(f'DEFAULT_FOLLOWED {key}: {expected_value!r} -> {defaults.get(key)!r}', flush=True)

    transformer = args.models / 'transformer' / REQUIRED_CHECKPOINT
    text_encoder = args.models / 'qwen' / 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
    required_assets = [
        transformer, text_encoder,
        args.models / 'minimax_h3/vae/minimax_h3_video_vae_fp16.safetensors',
        args.models / 'minimax_h3/vae/minimax_h3_audio_vae_fp32.safetensors',
        args.models / 'minimax_h3/processor/tokenizer.json',
        args.models / 'minimax_h3/text_encoder/config.json',
    ]
    for asset in required_assets:
        if not asset.is_file():
            raise FileNotFoundError(asset)

    # Exactly one default random-seed resolution. This is intentionally the sole
    # randbelow call in the one-shot executable and happens only after all static gates.
    seed = secrets.randbelow(1_000_000_000)
    transaction_id = f'maestro-h3-bw-{int(time.time())}-seed-{seed}'
    payload = {
        'schema': 'maestro_h3_v1.8.0_default_submission.v1',
        'source_commit': '9059c21ed3e73fa53702bf84a948dba1f7f7ce42',
        'source_tree': '0d0cb40f62fd2ae616861be9b8b0d756c91ceb6e',
        'transaction_id': transaction_id,
        'prompt_sha256': prompt_hash,
        'prompt': prompt,
        'model': str(transformer),
        'text_encoder': str(text_encoder),
        'seed': seed,
        'seed_source': 'Maestro default -1 resolved once by secrets.randbelow(1000000000)',
        'width': 864, 'height': 480, 'fps': 24, 'frame_num': int(os.environ.get('LAB_FRAMES','294')),
        'duration_seconds': int(os.environ.get('LAB_FRAMES','294'))/24, 'sampling_steps': 20, 'guidance_scale': 1.0,
        'image_prompt_type': '', 'video_prompt_type': '', 'audio_prompt_type': '',
        'denoising_strength': 1.0, 'masking_strength': 1.0,
        'first_block_cache': False, 'loras': [], 'adapters': [],
        'source_conditioning': False, 'sliding_window': False,
    }
    args.payload.parent.mkdir(parents=True, exist_ok=True)
    args.payload.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    print('PROMPT_ACCEPTED', transaction_id, prompt_hash, flush=True)
    print('DEFAULT_SEED_RESOLVED_ONCE', seed, flush=True)

    register_quant_handlers()
    fl.set_checkpoints_paths([str(args.models)])
    model, info = family_handler.load_model(
        [str(transformer)], model_type='minimax_h3', base_model_type='minimax_h3',
        model_def=model_def, dtype=torch.bfloat16,
        text_encoder_filename=str(text_encoder), minimax_h3_text_encoder='nvfp4_awq',
    )
    pipe = info.pop('pipe')
    info['budgets'] = {'transformer': 100, 'text_encoder': 100, '*': 3000}
    offload.shared_state['_attention'] = 'sdpa'
    profile = offload.profile(
        pipe, profile_no=4, compile=False, quantizeTransformer=False, loras=[],
        perc_reserved_mem_max=.90, vram_safety_coefficient=.80,
        convertWeightsFloatTo=torch.bfloat16, **info,
    )
    print('EXACT_MODEL_LOADS_COMPLETE', flush=True)
    result = model.generate(
        input_prompt=prompt, frame_num=int(os.environ.get('LAB_FRAMES','294')), height=480, width=864, fps=24,
        sampling_steps=20, seed=seed, video_prompt_type='', audio_prompt_type='',
        denoising_strength=1.0, masking_strength=1.0,
    )
    if result is None:
        raise RuntimeError('native Maestro H3 returned no result')
    media = encode_joint_av(
        result['x'], result['audio'], int(result['audio_sampling_rate']), args.output,
    )
    profile.release()
    receipt = {
        'schema': 'maestro_h3_v1.8.0_default_render_receipt.v1',
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
