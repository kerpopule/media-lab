"""H3 Ref2VA fail-closed reference contract shared by the app, the engine shim
(runner/engine_server.py) and the tests.

MiniMax H3 ships two checkpoints that do DIFFERENT jobs:
  fl2va  — first/last-frame to video+audio.  Conditions on a START FRAME.
  ref2va — reference-to-video+audio ("actor cloning"). Takes reference PICTURES
           of a person and preserves their identity through the shot. This is
           the only one that holds a likeness.

This one module is where the app, the engine shim and the tests all agree on the
rules, so a request can never silently degrade to a likeness-less path. The three
silent degradations this forbids:
  * references requested on a non-H3 engine (e.g. LTX),
  * references requested while the resident H3 checkpoint is fl2va,
  * a 'references' list that arrives empty or all-undecodable,
  * the actor-cloning checkpoint is resident (ref2va) but no usable reference
    was supplied — booting the actor-cloning model with no actors is strictly
    worse than a clean refusal.

Qwen-Image-Edit-2511 full-quality recipe (vs the Lightning preview below) is
also defined here so the app graph and its tests share the same numbers.
"""
import base64
import math

H3_REF2VA_VARIANT = "ref2va"
H3_FL2VA_VARIANT = "fl2va"
H3_FUSED_R1024_VARIANT = "fused_r1024"
# "Real / Long": the H3 Singularity dual-sampling engine (runner/h3_singularity.py),
# a load-on-demand variant of the same H3 unit with its own single task family.
H3_SINGULARITY_VARIANT = "singularity"
H3_SINGULARITY_TASK = "singularity"
H3_VARIANTS = (H3_FL2VA_VARIANT, H3_REF2VA_VARIANT, H3_FUSED_R1024_VARIANT,
               H3_SINGULARITY_VARIANT)
H3_TASKS = ("t2va", "fl2va", "ref2va", H3_SINGULARITY_TASK)
H3_SINGULARITY_REF_MAX = 9
H3_SINGULARITY_AUDIO_REF_MAX = 3
# Set once by the app from local.env: True when this host has the Real / Long
# engine enabled and installed. H3 reference jobs then route to it, because
# Sol's Ref2VA needs more memory than a 128 GB box has (120 GiB row vs ~117).
ROUTE_REFERENCES_TO_SINGULARITY = False
H3_VIDEO_REF_MAX = 3
H3_VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}

# The only H3 Turbo routes Media Lab may request. Keep these managed rather than
# accepting arbitrary LoRA paths/strengths from an API caller: Maestro v1.8.0's
# H3 loader validates the adapter, converts the Full/Pruned AdaLN layout when
# required, and reinstalls native Linear.forward on ConvRot-targeted layers.
# Sol attention and First Block Cache remain separate A/B variables; the first
# Turbo canary therefore stays on SDPA with cache disabled.
H3_TURBO_PRESET = "v4-6step"  # bool True remains shorthand for the promoted canary
H3_TURBO_PRESETS = {"v4-6step": 6, "v4-8step": 8}
H3_TURBO_FILENAME = "minimax_h3_turbo_v4_step600_ema.safetensors"
H3_TURBO_SHA256 = "5f3a626cd72c93a8b9318d6760c510bc5092d2ab13aaba1f932c5bab07a416d3"
H3_TURBO_SIZE_BYTES = 779849816
H3_TURBO_STEPS = H3_TURBO_PRESETS[H3_TURBO_PRESET]
H3_TURBO_STRENGTH = 1.0

# H3 Ref2VA's documented reference picture cap. Beyond this we refuse rather
# than ever concatenate people into a contact sheet (never a video input).
H3_REF_MAX = 4

ALLOWED_REFERENCE_DETAIL = ("match", "max")

QWEN_QUALITY_STEPS = 40
QWEN_QUALITY_CFG = 4.0
QWEN_QUALITY_GUIDE = 1.0

