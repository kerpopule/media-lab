#!/usr/bin/env python3
"""Media Lab warm engine server — runs INSIDE the maestro container.

Loads one video model family ONCE (ltx25 or h3), then serves:
  GET  /health              -> {"ok": true, "engine": ..., "loaded": true}
  POST /generate {prompt, frames, width, height} -> renders with the resident
       model, writes /work/out/<id>.mp4, returns {"ok": true, "file": ...}

Env: ENGINE=ltx25|h3, PORT, and optional LTX_PIPELINE=distilled|dev. Load pattern copied from render_lab_ltx25.py /
render_lab_h3.py (register_quant_handlers, fl.set_checkpoints_paths(['/models']),
offload.profile, uint8-capable encode_joint_av). Single-flight via a lock —
residency is parallel, generation is one-at-a-time.
"""
from __future__ import annotations

import base64, importlib.util, json, os, secrets, shutil, subprocess, sys, tempfile, threading, time, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path('/opt/maestro/app/app')
sys.path.insert(0, str(APP))
os.chdir(APP)

ENGINE = os.environ.get('ENGINE', 'ltx25')
LEASE_ENGINE = 'ltx' if ENGINE == 'ltx25' else ENGINE
PORT = int(os.environ.get('PORT', '8290'))
LTX_PIPELINE = os.environ.get('LTX_PIPELINE', 'distilled').strip().lower()
if LTX_PIPELINE not in ('distilled', 'dev'):
    raise RuntimeError(f'unsupported LTX pipeline: {LTX_PIPELINE!r}')
LTX_TRANSFORMER = os.environ.get(
    'LTX_TRANSFORMER',
    ('ltx-2.5-22b-distilled_diffusion_model_int8_convrot.safetensors'
     if LTX_PIPELINE == 'distilled'
     else 'ltx-2.5-22b-dev-transformer-bf16.safetensors'),
).strip()
OUT = Path('/work/out')
OUT.mkdir(parents=True, exist_ok=True)
LTX_INPUT_DIR = OUT / 'inputs'
FPS = 24

import imageio_ffmpeg
import numpy as np
import soundfile as sf
import torch
from mmgp import offload, quant_router
from shared.utils import files_locator as fl


def _install_decord_compat():
    """Provide the tiny decord surface LTX native-retake needs via PyAV.

    The pinned Maestro image ships PyAV but omits its optional decord package.
    Keep this bounded to one short studio clip; do not emulate decord generally.
    """
    if importlib.util.find_spec('decord') is not None:
        return 'native'
    import av

    class VideoReader:
        MAX_FRAMES = 512
        MAX_SIDE = 4096

        def __init__(self, path):
            container = av.open(str(path))
            try:
                stream = container.streams.video[0]
                self._fps = float(stream.average_rate or 24.0)
                self._frames = []
                for frame in container.decode(video=0):
                    pixels = frame.to_ndarray(format='rgb24')
                    height, width = pixels.shape[:2]
                    if height > self.MAX_SIDE or width > self.MAX_SIDE:
                        raise ValueError('retake input exceeds 4096px compatibility limit')
                    self._frames.append(pixels)
                    if len(self._frames) > self.MAX_FRAMES:
                        raise ValueError('retake input exceeds 512-frame compatibility limit')
            finally:
                container.close()
            if not self._frames:
                raise ValueError('retake input has no decodable video frames')

        def __len__(self):
            return len(self._frames)

        def __getitem__(self, index):
            return self._frames[index]

        def get_avg_fps(self):
            return self._fps

    module = types.ModuleType('decord')
    setattr(module, 'VideoReader', VideoReader)
    sys.modules['decord'] = module
    return 'pyav-compat'


