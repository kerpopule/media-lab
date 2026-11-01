"""H3 Singularity dual-sampling: the "Real / Long" H3 engine.

A load-on-demand alternative to the always-warm Sol-H3 pipeline. It runs inside
the same dormant H3 unit (media-lab-sol-h3.service), under the same exact GPU
lease and the same control-plane guard, when the unit is started with
``H3_VARIANT=singularity``: runner/sol_engine_server.py then drives this module
instead of Sol's Pipeline.

What it is: the community "MiniMax H3 Singularity" ref2va checkpoint rendered
with the dual-sampling recipe measured on the studio host (2026-09-25 eval):

  pass 1   2 euler steps at 0.5 MP with the Turbo LoRA
  upscale  MiniMax H3 latent upscaler (x1.25 by default)
  re-noise zero-step re-noise of the upscaled video latent
  pass 2   the remaining 10 steps with Turbo + LMS 0.5 + realism 1.0 LoRAs,
           video and audio denoised together

Why it exists: Sol-H3 is fast (~70 s a clip) but frozen at 5.04 s, 1344x768,
and its reference mode cannot fit on a 128 GB box. Singularity takes text,
up to 9 reference pictures, 3 reference videos and 3 reference audio clips,
renders portrait, landscape or square, 5 to 15 s, and holds faces. It is about
4x slower (~285 s per warm 5 s clip) and needs ~75 GiB resident, so it never
sits beside Sol: loading it evicts Sol, and the studio restores warm Sol after.

The renderer is an isolated ComfyUI (its own venv, loopback only) that this
module starts as a child process, so the unit's cgroup - and the guard's exact
cgroup termination - covers it. Stdlib only.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

VARIANT = "singularity"
TASK = "singularity"
LABEL = "Real / Long"

FPS = 24
MIN_FRAMES = 124            # 5.17 s, the shortest take on the 17k+5 grid
TRAINED_MAX_FRAMES = 362    # 15.08 s, the longest take the checkpoint was trained on
HARD_MAX_FRAMES = 481       # 20.0 s: beyond this the box has no measured headroom
IMAGE_REF_MAX = 9
VIDEO_REF_MAX = 3
AUDIO_REF_MAX = 3
PASS1_MEGAPIXELS = 0.5
CANVAS_MULTIPLE = 32
DEFAULT_UPSCALE = 1.25
TRIGGER = "r34l1sm"         # the realism LoRA's trigger word
ASPECTS = {"landscape": (16, 9), "portrait": (9, 16), "square": (1, 1)}

# The lean weight set measured on the studio host: the pruned int8 checkpoint
# plus the NVFP4 text encoder (the LMS LoRA was trained against the pruned
# layout; on the full checkpoint 50 of its keys do not fit).
DEFAULT_MODELS = {
    "dit": "Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors",
    "text_encoder": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "turbo_lora": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
    "lms_lora": "minimax_h3_lms_v1.0_r64.safetensors",
    "realism_lora": "h3-realism-people-t2v-i2v-r2v.safetensors",
    "upscaler": "minimax_h3_latent_upscaler_3d_bf16.safetensors",
}
MODEL_ENV = {
    "dit": "H3_SINGULARITY_DIT",
    "text_encoder": "H3_SINGULARITY_TEXT_ENCODER",
    "turbo_lora": "H3_SINGULARITY_TURBO_LORA",
    "lms_lora": "H3_SINGULARITY_LMS_LORA",
    "realism_lora": "H3_SINGULARITY_REALISM_LORA",
    "upscaler": "H3_SINGULARITY_UPSCALER",
}


class SingularityError(RuntimeError):
    """A render failed on the renderer (the engine latches its safety stop)."""


class SingularityConfigError(SingularityError):
    """The runtime is missing or misconfigured; raised before any GPU work."""


class SingularityRequestError(SingularityError):
    """The request itself is invalid (too many references, missing file...)."""


# ---------------------------------------------------------------- geometry
def frames_for_seconds(seconds, max_frames: int = TRAINED_MAX_FRAMES) -> int:
    """Seconds -> frame count on H3's 17k+5 grid at 24 fps, rounded UP so a
    spoken line is never clipped, clamped to [124, max_frames]."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 5.0
    if not math.isfinite(seconds):
        seconds = 5.0
    cap = max(MIN_FRAMES, min(int(max_frames), HARD_MAX_FRAMES))
    cap -= (cap - 5) % 17
    n = max(5, int(math.ceil(seconds * FPS - 1e-9)))
    n = ((n - 5 + 16) // 17) * 17 + 5
    return max(MIN_FRAMES, min(cap, n))


def aligned_frames(frames, max_frames: int = TRAINED_MAX_FRAMES) -> int:
    """Snap an explicit frame count onto the grid (up), clamped like seconds."""
    try:
        frames = int(frames)
    except (TypeError, ValueError):
        frames = MIN_FRAMES
    return frames_for_seconds(frames / FPS, max_frames)


def seconds_of(frames: int) -> float:
    return round(int(frames) / FPS, 3)


def orientation_of(width, height) -> str:
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return "landscape"
    if w <= 0 or h <= 0:
        return "landscape"
    if abs(w - h) <= max(w, h) * 0.05:
        return "square"
    return "landscape" if w > h else "portrait"


def pass1_size(orientation: str, megapixels: float = PASS1_MEGAPIXELS) -> tuple[int, int]:
    """First-pass canvas: ComfyUI's ResolutionSelector formula (megapixels are
    MiB of pixels, each side rounded to the nearest multiple of 32), which is
    what the measured eval used: 960x544 landscape, 544x960 portrait."""
    wr, hr = ASPECTS.get(orientation, ASPECTS["landscape"])
    scale = math.sqrt(megapixels * 1024 * 1024 / (wr * hr))
    width = round(wr * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE
    height = round(hr * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE
    return int(width), int(height)


def output_size(orientation: str, upscale: float = DEFAULT_UPSCALE) -> tuple[int, int]:
    """The delivered size after the latent upscaler (aligned to 32 per side).
    1.25x: 1216x672 landscape / 672x1216 portrait (both measured)."""
    w, h = pass1_size(orientation)
    return (int(round(w * upscale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE),
            int(round(h * upscale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE))


# ------------------------------------------------------------------ prompts
def compose_prompt(prompt: str, image_roles=(), video_roles=(), audio_roles=(),
                   start_frame_picture: int | None = None) -> str:
    """The Singularity prompt: trigger word, then subject definitions for each
    reference unless the caller already wrote a <Picture>/<Video>/<Audio> map
    (a director's relationship map must never be overwritten)."""
    body = str(prompt or "").strip()
    tagged = any(tag in body for tag in ("<Picture", "<Video", "<Audio"))
    lines = []
    if not tagged:
        subjects = []
        for i, role in enumerate(image_roles, 1):
            if start_frame_picture == i:
                continue
            what = str(role or "").strip() or "person"
            subjects.append(f"<Subject {len(subjects) + 1}> is the {what} in <Picture {i}>.")
        if subjects:
            lines.append("subject_definitions: " + " ".join(subjects))
        extra = []
        if start_frame_picture:
            extra.append(f"<Picture {start_frame_picture}> is the opening frame: match its "
                         "composition, setting and lighting.")
        for k, role in enumerate(video_roles, 1):
            what = str(role or "").strip() or "motion, performance and camera movement"
            extra.append(f"<Video {k}> provides the {what}.")
        for k, role in enumerate(audio_roles, 1):
            what = str(role or "").strip() or "voice and sound"
            extra.append(f"<Audio {k}> provides the {what}.")
        if extra:
            lines.append("reference_notes: " + " ".join(extra))
        if lines:
            # the studio hands over H3's three-field form; its first field IS
            # the description, so it goes in under the Ref2VA name, not nested
            body = re.sub(r"^integrated_multimodal_description:\s*", "", body, flags=re.I)
            body = "\n".join(lines) + "\n\ndetailed_description: " + body
    if not body.lower().startswith(TRIGGER):
        body = f"{TRIGGER}\n\n{body}"
    return body


# -------------------------------------------------------------------- graph
def models_from_env(env=None) -> dict:
    env = os.environ if env is None else env
    models = dict(DEFAULT_MODELS)
    for key, var in MODEL_ENV.items():
        value = str(env.get(var) or "").strip()
        if value:
            models[key] = Path(value).name
    return models


def build_graph(*, prompt: str, frames: int, orientation: str, seed: int, prefix: str,
                image_refs=(), video_refs=(), audio_refs=(), ref_image_size: str = "max",
                upscale: float = DEFAULT_UPSCALE, models: dict | None = None,
                width: int | None = None, height: int | None = None,
                sampling_steps: int = 6) -> dict:
    """ComfyUI API graph for one dual-sampling render.

    image_refs: input-directory filenames (<= 9). video_refs: dicts with an
    absolute ``path`` plus ``skip`` and ``cap`` frame counts (<= 3; always
    without their own soundtrack). audio_refs: dicts with an absolute ``path``
    (<= 3). ``width``/``height`` override the 0.5 MP first-pass canvas (the
    warm-up uses a tiny one)."""
    m = {**DEFAULT_MODELS, **(models or {})}
    if len(image_refs) > IMAGE_REF_MAX:
        raise SingularityRequestError(f"Real / Long takes at most {IMAGE_REF_MAX} reference pictures")
    if len(video_refs) > VIDEO_REF_MAX:
        raise SingularityRequestError(f"Real / Long takes at most {VIDEO_REF_MAX} reference videos")
    if len(audio_refs) > AUDIO_REF_MAX:
        raise SingularityRequestError(f"Real / Long takes at most {AUDIO_REF_MAX} reference audio clips")
    if ref_image_size not in ("match", "max"):
        raise SingularityRequestError("reference detail must be match or max")
    if (int(frames) - 5) % 17 or int(frames) < 5:
        raise SingularityRequestError(f"{frames} frames is not on H3's 17k+5 grid")
    if width is None or height is None:
        width, height = pass1_size(orientation)
    g = {
        "69": {"class_type": "UNETLoader", "inputs": {"unet_name": m["dit"], "weight_dtype": "default"}},
        "64": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["text_encoder"], "type": "minimax", "device": "default"}},
        "65": {"class_type": "VAELoader", "inputs": {"vae_name": m["video_vae"]}},
        "66": {"class_type": "VAELoader", "inputs": {"vae_name": m["audio_vae"]}},
        "119": {"class_type": "ModelAttentionBackend", "inputs": {"model": ["69", 0], "attention": "comfy kitchen attention"}},
        "121": {"class_type": "BlockSparseAttention", "inputs": {
            "model": ["119", 0], "selection": "sol-attn", "selection.tau": 1.3,
            "start_percent": 0.2, "end_percent": 0.9, "dense_blocks": "", "min_tokens": 12288,
            "extra_tokens": 256, "sink_conditioning": "exact_kv_and_rows", "verbose": False}},
        "143": {"class_type": "MiniMaxChunkFeedForward", "inputs": {"model": ["121", 0], "chunks": 2, "seq_threshold": 4096}},
        "118": {"class_type": "Lora Loader Stack (rgthree)", "inputs": {
            "model": ["143", 0], "clip": ["64", 0],
            "lora_01": "None", "strength_01": 1.0, "lora_02": m["turbo_lora"], "strength_02": 1.0,
            "lora_03": "None", "strength_03": 1.0, "lora_04": "None", "strength_04": 1.0}},
        "56": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["118", 1], "vae": ["65", 0], "audio_vae": ["66", 0], "prompt": prompt,
            "width": int(width), "height": int(height), "length": int(frames),
            "ref_image_size": ref_image_size}},
        "71": {"class_type": "BasicScheduler", "inputs": {"model": ["118", 0], "scheduler": "simple", "steps": int(sampling_steps), "denoise": 1.0}},
        "94": {"class_type": "ExtendIntermediateSigmas", "inputs": {"sigmas": ["71", 0], "steps": 2, "start_at_sigma": 1.0, "end_at_sigma": 0.0, "spacing": "linear"}},
        "99": {"class_type": "SplitSigmas", "inputs": {"sigmas": ["94", 0], "step": 2}},
        "100": {"class_type": "SplitSigmas", "inputs": {"sigmas": ["99", 1], "step": 0}},
        "63": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "58": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "57": {"class_type": "BasicGuider", "inputs": {"model": ["118", 0], "conditioning": ["56", 0]}},
        # pass 1 (small canvas)
        "108": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["63", 0], "guider": ["57", 0], "sampler": ["58", 0], "sigmas": ["99", 0], "latent_image": ["56", 1]}},
        # video from the denoised prediction, audio from the noisy state at the SAME sigma
        "102": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["108", 0]}},
        "104": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["108", 1]}},
        "124": {"class_type": "MinimaxH3LatentUpscaler3D", "inputs": {
            "latent": ["104", 0], "model_name": m["upscaler"], "mode": "scale by multiplier",
            "mode.scale": float(upscale), "align": 32, "enable_temporal_chunking": True,
            "force_unload": True, "device": "cuda", "precision": "bf16"}},
        # zero-step re-noise of the upscaled video to the split sigma
        "128": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["63", 0], "guider": ["57", 0], "sampler": ["58", 0], "sigmas": ["100", 0], "latent_image": ["124", 0]}},
        "105": {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": ["128", 0], "audio_latent": ["102", 1]}},
        "114": {"class_type": "DisableNoise", "inputs": {}},
        "106": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["114", 0], "guider": ["126", 0], "sampler": ["58", 0], "sigmas": ["99", 1], "latent_image": ["105", 0]}},
        "109": {"class_type": "VAEDecode", "inputs": {"samples": ["106", 1], "vae": ["65", 0]}},
        "110": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["106", 1], "vae": ["66", 0]}},
        "141": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["109", 0], "audio": ["110", 0], "frame_rate": FPS, "loop_count": 0,
            "filename_prefix": prefix, "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 19,
            "save_metadata": False, "trim_to_audio": False, "pingpong": False, "save_output": True}},
    }
    last = "118"
    g["127"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": [last, 0], "lora_name": m["lms_lora"], "strength_model": 0.5}}
    g["199"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["127", 0], "lora_name": m["realism_lora"], "strength_model": 1.0}}
    g["126"] = {"class_type": "BasicGuider", "inputs": {"model": ["199", 0], "conditioning": ["56", 0]}}
    for i, name in enumerate(image_refs):
        nid = str(170 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": str(name)}}
        g["56"]["inputs"][f"ref_images.ref_image_{i}"] = [nid, 0]
    for i, ref in enumerate(video_refs):
        nid = str(180 + i)
        g[nid] = {"class_type": "VHS_LoadVideoPath", "inputs": {
            "video": str(ref["path"]), "force_rate": FPS, "custom_width": 0, "custom_height": 0,
            "frame_load_cap": int(ref.get("cap") or 0), "skip_first_frames": int(ref.get("skip") or 0),
            "select_every_nth": 1, "format": "None"}}
        g["56"]["inputs"][f"ref_videos.ref_video_{i}"] = [nid, 0]
    for i, ref in enumerate(audio_refs):
        nid = str(190 + i)
        g[nid] = {"class_type": "VHS_LoadAudio", "inputs": {
            "audio_file": str(ref["path"]), "seek_seconds": float(ref.get("start_sec") or 0.0),
            "duration": float(ref.get("duration") or 0.0)}}
        g["56"]["inputs"][f"ref_audios.ref_audio_{i}"] = [nid, 0]
    return g


def warm_graph(prefix: str, models: dict | None = None) -> dict:
    """A tiny render that makes ComfyUI load every model the real graph uses
    (text encoder, DiT with its attention patches and LoRAs, both VAEs, the
    latent upscaler), so the first real take starts warm."""
    return build_graph(prompt=f"{TRIGGER}\n\nA still grey wall. Silence.", frames=5,
                       orientation="square", seed=1, prefix=prefix, models=models,
                       width=256, height=256, ref_image_size="match", sampling_steps=2)


# ------------------------------------------------------------------ comfy io
def _http(url: str, payload=None, timeout: float = 30.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {},
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def extra_model_paths_yaml(env) -> str:
    """ComfyUI extra_model_paths.yaml for the isolated runtime (from local.env)."""
    root = str(env.get("H3_SINGULARITY_MODELS_ROOT") or "").strip()
    if not root:
        raise SingularityConfigError("H3_SINGULARITY_MODELS_ROOT is not configured")
    loras = [d for d in (f"{root}/loras", str(env.get("H3_SINGULARITY_EXTRA_LORAS") or "").strip()) if d]
    lines = ["h3singularity:", f"  base_path: {root}", "  diffusion_models: diffusion_models",
             "  text_encoders: text_encoders", "  vae: vae", "  loras: |"]
    lines += [f"    {d}" for d in loras]
    upscalers = str(env.get("H3_SINGULARITY_UPSCALERS") or "").strip()
    if upscalers:
        lines.append(f"  latent_upscale_models: {upscalers}")
    return "\n".join(lines) + "\n"


class ComfyProcess:
    """The isolated ComfyUI child. Loopback only; killed with its process group."""

    def __init__(self, env, runtime_root: str | os.PathLike):
        self.env = dict(env)
        self.comfy_dir = Path(str(env.get("H3_SINGULARITY_COMFY_DIR") or "")).expanduser()
        self.port = int(env.get("H3_SINGULARITY_PORT") or 18188)
        self.root = Path(runtime_root) / "singularity"
        self.dirs = {k: self.root / k for k in ("in", "out", "temp", "logs")}
        self.proc: subprocess.Popen | None = None
        self.url = f"http://127.0.0.1:{self.port}"

    def python(self) -> Path:
        explicit = str(self.env.get("H3_SINGULARITY_PYTHON") or "").strip()
        return Path(explicit).expanduser() if explicit else self.comfy_dir / ".venv" / "bin" / "python"

    def start(self, ready_timeout_s: float = 300.0):
        if not (self.comfy_dir / "main.py").is_file():
            raise SingularityConfigError(f"Real / Long runtime is missing ({self.comfy_dir}/main.py)")
        if not self.python().exists():
            raise SingularityConfigError("Real / Long runtime has no Python environment")
        for d in self.dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        yaml = self.root / "extra_model_paths.yaml"
        yaml.write_text(extra_model_paths_yaml(self.env))
        if self.alive_http():
            raise SingularityConfigError(f"port {self.port} already answers; refusing to share a renderer")
        log = (self.dirs["logs"] / f"comfy-{int(time.time())}.log").open("ab")
        cmd = [str(self.python()), "main.py", "--listen", "127.0.0.1", "--port", str(self.port),
               "--highvram", "--disable-auto-launch",
               "--output-directory", str(self.dirs["out"]), "--input-directory", str(self.dirs["in"]),
               "--temp-directory", str(self.dirs["temp"]), "--extra-model-paths-config", str(yaml)]
        child_env = {k: v for k, v in os.environ.items() if not k.startswith("MEDIA_LAB_GPU_")}
        child_env["PYTORCH_CUDA_ALLOC_CONF"] = self.env.get("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
        self.proc = subprocess.Popen(cmd, cwd=str(self.comfy_dir), stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, env=child_env, start_new_session=True)
        log.close()
        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SingularityConfigError(f"Real / Long renderer exited during start (code {self.proc.returncode})")
            if self.alive_http():
                return self
            time.sleep(1.0)
        self.stop()
        raise SingularityConfigError("Real / Long renderer did not answer within its start window")

    def alive_http(self) -> bool:
        try:
            _http(f"{self.url}/system_stats", timeout=3)
            return True
        except Exception:
            return False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout_s: float = 30.0):
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=10)
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise SingularityError("Real / Long renderer process group survived stop")
        self.proc = None

    def interrupt(self):
        try:
            _http(f"{self.url}/interrupt", {}, timeout=5)
        except Exception:
            pass

    def run(self, graph: dict, timeout_s: float, poll_s: float = 2.0, progress=None) -> dict:
        """Queue one graph and wait for its history entry; returns the outputs."""
        client = uuid.uuid4().hex
        try:
            resp = _http(f"{self.url}/prompt", {"prompt": graph, "client_id": client}, timeout=60)
        except urllib.error.HTTPError as exc:
            # graph validation (a missing node or model file) happens before any GPU work
            body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else str(exc)
            raise SingularityConfigError(f"renderer refused the graph: {body[:400]}") from None
        pid = resp.get("prompt_id")
        if not pid:
            raise SingularityConfigError(f"renderer refused the graph: {json.dumps(resp)[:400]}")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.running():
                raise SingularityError("Real / Long renderer exited mid-render")
            try:
                hist = _http(f"{self.url}/history/{pid}", timeout=15).get(pid)
            except Exception:
                hist = None
            if hist:
                status = hist.get("status") or {}
                messages = status.get("messages") or []
                failure = next((m[1] for m in messages
                                if isinstance(m, (list, tuple)) and len(m) > 1
                                and m[0] in ("execution_error", "execution_interrupted")), None)
                if status.get("status_str") == "error" or failure is not None:
                    detail = failure if isinstance(failure, dict) else {}
                    raise SingularityError(f"render failed in {detail.get('node_type', 'a node')}: "
                                           f"{str(detail.get('exception_message', ''))[:300]}")
                if status.get("completed") or status.get("status_str") == "success":
                    return hist.get("outputs") or {}
            if progress is not None:
                progress()
            time.sleep(poll_s)
        self.interrupt()
        raise SingularityError("Real / Long render timed out")


def _output_video(outputs: dict, out_dir: Path) -> Path:
    for node in ("141",):
        for item in (outputs.get(node) or {}).get("gifs") or []:
            path = Path(str(item.get("fullpath") or ""))
            if not path.is_file():
                path = out_dir / str(item.get("subfolder") or "") / str(item.get("filename") or "")
            if path.is_file() and path.suffix == ".mp4":
                return path
    raise SingularityError("the render finished without a video file")


# ---------------------------------------------------------------- pipeline
class SingularityPipeline:
    """What sol_engine_server holds as its resident pipeline when
    H3_VARIANT=singularity: start() loads, generate() renders, close() unloads."""

    def __init__(self, env, runtime_root, log=print, media_dir=None):
        self.env = dict(env)
        # where the studio stages motion-reference videos (the engine's out dir)
        self.media_dir = Path(media_dir) if media_dir else None
        self.comfy = ComfyProcess(self.env, runtime_root)
        self.models = models_from_env(self.env)
        self.max_frames = int(self.env.get("H3_SINGULARITY_MAX_FRAMES") or TRAINED_MAX_FRAMES)
        self.log = log
        self.warm_s = None

    @property
    def process(self):  # close_pipeline() inspects .process like Sol's workers
        return self.comfy.proc

    def start(self, warm_timeout_s: float = 1800.0):
        t0 = time.monotonic()
        self.comfy.start()
        prefix = f"warm-{uuid.uuid4().hex[:8]}"
        try:
            self.comfy.run(warm_graph(prefix, self.models), timeout_s=warm_timeout_s)
        except Exception:
            self.comfy.stop()
            raise
        for path in self.comfy.dirs["out"].glob(f"{prefix}_*"):
            try:
                path.unlink()
            except OSError:
                pass
        self.warm_s = round(time.monotonic() - t0, 1)
        self.log(f"singularity warm in {self.warm_s}s")
        return self

    def close(self):
        self.comfy.stop()

    def finish(self):  # Sol's Pipeline API; nothing to report here
        return None

    def interrupt(self):
        self.comfy.interrupt()

    def _stage_image(self, b64: str, name: str) -> str:
        data = base64.b64decode(b64, validate=True)
        (self.comfy.dirs["in"] / name).write_bytes(data)
        return name

    def _staged_path(self, ref: dict, kind: str = "video") -> str:
        """A staged reference: a bare file name inside the studio's staging dir
        (what the app sends), never a path the request could point elsewhere."""
        name = Path(str(ref.get("file") or "")).name
        if not name or self.media_dir is None:
            raise SingularityRequestError(f"a reference {kind} was not staged by the studio")
        path = self.media_dir / name
        if not path.is_file():
            raise SingularityRequestError(f"a reference {kind} is missing on the studio host")
        return str(path)

    def request_spec(self, req: dict, rid: str) -> dict:
        """Validate a /generate body and turn it into build_graph() arguments."""
        refs = [r for r in (req.get("references") or []) if isinstance(r, dict) and r.get("b64")]
        vrefs = [v for v in (req.get("video_references") or []) if isinstance(v, dict)]
        arefs = [a for a in (req.get("audio_references") or []) if isinstance(a, dict)]
        if req.get("audio_wav_b64"):
            arefs.append({"b64": req["audio_wav_b64"], "role": "voice and delivery of the line"})
        start_b64 = str(req.get("start_image_b64") or "").strip()
        if len(refs) + (1 if start_b64 else 0) > IMAGE_REF_MAX:
            raise SingularityRequestError(f"Real / Long takes at most {IMAGE_REF_MAX} reference pictures")
        if len(vrefs) > VIDEO_REF_MAX:
            raise SingularityRequestError(f"Real / Long takes at most {VIDEO_REF_MAX} reference videos")
        if len(arefs) > AUDIO_REF_MAX:
            raise SingularityRequestError(f"Real / Long takes at most {AUDIO_REF_MAX} reference audio clips")
        frames = aligned_frames(req.get("frames") or MIN_FRAMES, self.max_frames)
        orientation = str(req.get("orientation") or "") or orientation_of(req.get("width"), req.get("height"))
        if orientation not in ASPECTS:
            orientation = "landscape"
        image_names, image_roles = [], []
        for i, ref in enumerate(refs):
            image_names.append(self._stage_image(ref["b64"], f"{rid}-ref{i}.png"))
            image_roles.append(ref.get("role") or "")
        start_pic = None
        if start_b64:
            image_names.append(self._stage_image(start_b64, f"{rid}-start.png"))
            image_roles.append("opening frame")
            start_pic = len(image_names)
        video_specs, video_roles = [], []
        for v in vrefs:
            path = self._staged_path(v)
            if v.get("include_audio"):
                raise SingularityRequestError("reference-video soundtracks are never conditioning here")
            # the studio stages each motion reference already trimmed to its start
            skip = 0 if v.get("file") else max(0, int(round(float(v.get("start_sec") or 0.0) * FPS)))
            video_specs.append({"path": path, "skip": skip, "cap": frames})
            video_roles.append(v.get("role") or "")
        audio_specs, audio_roles = [], []
        for k, a in enumerate(arefs):
            if a.get("b64"):
                path = str(self.comfy.dirs["in"] / f"{rid}-audio{k}.wav")
                Path(path).write_bytes(base64.b64decode(a["b64"], validate=True))
            else:
                path = self._staged_path(a, kind="audio clip")
            audio_specs.append({"path": path, "start_sec": a.get("start_sec") or 0.0})
            audio_roles.append(a.get("role") or "")
        detail = str(req.get("reference_detail") or "max")
        prompt = compose_prompt(req.get("prompt") or "", image_roles, video_roles, audio_roles,
                                start_frame_picture=start_pic)
        try:
            upscale = float(req.get("upscale") or DEFAULT_UPSCALE)
        except (TypeError, ValueError):
            upscale = DEFAULT_UPSCALE
        upscale = min(1.5, max(1.0, upscale))
        return {"prompt": prompt, "frames": frames, "orientation": orientation,
                "seed": int(req.get("seed") or 0), "prefix": rid, "image_refs": image_names,
                "video_refs": video_specs, "audio_refs": audio_specs,
                "ref_image_size": detail if detail in ("match", "max") else "max",
                "upscale": upscale, "models": self.models}

    def generate(self, req: dict, rid: str, timeout_s: float = 4 * 3600.0) -> dict:
        spec = self.request_spec(req, rid)
        t0 = time.monotonic()
        outputs = self.comfy.run(build_graph(**spec), timeout_s=timeout_s)
        video = _output_video(outputs, self.comfy.dirs["out"])
        return {"output": str(video), "frames": spec["frames"], "orientation": spec["orientation"],
                "render_s": round(time.monotonic() - t0, 1)}


def sweep_inputs(runtime_root, rid: str):
    """Remove one request's staged reference files and the renderer's own copies
    of its output (the engine keeps the finished take in its out dir), so the
    runtime dir does not grow with every take. Best effort."""
    root = Path(runtime_root) / "singularity"
    for base, pattern in ((root / "in", f"{rid}-*"), (root / "out", f"{rid}_*")):
        for path in base.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


def copy_output(src: str, dst: str):
    tmp = f"{dst}.part"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