# Lightning 4-step preview recipe — explicitly the fast preview, never an
# approval path. This is the sole graph node that loads the Lightning LoRA.
QWEN_PREVIEW_STEPS = 4
QWEN_PREVIEW_CFG = 1.0
QWEN_PREVIEW_LORA = "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"

# PNG / JPEG / WebP magic prefixes used to confirm a reference is real image
# bytes (base64 will happily decode any text; it must also LOOK like a picture).
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")


def _decodable_image_bytes(ref):
    """Return (bytes, True) when ref carries base64 that also decodes to image
    bytes; return (None, False) otherwise. Used so garbage b64 can never reach
    a silent route — a reference is only "usable" if it is real picture bytes.

    Strict base64 (validate=True) so non-canonical garbage is rejected, and a
    WebP is only accepted when its RIFF container really names a WEBP chunk
    (data[8:12] == b'WEBP') so a base64 WAV/AVI RIFF payload can never pass as
    an image."""
    raw = (ref.get("b64") or "").strip()
    if not raw:
        return None, False
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        return None, False
    if not data:
        return None, False
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
        return data, True
    # A real WebP is RIFF .... WEBP, not any RIFF (that is how a base64 WAV or
    # AVI would sneak in). Check the fourcc, not just the RIFF container.
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return data, True
    return None, False


def normalize_references(references):
    """Return only reference items that actually carry a usable base64 image
    (decodable AND matching a png/jpeg/webp magic prefix). Role fields are
    preserved verbatim so the app can pass separate person/product/style
    tagged subjects through to the engine manifest."""
    out = []
    for ref in (references or []):
        if not isinstance(ref, dict):
            continue
        if not _decodable_image_bytes(ref)[1]:
            continue
        out.append(ref)
    return out


def has_usable_reference(references):
    """True iff normalize_references yields at least one usable image."""
    return bool(normalize_references(references))


def normalize_video_references(references):
    """Validate lightweight Media Lab video-reference descriptors.

    Video pixels remain on disk; jobs store only a bounded /media URL, role,
    timeline offset, and a pinned no-audio contract. Source-video audio is never
    conditioning in this path: instruments/percussion made mouths move in the
    likeness qualification takes. The worker performs a real decode before the
    GPU is touched.
    """
    out = []
    for raw in (references or []):
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source") or "").strip()[:500]
        path_only = source.split("?", 1)[0]
        suffix = "." + path_only.rsplit(".", 1)[-1].lower() if "." in path_only else ""
        if not source.startswith("/media/") or suffix not in H3_VIDEO_EXTENSIONS:
            continue
        try:
            start = float(raw.get("start_sec") or 0.0)
        except (TypeError, ValueError):
            continue
        if start < 0 or not math.isfinite(start):
            continue
        include_audio = raw.get("include_audio", False)
        if not isinstance(include_audio, bool) or include_audio:
            continue
        out.append({
            "source": source,
            "role": str(raw.get("role") or "source motion, performance, and camera movement")[:500],
            "start_sec": start,
            "include_audio": False,
        })
    if len(out) > H3_VIDEO_REF_MAX:
        raise ValueError(f"MiniMax H3 accepts at most {H3_VIDEO_REF_MAX} video references.")
    return out


H3_AUDIO_EXTENSIONS = {".flac", ".m4a", ".mp3", ".ogg", ".wav"}


def normalize_audio_references(references):
    """Validate Real / Long reference-audio descriptors ({source:/media/..., role,
    start_sec}). Pixels and samples stay on disk; the worker stages them."""
    out = []
    for raw in (references or []):
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source") or "").strip()[:500]
        path_only = source.split("?", 1)[0]
        suffix = "." + path_only.rsplit(".", 1)[-1].lower() if "." in path_only else ""
        if not source.startswith("/media/") or suffix not in H3_AUDIO_EXTENSIONS:
            continue
        try:
            start = float(raw.get("start_sec") or 0.0)
        except (TypeError, ValueError):
            continue
        if start < 0 or not math.isfinite(start):
            continue
        out.append({"source": source,
                    "role": str(raw.get("role") or "voice and sound")[:500],
                    "start_sec": start})
    if len(out) > H3_SINGULARITY_AUDIO_REF_MAX:
        raise ValueError(f"Real / Long accepts at most {H3_SINGULARITY_AUDIO_REF_MAX} audio references.")
    return out