def _install_ltx_retake_dtype_compat(ltx_model):
    """Align cached Gemma states on the two live native-retake connectors.

    Maestro's retake cache materializes float32 text states while its pinned LTX
    connector weights are bfloat16. MMGP replaces ``forward`` on each connector
    instance after profiling, so a class-level wrapper never runs. Bind the shim
    to the loaded video/audio instances and cast each input only to its receiving
    projection's own weight dtype.
    """
    base_module = importlib.import_module(
        'models.ltx2.ltx_core.text_encoders.gemma.encoders.base_encoder')
    current = base_module._apply_feature_extractor
    if not getattr(current, '_media_lab_dtype_compat', False):
        def aligned(hidden_states, attention_mask, padding_side, feature_extractor):
            parameter = next(feature_extractor.parameters(), None)
            dtype = getattr(parameter, 'dtype', None)
            if dtype is not None:
                hidden_states = tuple(
                    tensor.to(dtype=dtype) if getattr(tensor, 'dtype', None) != dtype else tensor
                    for tensor in hidden_states
                )
            return current(hidden_states, attention_mask, padding_side, feature_extractor)

        setattr(aligned, '_media_lab_dtype_compat', True)
        setattr(base_module, '_apply_feature_extractor', aligned)

    connectors = []
    for name in ('video_embeddings_connector', 'audio_embeddings_connector'):
        connector = getattr(ltx_model, name, None)
        if connector is None:
            raise RuntimeError(f'LTX native retake is missing live {name}')
        connectors.append(connector)

    for connector in connectors:
        connector_forward = connector.forward
        if getattr(connector_forward, '_media_lab_dtype_compat', False):
            continue

        for block in connector.modules():
            gate = getattr(block, 'to_gate_logits', None)
            if gate is None or getattr(gate, '_media_lab_dtype_compat', False):
                continue
            gate_forward = gate.forward

            def aligned_gate(hidden_states, *args, _gate=gate,
                             _forward=gate_forward, **kwargs):
                weight = getattr(_gate, 'weight', None)
                dtype = getattr(weight, 'dtype', None)
                if (dtype is not None
                        and getattr(hidden_states, 'dtype', None) != dtype):
                    hidden_states = hidden_states.to(dtype=dtype)
                return _forward(hidden_states, *args, **kwargs)

            setattr(aligned_gate, '_media_lab_dtype_compat', True)
            setattr(gate, 'forward', aligned_gate)
            setattr(gate, '_media_lab_dtype_compat', True)

        def aligned_connector(hidden_states, attention_mask=None,
                              _connector=connector, _forward=connector_forward):
            parameter = next(_connector.parameters(), None)
            dtype = getattr(parameter, 'dtype', None)
            if dtype is not None and getattr(hidden_states, 'dtype', None) != dtype:
                hidden_states = hidden_states.to(dtype=dtype)
            return _forward(hidden_states, attention_mask)

        setattr(aligned_connector, '_media_lab_dtype_compat', True)
        setattr(connector, 'forward', aligned_connector)
    return 'hidden-state-and-live-connector-gates-dtype-aligned'


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


def encode_joint_av(video: torch.Tensor, audio, sample_rate: int, output: Path) -> None:
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
        raise RuntimeError(f'unexpected video channels {channels}')
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 1:
        audio = np.stack([audio, audio], axis=1)
    if audio.ndim != 2:
        raise RuntimeError(f'unexpected audio shape {audio.shape}')
    if audio.shape[0] in (1, 2) and audio.shape[1] > 2:
        audio = audio.T
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='media-lab-av-', dir='/tmp') as temp:
        wav = Path(temp) / 'native-model-audio.wav'
        sf.write(wav, audio, sample_rate, subtype='PCM_16')
        cmd = [
            str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-nostdin',
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


print(f'ENGINE_LOADING {ENGINE} pipeline={LTX_PIPELINE if ENGINE == "ltx25" else "n/a"}', flush=True)
register_quant_handlers()
fl.set_checkpoints_paths(['/models'])
offload.shared_state['_attention'] = 'sdpa'

if ENGINE == 'ltx25':
    import models.ltx25.ltx25_handler as ltx25_handler
    family_handler = ltx25_handler.family_handler
    model_def = family_handler.query_model_def(
        'ltx2_25', {'architecture': 'ltx2_25', 'ltx2_pipeline': LTX_PIPELINE})
    transformer = f'/models/{LTX_TRANSFORMER}'
    if not Path(transformer).is_file():
        raise FileNotFoundError(f'LTX transformer missing: {transformer}')
    # Maestro chooses connector precision from the transformer filename. The
    # official dev transformer is BF16, but the locally installed Maestro pack
    # intentionally carries its supported INT8 ConvRot connector pair instead
    # of duplicating another ~2 GB of projection weights. Connector checkpoints
    # are independently quantized components, so select that installed pair
    # explicitly while keeping the full BF16 dev transformer and 30-step path.
    bf16_connectors = (
        Path('/models/ltx-2.5-22b_video_embeddings_connector_bf16.safetensors'),
        Path('/models/ltx-2.5-22b_audio_embeddings_connector_bf16.safetensors'),
    )
    int8_connectors = (
        Path('/models/ltx-2.5-22b_video_embeddings_connector_int8_convrot.safetensors'),
        Path('/models/ltx-2.5-22b_audio_embeddings_connector_int8_convrot.safetensors'),
    )
    if LTX_PIPELINE == 'dev' and not all(p.is_file() for p in bf16_connectors):
        missing = [str(p) for p in int8_connectors if not p.is_file()]
        if missing:
            raise FileNotFoundError(f'LTX dev connector fallback missing: {missing}')
        ltx25_handler._native_connector_variant = lambda _path: 'int8'
        print('LTX_DEV_CONNECTORS int8_convrot (BF16 connector pair not installed)', flush=True)
    text_encoder = '/models/gemma4-12b-ltx-v1/gemma4-12b-ltx-v1_int8_convrot.safetensors'
    model, pipe = family_handler.load_model(
        [transformer], model_type='ltx2_25', base_model_type='ltx2_25',
        model_def={**model_def, 'architecture': 'ltx2_25', 'ltx2_pipeline': LTX_PIPELINE},
        dtype=torch.bfloat16, text_encoder_filename=text_encoder)
    profile = offload.profile(
        pipe, profile_no=4, compile=False, quantizeTransformer=False, loras=[],
        perc_reserved_mem_max=.90, vram_safety_coefficient=.80,
        convertWeightsFloatTo=torch.bfloat16,
        budgets={'transformer': 100, 'text_encoder': 100, '*': 3000})

    def generate(prompt: str, frames: int, width: int, height: int, seed: int, extra=None):
        # Per-request overrides win over the pipeline defaults; pop them out of
        # extra first or they arrive twice and python raises on the duplicate
        extra = dict(extra or {})
        tunable = (
            'sampling_steps', 'guide_scale', 'audio_cfg_scale',
            'alt_guide_scale', 'alt_scale', 'stg_scale', 'cfg_rescale',
            'modality_scale', 'perturbation_switch', 'perturbation_layers',
            'perturbation_start', 'perturbation_end', 'input_video_strength',
        )
        tuned = {k: extra.pop(k) for k in tunable if k in extra}
        if LTX_PIPELINE == 'dev':
            defaults = {
                # Official LTX-2.5 full/dev A2V guidance defaults. The supplied
                # audio remains frozen; modality_scale is the actual A2V lever.
                'sampling_steps': 30,
                'guide_scale': 3.0,
                'audio_cfg_scale': 7.0,
                'alt_guide_scale': 3.0,
                'alt_scale': 0.7,
                'stg_scale': 1.0,
                'cfg_rescale': 0.7,
                'modality_scale': 3.0,
                'perturbation_switch': 1,
                'perturbation_layers': [28],
                'perturbation_start': 0.0,
                'perturbation_end': 1.0,
                'input_video_strength': 0.7,
            }
        else:
            defaults = {
                'sampling_steps': 8,
                'guide_scale': 1.0,
                'audio_cfg_scale': 1.0,
                'alt_guide_scale': 1.0,
                'alt_scale': 0.0,
            }
        return model.generate(
            input_prompt=prompt, **extra,
            frame_num=frames, height=height, width=width, fps=FPS,
            **{**defaults, **tuned},
            seed=seed, sample_solver='euler', video_prompt_type='')
else:
    from models.minimax_h3.minimax_h3_handler import family_handler
    from shared.utils.loras_mutipliers import parse_loras_multipliers
    # ref2va is the ACTOR-CLONING checkpoint: it takes reference pictures of a
    # person and preserves their identity through the shot. fl2va only conditions
    # on a start frame, which is why its takes drifted off the character.
    # omni_reference is what switches the handler onto the reference pipeline.
    H3_VARIANT = os.environ.get('H3_VARIANT', 'fl2va')
    if H3_VARIANT not in ('fl2va', 'ref2va', 'fused_r1024'):
        raise RuntimeError(f'unsupported H3 variant: {H3_VARIANT!r}')
    # Explicit experimental gate for a fused FL2VA+Ref2VA checkpoint. The
    # promoted launch path never selects this variant; only a job carrying the
    # internal h3_fused_r1024 flag may request it. Maestro's omni/Ref2VA branch
    # silently ignores image_start.  It *does* honor a one-frame continuation
    # boundary as the shot's composition keyframe, while identity/audio remain in
    # the Ref2VA manifest.  Using any other route invalidates the A/B.
    H3_FUSED_COMBINED = H3_VARIANT == 'fused_r1024'
    H3_TURBO_PRESET = os.environ.get('H3_TURBO_PRESET', '').strip()
    H3_TURBO_STEPS = {'v4-6step': 6, 'v4-8step': 8}.get(H3_TURBO_PRESET)
    H3_TURBO_STRENGTH = 1.0
    H3_TURBO_FILENAME = 'minimax_h3_turbo_v4_step600_ema.safetensors'
    if H3_TURBO_PRESET not in ('', 'v4-6step', 'v4-8step'):
        raise RuntimeError(f'unsupported managed H3 Turbo preset: {H3_TURBO_PRESET!r}')
    _turbo_loras = ([] if not H3_TURBO_PRESET else
                    [f'/models/loras/{H3_TURBO_FILENAME}'])
    for _turbo_path in _turbo_loras:
        if not Path(_turbo_path).is_file():
            raise FileNotFoundError(f'managed H3 Turbo adapter is missing: {_turbo_path}')
    _omni = H3_VARIANT in ('ref2va', 'fused_r1024')
    _arch = 'minimax_h3_ref2va_pruned' if _omni else 'minimax_h3'
    model_def = family_handler.query_model_def(
        _arch, {'architecture': _arch, 'omni_reference': _omni})
    _transformer_variant = 'ref2va' if _omni else 'fl2va'
    transformer = f'/models/transformer/minimax_h3_{_transformer_variant}_pruned_int8_convrot.safetensors'
    text_encoder = '/models/qwen/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
    _mt = 'minimax_h3_ref2va' if _omni else 'minimax_h3'
    model, info = family_handler.load_model(
        [transformer], model_type=_mt, base_model_type=_mt,
        model_def={**model_def, 'omni_reference': _omni}, dtype=torch.bfloat16,
        text_encoder_filename=text_encoder, minimax_h3_text_encoder='nvfp4_awq')
    pipe = info.pop('pipe')
    _h3_transformer = pipe['transformer']
    info['budgets'] = {'transformer': 100, 'text_encoder': 100, '*': 3000}
    profile = offload.profile(
        pipe, profile_no=4, compile=False, quantizeTransformer=False,
        # MMGP expects module names here, not adapter paths. Declaring
        # "transformer" creates the LoRA-aware offload object; the verified path
        # is supplied later to load_loras_into_model.
        loras=['transformer'] if _turbo_loras else [],
        perc_reserved_mem_max=.90, vram_safety_coefficient=.80,
        convertWeightsFloatTo=torch.bfloat16, **info)

    if H3_FUSED_COMBINED:
        # The experimental r1024 merge deliberately retains raw BF16 fc2
        # projections, while the promoted checkpoints use quantized ConvRot fc2
        # forwards that already accept float activations. Install this bridge
        # only after MMGP has wrapped its offload forwards, and fail closed if the
        # checkpoint no longer exposes the exact 52-layer contract we qualified.
        from h3_fused_dtype import install_fused_fc2_dtype_bridge
        _fused_fc2_count = install_fused_fc2_dtype_bridge(_h3_transformer)
        if _fused_fc2_count != 52:
            raise RuntimeError(
                f'fused r1024 expected 52 raw floating fc2 projections; '
                f'wrapped {_fused_fc2_count}')
        print('H3 FUSED R1024 FC2 DTYPE BRIDGE READY layers=52', flush=True)

    if _turbo_loras:
        # Match Maestro v1.8.0's managed H3 path instead of attaching the LoRA
        # generically. validate_loras marks Turbo active; preprocess_loras converts
        # Full/Pruned AdaLN widths; finalize_loras reinstalls native Linear.forward
        # on every adapter-targeted ConvRot layer and fails if that ConvRot-safe
        # route cannot attach.  Keep the adapter unpinned on unified memory.
        model.validate_loras(_turbo_loras)
        _multipliers, _schedule, _errors = parse_loras_multipliers(
            str(H3_TURBO_STRENGTH), len(_turbo_loras), H3_TURBO_STEPS, nb_phases=1)
        if _errors:
            raise RuntimeError(f'invalid managed H3 Turbo multiplier: {_errors}')

        def _preprocess_h3_turbo(state_dict):
            return _h3_transformer.preprocess_loras(_mt, state_dict)

        offload.load_loras_into_model(
            _h3_transformer, _turbo_loras, _multipliers,
            activate_all_loras=True, preprocess_sd=_preprocess_h3_turbo,
            pinnedLora=False, maxReservedLoras=0,
            split_linear_modules_map=getattr(
                _h3_transformer, 'split_linear_modules_map', None))
        _load_errors = getattr(_h3_transformer, '_loras_errors', [])
        if _load_errors:
            raise RuntimeError(f'managed H3 Turbo load failed: {_load_errors}')
        model.finalize_loras()
        print(f'H3 TURBO READY preset={H3_TURBO_PRESET} steps={H3_TURBO_STEPS} '
              'strength=1.0 attention=sdpa cache=off ConvRot-safe=true', flush=True)

    # THE CANVAS RULE IS MAESTRO'S OWN: scale BOTH axes to fit the area cap and
    # round each to a multiple of 32 (packing.py resolve_canvas_size). It never
    # changes the aspect ratio, and its ranker weights aspect error 8x above area
    # error — when the pixels do not fit, the RATIO is what you protect.
    #
    # What used to be here was a rule that does not exist: "keep the 768 short edge
    # and narrow the WIDTH". Nothing in H3, Maestro or MiniMax's docs says it. It
    # made a 16:9 request render 4:3 (1344x768 -> 1024x768), and H3 STRETCHES the
    # start frame onto the canvas (packing.py: `if stretch: image.resize(...)`,
    # and stretch is always True for a single keyframe). Landscape faces came out
    # 0.80x too narrow, portrait faces 1.39x too WIDE. Never reinstate it.
    #
    # 1344x768 (1,032,192 px) is the released canvas but it is not reachable here:
    # measured 2026-08-18, a 1024x768 take held the GPU at 96% with memory AND swap
    # flat and completed 0 of 24 steps in 9 minutes. Compute-bound, not starved —
    # freeing RAM will not unlock it. The working band is 500-750k px, and face
    # detail is bought with FRAMING (FACE_TARGET in app.py), not with pixels.
    H3_MAX_PIXELS = int(os.environ.get('H3_MAX_PIXELS', '737280'))   # 1152x640

    def _r32(n):
        return max(32, int(round(n / 32.0)) * 32)

    def h3_canvas(width: int, height: int):
        """Uniform scale under the area cap, both axes to /32, aspect preserved."""
        width, height = max(32, int(width)), max(32, int(height))
        scale = min(1.0, (H3_MAX_PIXELS / float(width * height)) ** 0.5)
        w, h = _r32(width * scale), _r32(height * scale)
        while w * h > H3_MAX_PIXELS and min(w, h) > 32:      # rounding can re-cross
            scale *= 0.98
            w, h = _r32(width * scale), _r32(height * scale)
        return w, h

    def cover_crop(img, w: int, h: int):
        """Centre cover-crop to EXACTLY the render canvas, so H3's stretch is a no-op.

        H3 always stretches the first keyframe onto the canvas. That is meant to be
        harmless, because the canvas is meant to follow the frame's own aspect. We
        override the canvas, so the frame has to match it instead — otherwise the
        face is distorted before the model or its vision tower ever sees it.
        """
        from PIL import Image as _PIL
        iw, ih = img.size
        if (iw, ih) == (w, h):
            return img
        scale = max(w / float(iw), h / float(ih))
        nw, nh = max(w, int(round(iw * scale))), max(h, int(round(ih * scale)))
        img = img.resize((nw, nh), _PIL.LANCZOS)
        left, top = (nw - w) // 2, (nh - h) // 2
        return img.crop((left, top, left + w, top + h))

    def generate(prompt: str, frames: int, width: int, height: int, seed: int, extra=None):
        extra = dict(extra or {})
        apt = extra.pop('audio_prompt_type', '')
        requested_steps = extra.pop('sampling_steps', None)
        if H3_TURBO_PRESET:
            if requested_steps not in (None, '', 0, H3_TURBO_STEPS):
                raise ValueError(
                    f'{H3_TURBO_PRESET} is a managed {H3_TURBO_STEPS}-step preset; '
                    f'received sampling_steps={requested_steps!r}')
            steps = H3_TURBO_STEPS
        else:
            steps = int(requested_steps or 25)
        w, h = h3_canvas(width, height)
        print(f'H3 CANVAS {width}x{height} -> {w}x{h} '
              f'(aspect {width / float(height):.3f} -> {w / float(h):.3f}, {w * h} px)',
              flush=True)
        # The start frame must arrive AT the render canvas, or H3 stretches it.
        img0 = extra.get('image_start')
        if img0 is not None:
            try:
                before = img0.size
                extra['image_start'] = cover_crop(img0, w, h)
                if before != (w, h):
                    print(f'H3 START FRAME {before[0]}x{before[1]} -> {w}x{h} '
                          f'(cover-cropped, not stretched)', flush=True)
            except Exception as e:
                print(f'H3 start-frame crop failed ({e}) — sending as-is', flush=True)
        return model.generate(
            input_prompt=prompt, **extra,
            frame_num=frames, height=h, width=w, fps=FPS,
            sampling_steps=steps, seed=seed, video_prompt_type='', audio_prompt_type=apt,
            denoising_strength=1.0, masking_strength=1.0)

print(f'ENGINE_READY {ENGINE}', flush=True)
_busy = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print('[http]', fmt % args, flush=True)

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            if ENGINE != 'h3':
                self._send(200, {'ok': True, 'engine': ENGINE, 'loaded': True,
                                 'busy': _busy.locked(), 'pipeline': LTX_PIPELINE,
                                 'transformer': LTX_TRANSFORMER})
            else:
                # H3's two checkpoints do DIFFERENT jobs. Expose the RESIDENT
                # variant so the app can fail closed before feeding reference
                # pictures to fl2va ever again.
                self._send(200, {'ok': True, 'engine': ENGINE, 'loaded': True,
                                 'busy': _busy.locked(), 'variant': H3_VARIANT,
                                 'turbo_preset': H3_TURBO_PRESET or None,
                                 'fused_combined': H3_FUSED_COMBINED,
                                 'attention': 'sdpa', 'cache': 'off'})
        else:
            self._send(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        if self.path != '/generate':
            return self._send(404, {'ok': False, 'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            req = json.loads(self.rfile.read(length))
            prompt = str(req['prompt'])
            request_id = str(req.get('request_id') or '').strip()
            if request_id and (len(request_id) > 128 or
                               not all(c.isalnum() or c in '._-' for c in request_id)):
                raise ValueError('request_id must be 1-128 safe filename characters')
            frames = int(req.get('frames', 121))
            width = int(req.get('width', 1280))
            height = int(req.get('height', 704))
            audio_b64 = req.get('audio_wav_b64') or ''
            image_b64 = req.get('start_image_b64') or ''
            if ENGINE == 'h3' and H3_FUSED_COMBINED and not image_b64:
                raise ValueError(
                    'fused r1024 requires a supplied scene-complete start frame; '
                    'refusing identity-only fallback')
            # optional caller-pinned seed: a storyboard reuses ONE seed for every
            # beat so the look does not wander between clips. Absent -> random.
            req_seed = req.get('seed')
            req_seed = int(req_seed) % 1_000_000_000 if req_seed not in (None, '', 0) else None
            # LTX cross-modal guidance. The pipeline's modality_scale defaults to
            # 1.0, and its guider is a no-op at exactly 1.0 — so audio-visual
            # coherence was switched OFF for every take we have ever rendered.
            # LTX's own guidance: raise it when "the mouth moves but doesn't
            # match the words".
            modality_scale = float(req.get('modality_scale') or 0) or None
            # THE lip-sync lever on the DISTILLED pipeline. ltx2.py: audio_scale
            # becomes AudioConditionByLatent(..., strength) and, above 1.0, also
            # multiplies the audio latent — "amplify the latent signal to boost
            # audio influence". (modality_scale is only read by the reference
            # pipeline; the distilled path we run ignores it entirely — proven by
            # two byte-identical renders at 1.0 and 3.0.)
            audio_scale = float(req.get('audio_scale') or 0) or None
            # H3 recipe controls. The early H3 takes people liked were made with
            # audio_prompt_type='' (H3 speaks the dialogue ITSELF, no conditioning
            # waveform) at 20 steps; everything since has forced 'A' + 25. Let the
            # caller pick so the two can be compared honestly.
            apt_override = req.get('audio_prompt_type')
            # H3 ref2va ACTOR CLONING: a manifest of reference pictures of the
            # people who must appear. Each item {b64, role}. The model tags an
            # untagged prompt itself ("<Subject 1> is the person in <Picture 1>"),
            # so callers may send plain prose.
            references = req.get('references') or []
            # Native Maestro Ref2VA accepts video files in the same manifest.
            # Media Lab passes only basenames already staged in /work/out; never
            # accept arbitrary container paths from an API caller.
            video_references = req.get('video_references') or []
            if len(video_references) > 3:
                raise ValueError('MiniMax H3 accepts at most 3 video references')
            # quality/pipeline knobs, so a take can be tuned without a redeploy:
            # the distilled path runs 8 steps unguided; the REFERENCE pipeline
            # (ti2vid_two_stages_ref) is the one that honours modality_scale/CFG
            steps = int(req.get('sampling_steps') or 0) or None
            guide = float(req.get('guide_scale') or 0) or None
            stg = float(req.get('stg_scale') or 0) or None
            input_video_strength = float(req.get('input_video_strength') or 0) or None
            ref_pipe = bool(req.get('reference_pipeline'))
            input_video_raw = str(req.get('input_video_file') or '')
            input_video_name = Path(str(req.get('input_video_file') or '')).name
            ltx_input_video = None
            retake_strength = float(req.get('retake_strength') or 0.35)
            regenerate_audio = bool(req.get('regenerate_audio', False))
            if input_video_raw:
                if ENGINE != 'ltx25':
                    raise ValueError('input_video_file is available only to LTX 2.5')
                if input_video_name != input_video_raw or not input_video_name.endswith('.mp4'):
                    raise ValueError('input_video_file must be one staged MP4 basename')
                ltx_input_video = LTX_INPUT_DIR / input_video_name
                if not ltx_input_video.is_file() or ltx_input_video.stat().st_size <= 0:
                    raise ValueError('staged LTX input video is missing')
                if not 0.0 < retake_strength <= 1.0:
                    raise ValueError('retake_strength must be in (0, 1]')
        except Exception as exc:
            return self._send(400, {'ok': False, 'error': f'bad request: {exc}'})
        task = ('ref2va' if ENGINE == 'h3' and (references or video_references) else
                'fl2va' if ENGINE == 'h3' and image_b64 else 't2va')
        from media_lab_core.gpu_lease_runtime import authorize_values, open_protocol
        if not authorize_values(open_protocol(), dict(self.headers.items()),
                                engine=LEASE_ENGINE, task=task):
            return self._send(403, {'ok': False, 'error': 'exact GPU lease delegation required'})
        started = time.time()
        with _busy:  # strict single-flight generation
            # A caller can lose its HTTP connection while a multi-hour H3 render
            # continues. Re-check only after taking the generation lock: the first
            # request may have completed while this retry waited. Successful
            # request-ID outputs are atomically published below, so a reconnect
            # returns the existing artifact instead of rendering it again.
            idempotent_out = OUT / f'job-{request_id}.mp4' if request_id else None
            if idempotent_out and idempotent_out.is_file() and idempotent_out.stat().st_size > 0:
                return self._send(200, {'ok': True, 'file': idempotent_out.name,
                                        'seed': req_seed, 'elapsed': 0.0, 'cached': True})
            # Engine outputs are staging duplicates; successful masters are
            # copied into Media Lab's media/. Bound staging and refuse low disk.
            for old in sorted(OUT.glob('*.mp4'), key=lambda p: p.stat().st_mtime,
                              reverse=True)[20:]:
                old.unlink(missing_ok=True)
            if shutil.disk_usage(OUT).free < 10 * 1024 ** 3:
                return self._send(507, {'ok': False,
                                        'error': 'less than 10 GiB free for render staging'})
            tmp_wav = tmp_img = None
            ref_files = []
            try:
                seed = req_seed if req_seed is not None else secrets.randbelow(1_000_000_000)
                # a pinned seed repeats across beats, so the filename cannot rely
                # on it alone for uniqueness
                rid = (f'job-{request_id}' if request_id else
                       f'{int(time.time())}-{seed}-{secrets.token_hex(3)}')
                extra = {}
                is_ref2va = (ENGINE == 'h3' and
                             H3_VARIANT in ('ref2va', 'fused_r1024'))
                if audio_b64:
                    # Audio conditioning has two genuinely different contracts:
                    # LTX / H3 FL2VA consume input_waveform directly. H3 Ref2VA
                    # explicitly disables that source_audio_mode in Maestro, so
                    # the same payload is silently ignored there. Ref2VA receives
                    # this wav later as an audio reference with intent='drive'.
                    tmp_wav = Path(tempfile.mkstemp(suffix='.wav', dir='/tmp')[1])
                    tmp_wav.write_bytes(base64.b64decode(audio_b64))
                    # LTX conditions best on 48 kHz MONO at about -16 LUFS with
                    # peaks near -3 dBFS; a hot or stereo track makes sync slippery
                    norm = tmp_wav.with_name(tmp_wav.stem + '-cond.wav')
                    rc = subprocess.run([str(Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()),
                                         '-nostdin', '-v', 'error', '-y', '-i', str(tmp_wav),
                                         '-af', 'loudnorm=I=-16:TP=-3:LRA=11', '-ac', '1',
                                         '-ar', '48000', '-c:a', 'pcm_s16le', str(norm)],
                                        capture_output=True)
                    if rc.returncode == 0 and norm.exists() and norm.stat().st_size > 1000:
                        tmp_wav.unlink(missing_ok=True)
                        tmp_wav = norm
                    wav, sr = sf.read(str(tmp_wav), dtype='float32', always_2d=True)
                    if is_ref2va:
                        print(f'AUDIO_DRIVE_REFERENCE samples={wav.shape} sr={sr}', flush=True)
                    else:
                        extra = {'input_waveform': wav.T,
                                 'input_waveform_sample_rate': int(sr)}
                    if ENGINE == 'h3' and not is_ref2va:
                        extra['audio_prompt_type'] = ('A' if apt_override is None
                                                      else str(apt_override))
                    elif ENGINE != 'h3':
                        extra['audio_scale'] = audio_scale if audio_scale else 1.0
                        if audio_scale:
                            print(f'AUDIO_SCALE {audio_scale}', flush=True)
                    if not is_ref2va:
                        print(f'AUDIO_CONDITIONING samples={wav.shape} sr={sr}', flush=True)
                if image_b64:
                    # LTX and H3 FL2VA accept a literal image_start. Maestro's H3
                    # Ref2VA omni branch ignores image_start even though the call
                    # succeeds, so retain the decoded file and insert it as the
                    # first composition reference in the manifest below.
                    tmp_img = Path(tempfile.mkstemp(suffix='.png', dir='/tmp')[1])
                    tmp_img.write_bytes(base64.b64decode(image_b64))
                    if ENGINE == 'ltx25':
                        extra['image_start'] = str(tmp_img)
                    elif not is_ref2va:
                        from PIL import Image as _PILImage
                        with _PILImage.open(tmp_img) as im:
                            extra['image_start'] = im.convert('RGB').copy()
                    elif H3_FUSED_COMBINED:
                        # Maestro v1.8.0 enters its omni_reference branch for the
                        # fused Ref2VA architecture and ignores image_start there.
                        # A legal one-frame continuation prefix is the supported
                        # path that becomes the composition keyframe before the
                        # identity/audio references are applied.  Build C,T,H,W
                        # uint8 at the exact H3 canvas so no hidden stretch can
                        # alter camera geometry or faces.
                        from PIL import Image as _PILImage
                        with _PILImage.open(tmp_img) as im:
                            before = im.size
                            w, h = h3_canvas(width, height)
                            frame = cover_crop(im.convert('RGB'), w, h)
                            pixels = np.array(frame, dtype=np.uint8, copy=True)
                        extra['input_video'] = (
                            torch.from_numpy(pixels).permute(2, 0, 1)
                            .unsqueeze(1).contiguous())
                        extra['prefix_frames_count'] = 1
                        print(f'FUSED_COMBINED_BOUNDARY_KEYFRAME {tmp_img} '
                              f'{before[0]}x{before[1]} -> {w}x{h} prefix_frames=1 '
                              '(identity/audio remain Ref2VA references)', flush=True)
                    if is_ref2va and not H3_FUSED_COMBINED:
                        print(f'SOURCE_COMPOSITION_REFERENCE {tmp_img} (h3-ref2va)', flush=True)
                    else:
                        print(f'START_IMAGE {tmp_img} ({ENGINE})', flush=True)
                if (ENGINE == 'h3' and not is_ref2va and apt_override is not None
                        and 'audio_prompt_type' not in extra):
                    extra['audio_prompt_type'] = str(apt_override)
                if ENGINE == 'h3' and steps:
                    extra['sampling_steps'] = steps
                if ENGINE == 'ltx25':
                    if ltx_input_video is not None:
                        # Native LTX retake consumes the whole H3 clip as temporal
                        # visual conditioning.  Keeping regenerate_audio false
                        # preserves the source performance soundtrack exactly.
                        extra['retake_video'] = str(ltx_input_video)
                        extra['retake_start_frame'] = 0
                        extra['retake_end_frame'] = -1
                        extra['retake_strength'] = retake_strength
                        extra['retake_engine'] = 'native'
                        extra['regenerate_audio'] = regenerate_audio
                        decoder = _install_decord_compat()
                        dtype_compat = _install_ltx_retake_dtype_compat(model)
                        print(f'NATIVE_RETAKE {ltx_input_video} strength={retake_strength} '
                              f'regenerate_audio={regenerate_audio} decoder={decoder} '
                              f'dtype_compat={dtype_compat}', flush=True)
                    if modality_scale:
                        extra['modality_scale'] = modality_scale
                    if stg:
                        extra['stg_scale'] = stg
                    if input_video_strength:
                        if not 0.0 < input_video_strength <= 1.0:
                            raise ValueError('input_video_strength must be in (0, 1]')
                        extra['input_video_strength'] = input_video_strength
                    if ref_pipe:
                        extra['reference_pipeline'] = True
                    if steps:
                        extra['sampling_steps'] = steps
                    if guide:
                        extra['guide_scale'] = guide
                    if any((modality_scale, stg, input_video_strength,
                            ref_pipe, steps, guide)):
                        print(f'TUNE modality={modality_scale} stg={stg} ref={ref_pipe} '
                              f'steps={steps} guide={guide} '
                              f'input_video_strength={input_video_strength}', flush=True)
                if is_ref2va and (references or video_references or tmp_img or tmp_wav):
                    manifest = []
                    if tmp_img and not H3_FUSED_COMBINED:
                        manifest.append({
                            'type': 'image', 'path': str(tmp_img),
                            'role': 'approved source scene, wardrobe, framing, and cast layout',
                            'image_intent': 'composition',
                        })
                    for i, ref in enumerate(references, 1):
                        raw = ref.get('b64') or ''
                        if not raw:
                            continue
                        rp = Path(tempfile.mkstemp(suffix=f'-ref{i}.png', dir='/tmp')[1])
                        rp.write_bytes(base64.b64decode(raw))
                        ref_files.append(rp)
                        manifest.append({
                            'type': 'image', 'path': str(rp),
                            'role': ref.get('role') or f'Subject {i}',
                            'image_intent': ref.get('image_intent') or 'identity',
                        })
                    for i, ref in enumerate(video_references, 1):
                        if not isinstance(ref, dict):
                            raise ValueError(f'video reference {i} must be an object')
                        if ref.get('include_audio') is not False:
                            raise ValueError('source-video audio is prohibited for H3 motion references')
                        name = Path(str(ref.get('file') or '')).name
                        vp = OUT / name
                        if (not name or vp.suffix.lower() not in
                                {'.avi', '.m4v', '.mkv', '.mov', '.mp4', '.webm'}
                                or not vp.is_file() or vp.stat().st_size <= 0):
                            raise ValueError(f'video reference {i} is missing or unsupported')
                        manifest.append({
                            'type': 'video', 'path': str(vp),
                            'role': str(ref.get('role') or
                                        'source motion, performance, and camera movement'),
                            'include_audio': False,
                        })
                        print(f'VIDEO_MOTION_REFERENCE {vp} include_audio=False', flush=True)
                    if tmp_wav:
                        manifest.append({
                            'type': 'audio', 'path': str(tmp_wav),
                            'role': 'the supplied performance soundtrack',
                            'audio_intent': 'drive',
                        })
                    if manifest:
                        extra['minimax_h3_references'] = manifest
                        extra['minimax_h3_reference_detail'] = req.get('reference_detail') or 'match'
                        print(f'REF2VA {len(manifest)} composition/identity/video/audio reference(s): '
                              f'{[(m["type"], m["role"]) for m in manifest]}', flush=True)
                result = generate(prompt, frames, width, height, seed, extra)
                if result is None:
                    raise RuntimeError('model returned no result')
                out = OUT / f'{rid}.mp4'
                staged_out = out.with_name(out.stem + '.partial.mp4')
                staged_out.unlink(missing_ok=True)
                encode_joint_av(result['x'], result['audio'],
                                int(result['audio_sampling_rate']), staged_out)
                if not staged_out.is_file() or staged_out.stat().st_size <= 0:
                    raise RuntimeError('encoder produced no artifact')
                os.replace(staged_out, out)
                del result
                torch.cuda.empty_cache()
                self._send(200, {'ok': True, 'file': out.name, 'seed': seed,
                                 'elapsed': round(time.time() - started, 1), 'cached': False})
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f'ENGINE_GENERATE_ERROR {type(exc).__name__}: {exc}', flush=True)
                torch.cuda.empty_cache()
                self._send(500, {'ok': False, 'error': str(exc)[:500]})
            finally:
                temps = ref_files + ([tmp_wav] if tmp_wav else []) + ([tmp_img] if tmp_img else [])
                for p in temps:
                    p.unlink(missing_ok=True)


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