def required_variant(references=None):
    """Return the only H3 checkpoint compatible with this request.

    No references preserves the promoted FL2VA start-frame workflow. One or
    more usable reference pictures requires Ref2VA actor cloning. Callers must
    still reject a non-empty but unusable list rather than treating it as the
    no-reference case.
    """
    return H3_REF2VA_VARIANT if has_usable_reference(references) else H3_FL2VA_VARIANT


def required_turbo_preset(request=None):
    """Resolve the opt-in managed H3 Turbo preset for a queued request.

    False/empty means the promoted non-Turbo H3 runtime.  True is shorthand for
    Media Lab's pinned six-step preset. Exact managed preset strings are accepted
    for reproducibility receipts. Everything else fails loudly; arbitrary adapters,
    strengths, step counts, attention modes, and cache settings are not an API.
    """
    value = (request or {}).get("h3_turbo")
    if value in (None, False, 0, "", "off", "false"):
        return None
    if value is True or value == 1:
        return H3_TURBO_PRESET
    if isinstance(value, str) and value in H3_TURBO_PRESETS:
        return value
    raise ValueError(
        f"h3_turbo must be false or one of {sorted(H3_TURBO_PRESETS)!r} "
        f"(got {value!r})")


def turbo_steps(preset):
    """Return the pinned evaluation count for a managed Turbo preset."""
    if preset not in H3_TURBO_PRESETS:
        raise ValueError(f"unsupported managed H3 Turbo preset: {preset!r}")
    return H3_TURBO_PRESETS[preset]


def wants_singularity(request=None):
    """True when this request must run on the Real / Long (Singularity) engine:
    it asked for it (``h3_engine: "singularity"``), or it carries references
    and this host routes H3 reference work there. A fused-r1024 request keeps
    its own explicit checkpoint."""
    request = request or {}
    if request.get("h3_fused_r1024"):
        return False
    if str(request.get("h3_engine") or "").strip().lower() == H3_SINGULARITY_VARIANT:
        return True
    return bool(ROUTE_REFERENCES_TO_SINGULARITY and (
        request.get("references") or request.get("video_references")
        or request.get("audio_references")))


def task_for(request=None):
    """The H3 task family (GPU lease task) a request runs as."""
    request = request or {}
    if wants_singularity(request):
        return H3_SINGULARITY_TASK
    if request.get("references") or request.get("video_references"):
        return "ref2va"
    if request.get("source") or request.get("start_image") or request.get("start_image_b64"):
        return "fl2va"
    return "t2va"


def required_runtime_config(request=None):
    """Return the exact H3 checkpoint+adapter residency needed by a job."""
    request = request or {}
    if wants_singularity(request):
        if required_turbo_preset(request):
            raise ValueError("Real / Long runs its own pinned dual-sampling recipe; "
                             "the managed H3 Turbo preset does not apply to it")
        return {"variant": H3_SINGULARITY_VARIANT, "turbo_preset": None}
    video_refs = normalize_video_references(request.get("video_references") or [])
    if request.get("h3_fused_r1024"):
        if not has_usable_reference(request.get("references") or []):
            raise ValueError("h3_fused_r1024 requires at least one usable identity reference")
        if not str(request.get("source") or "").strip():
            raise ValueError("h3_fused_r1024 requires an approved start-frame source")
        variant = H3_FUSED_R1024_VARIANT
    else:
        variant = (H3_REF2VA_VARIANT if video_refs else
                   required_variant(request.get("references") or []))
    return {
        "variant": variant,
        "turbo_preset": required_turbo_preset(request),
    }


def resolve_reference_detail(reference_detail=None):
    """Resolve + validate the `reference_detail` value. Returns the string, or
    raises ValueError when the caller supplied something outside match|max."""
    detail = str(reference_detail or "match")
    if detail not in ALLOWED_REFERENCE_DETAIL:
        raise ValueError(
            f"reference_detail must be one of {sorted(ALLOWED_REFERENCE_DETAIL)} "
            f"(got {detail!r})")
    return detail


def validate_h3_reference_request(engine, variant, references, reference_detail="match"):
    """Fail-closed gate for an H3 reference (Ref2VA) request.

    Returns None when the request may proceed, else an error string safe to
    surface as an HTTP 400 or a job failure. Checked in order so an invalid
    detail is never masked by an empty reference list:

      1. reference_detail is one of match|max.
      2. The actor-cloning checkpoint (variant == ref2va) REQUIRES a usable
         reference image. If it is resident but no decodable picture was
         supplied, refuse hard — never run the actor-cloning model actorless.
      3. references present implies engine == "h3" (never a silent LTX/other).
      4. references present implies resident H3 variant == "ref2va" (never
         fl2va — feeding pictures to the start-frame model is a likeness lie).
      5. references present implies at least one decodable reference image.
    """
    try:
        resolve_reference_detail(reference_detail)
    except ValueError as exc:
        return str(exc)
    variant = variant or H3_FL2VA_VARIANT
    usable = has_usable_reference(references)
    if variant == H3_SINGULARITY_VARIANT:
        # Real / Long takes text alone or up to nine pictures; it is never an
        # actorless actor-cloning run because it is a text+reference model.
        if references and engine != "h3":
            return ("H3 reference conditioning requires the h3 ENGINE, but the "
                    f"resident engine is {engine!r}. Refusing a silent fallback.")
        if references and not usable:
            return "references were requested but none carried a decodable image"
        return None
    # INVARIANT: ref2va resident + no usable reference == hard fail. This must
    # be checked before the generic empty-list check below.
    if variant in (H3_REF2VA_VARIANT, H3_FUSED_R1024_VARIANT) and not usable:
        return ("Ref2VA actor-cloning is resident but no usable reference picture "
                "was supplied. Refusing to run the actor-cloning model with no "
                "actors — pass at least one decodable reference image.")
    if not references:
        return None
    if engine != "h3":
        return ("H3 reference (Ref2VA) conditioning requires the h3 ENGINE, but the "
                f"resident engine is {engine!r}. Refusing a silent fallback.")
    if variant not in (H3_REF2VA_VARIANT, H3_FUSED_R1024_VARIANT):
        return ("Ref2VA actor-cloning was requested but the resident H3 checkpoint is "
                f"{variant!r}, not an actor-capable ref2va/fused variant. Refusing to feed "
                "reference pictures to the fl2va start-frame model. Boot an actor-capable "
                "H3 variant first.")
    if not usable:
        return "references were requested but none carried a decodable image"
    return None


def assert_ref_count_ok(reference_count, singularity=False):
    """Refuse more separate reference pictures than the engine can take, rather
    than ever concatening people into a contact-sheet input."""
    if singularity:
        if reference_count > H3_SINGULARITY_REF_MAX:
            raise ValueError(
                f"Real / Long accepts at most {H3_SINGULARITY_REF_MAX} separate reference "
                f"pictures (got {reference_count}).")
        return
    if reference_count > H3_REF_MAX:
        raise ValueError(
            f"H3 Ref2VA accepts at most {H3_REF_MAX} separate reference pictures "
            f"(got {reference_count}). Refusing to concatenate into a contact sheet — "
            "pass separate clean crops instead.")