"""Per-host Media Lab settings: one small env file instead of edited literals.

Every value that used to be a hard-coded hostname, tailnet address, home
directory or model path now comes from here. Resolution order, highest wins:

  1. the process environment (``MEDIA_LAB_BIND_HOST=... python -m uvicorn``),
  2. ``$MEDIA_LAB_HOME/config/local.env`` (gitignored, per machine),
  3. ``<repo>/config/local.env`` next to this package (a dev checkout),
  4. the defaults below, which describe a private single-machine install.

The file is plain ``KEY=VALUE`` lines (``export KEY=VALUE`` and quotes are
tolerated, ``#`` starts a comment) so the same file can be sourced by the
shell runners and read by ``EnvironmentFile=`` in systemd units. Stdlib only:
the runner scripts import this from a bare timer with no virtualenv.

``python -m media_lab_core.local_config`` prints the resolved values.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULTS: dict[str, str] = {
    # Data root: jobs.json, media/, pool/, config/local.env. install.sh keeps a
    # symlink at ~/media-lab-simple when it is moved elsewhere.
    "MEDIA_LAB_HOME": "~/media-lab-simple",
    # Address uvicorn and the sidecars bind. 127.0.0.1 is private-by-default;
    # a tailnet address makes the studio reachable from paired devices.
    "MEDIA_LAB_BIND_HOST": "127.0.0.1",
    # Hostnames a reverse proxy / tunnel presents (comma separated). Requests
    # carrying one of these are treated as public-edge traffic.
    "MEDIA_LAB_PUBLIC_HOSTS": "",
    "MEDIA_LAB_BROWSER_ORIGINS": "",
    "MEDIA_LAB_INFERENCE_LOCK": "",
    "MEDIA_LAB_GPU_LOCK": "",
    # Where engine weights live (HunyuanVideo-Avatar, MuseTalk, ...).
    "MEDIA_LAB_MODELS_ROOT": "~/.local/share/media-lab-p2-models",
    # Where engine runtimes are checked out (LatentSync.stage, comfy-*, ...).
    "MEDIA_LAB_RUNTIME_ROOT": "~/runtime",
    # This machine's tailnet address or MagicDNS name, if it has one. Not a
    # trust grant: tailnet devices enter the family code like everyone else.
    # The bind-readiness drop-in waits for it after boot.
    "MEDIA_LAB_TAILNET_HOST": "",
    # The OpenAI-compatible text runtime (PPLX/Flash) behind the studio.
    "MEDIA_LAB_TEXT_UPSTREAM": "http://127.0.0.1:8004",
    # Optional: ssh target of the studio host for Mac-side helpers
    # (local_studio.py, tools/topaz_worker.py). Empty disables them.
    "MEDIA_LAB_SSH": "",
    # Resident text-model budget (GB) and the unified-memory ceiling app.py
    # plans against. 0 = the chat model is served remotely, nothing resident.
    "MEDIA_LAB_QWEN_GB": "0",
    "MEDIA_LAB_MEM_CAP_GB": "120",
    "MEDIA_LAB_H3_LTX_RETAKE_STRENGTH": "0.35",
    # Always-warm H3 (idle profile qwen-h3): the Sol task the idle preload boots
    # (t2va = text-only, what nearly every H3 job uses; or fl2va); the quiet
    # seconds with no local GPU job before a pushed-out H3 is reloaded; how long
    # a queued H3 take may wait behind other work while H3 is out; and the
    # bounded memory-settle gate in front of every H3 cold load.
    "MEDIA_LAB_H3_IDLE_TASK": "t2va",
    "MEDIA_LAB_H3_RESTORE_QUIET_S": "300",
    "MEDIA_LAB_GRACEFUL_HANDOFF": "0",
    "MEDIA_LAB_HOLD_AUTORECOVER": "0",
    "MEDIA_LAB_H3_CACHE_TRIM": "0",
    "MEDIA_LAB_BOOT_AUTOCLEAR": "0",
    "MEDIA_LAB_H3_BATCH_MAX_WAIT_S": "900",
    "MEDIA_LAB_H3_LOAD_SETTLE_MAX_PSI": "2",
    "MEDIA_LAB_H3_LOAD_SETTLE_MAX_PSI60": "3",
    "MEDIA_LAB_H3_LOAD_SETTLE_SAMPLES": "10",
    "MEDIA_LAB_H3_LOAD_SETTLE_MAX_WAIT_S": "120",
    # Sol-H3-Spark (the whole-box H3 video engine). Empty SOL_PKG = not installed.
    "SOL_PKG": "",
    "SOL_ROOT": "~/.local/share/sol-h3-spark",
    "SOL_H3_SPARK_RUNTIME_ROOT": "",
    "SOL_H3_SPARK_QWEN_WEIGHTS_ROOT": "~/.local/share",
    "SOL_H3_SPARK_QWEN_IMAGE": "sol-h3-spark-qwen",
    # Real / Long: the H3 Singularity dual-sampling engine (runner/h3_singularity.py),
    # a load-on-demand variant of the H3 unit. It needs its own isolated ComfyUI
    # (with the MiniMax H3, KJNodes, rgthree, VideoHelperSuite and H3 latent
    # upscaler nodes) and the lean weight set under H3_SINGULARITY_MODELS_ROOT
    # (diffusion_models/, text_encoders/, vae/, loras/). Personal licence: it is
    # also gated by MEDIA_LAB_PERSONAL_ENGINES=h3,h3-singularity.
    "H3_SINGULARITY_COMFY_DIR": "",
    "H3_SINGULARITY_MODELS_ROOT": "",
    "H3_SINGULARITY_EXTRA_LORAS": "",
    "H3_SINGULARITY_UPSCALERS": "",
    "H3_SINGULARITY_PORT": "18188",
    "H3_SINGULARITY_MAX_FRAMES": "362",
    # how long a loaded Real / Long engine waits for the next take before the
    # studio stands it down and brings the warm Sol engine back
    "MEDIA_LAB_H3_SINGULARITY_LINGER_S": "600",
    # Lip-sync check after each Real / Long take (media_lab_core/av_sync.py):
    # a SyncNet environment (python with torch/opencv/scipy/python_speech_features)
    # and a checkout holding eval/syncnet, eval/detectors and
    # checkpoints/auxiliary/{syncnet_v2.model,sfd_face.pth}. auto = on when both
    # exist; off disables it.
    "MEDIA_LAB_AV_SYNC": "auto",
    "MEDIA_LAB_AV_SYNC_PYTHON": "",
    "MEDIA_LAB_AV_SYNC_ROOT": "",
    "MEDIA_LAB_AV_SYNC_THREADS": "8",
    # YuE2 music engine (runner/yue2_engine_server.py): the isolated kit
    # (venv, YuE checkout, SheetSage2 venv), the weights root holding
    # YuE2-3B / YuE2-Vae / SheetSage2 / MERT-v2-FullSong, and the loopback port
    # app.py starts the transient unit media-lab-yue2.service on.
    "YUE2_KIT": "~/runtime/yue2-iso",
    "YUE2_MODELS_ROOT": "~/.local/share/media-lab-p3-models/yue2",
    "YUE2_PORT": "8197",
    # Mel-Band RoFormer vocal/instrumental separator used for song stems:
    # <root>/.venv/bin/melband-roformer-infer and <root>/models/<model>.
    "MELBAND_ROFORMER_ROOT": "~/runtime/melband-roformer-0.1.5",
    "MELBAND_ROFORMER_MODEL": "melband-roformer-kim-vocals",
    # Engines whose model licence is personal / non-commercial / restricted
    # (see media_lab_core/engine_licences.py and docs/ENGINE-LICENCES.md) stay
    # OFF until this host names them: a comma list of engine ids, or "all".
    # Only enable one if your use fits its licence.
    "MEDIA_LAB_PERSONAL_ENGINES": "",
    # Web-push contact (the VAPID "sub" claim): mailto:you@example.com or an
    # https:// URL. Empty = the project's public URL, never a person's inbox.
    "MEDIA_LAB_VAPID_SUBJECT": "",
    # Character names whose cast jobs get the likeness-qualification framing
    # guard from the director (large faces, restrained expression). Comma
    # separated, case-insensitive. Empty = no named qualification cast.
    "MEDIA_LAB_QUALIFICATION_CAST": "",
}

_PATH_KEYS = {"MEDIA_LAB_HOME", "MEDIA_LAB_MODELS_ROOT", "MEDIA_LAB_RUNTIME_ROOT",
              "SOL_PKG", "SOL_ROOT", "SOL_H3_SPARK_RUNTIME_ROOT",
              "SOL_H3_SPARK_QWEN_WEIGHTS_ROOT", "YUE2_KIT", "YUE2_MODELS_ROOT",
              "MELBAND_ROFORMER_ROOT", "H3_SINGULARITY_COMFY_DIR", "H3_SINGULARITY_MODELS_ROOT",
              "H3_SINGULARITY_EXTRA_LORAS", "H3_SINGULARITY_UPSCALERS",
              "MEDIA_LAB_AV_SYNC_PYTHON", "MEDIA_LAB_AV_SYNC_ROOT"}

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines. Missing or unreadable file -> {} (never raises)."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _expand(key: str, value: str) -> str:
    value = os.path.expandvars(value)
    if key in _PATH_KEYS and value:
        value = os.path.expanduser(value)
    return value


def env_file_candidates() -> list[Path]:
    """Where local.env is looked for, in precedence order (first wins per key)."""
    home_raw = os.environ.get("MEDIA_LAB_HOME", "").strip() or DEFAULTS["MEDIA_LAB_HOME"]
    home = Path(os.path.expanduser(os.path.expandvars(home_raw)))
    candidates = [home / "config" / "local.env", SOURCE_ROOT / "config" / "local.env"]
    seen: list[Path] = []
    for c in candidates:
        if c not in seen:
            seen.append(c)
    return seen


def load(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve every known key: environ > local.env files > defaults."""
    environ = os.environ if environ is None else environ
    resolved: dict[str, str] = dict(DEFAULTS)
    for path in reversed(env_file_candidates()):
        for key, value in parse_env_file(path).items():
            if key in DEFAULTS:
                resolved[key] = value
    for key in DEFAULTS:
        if key in environ and environ[key] != "":
            resolved[key] = environ[key]
    return {key: _expand(key, value) for key, value in resolved.items()}


def get(key: str, default: str | None = None) -> str:
    """One resolved value. Unknown keys fall back to the environment."""
    if key in DEFAULTS:
        return load()[key]
    return os.environ.get(key, "" if default is None else default)


def path(key: str) -> Path:
    return Path(get(key))


def home() -> Path:
    return path("MEDIA_LAB_HOME")


def bind_host() -> str:
    return get("MEDIA_LAB_BIND_HOST") or "127.0.0.1"


def tailnet_host() -> str:
    return get("MEDIA_LAB_TAILNET_HOST").strip()


def public_hosts() -> set[str]:
    return {h.strip().lower() for h in get("MEDIA_LAB_PUBLIC_HOSTS").split(",") if h.strip()}


# Browser origins the VibeX Studio app may call this studio from. Native apps
# (iOS/Android) are not subject to CORS; the desktop shell and any web build are.
# The Tauri desktop origins are always allowed; add exact http(s) origins for
# a web build or a dev server, e.g. http://localhost:8098.
TAURI_DESKTOP_ORIGINS = ("tauri://localhost", "http://tauri.localhost")


def browser_origins() -> list[str]:
    extra = [o.strip().rstrip("/") for o in get("MEDIA_LAB_BROWSER_ORIGINS").split(",") if o.strip()]
    out = list(TAURI_DESKTOP_ORIGINS)
    for o in extra:
        if o not in out:
            out.append(o)
    return out


def runtime_dir() -> str:
    """The per-user runtime directory (systemd's XDG_RUNTIME_DIR, else /run/user/<uid>)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg and os.path.isdir(xdg):
        return xdg.rstrip("/")
    try:
        candidate = f"/run/user/{os.getuid()}"
    except AttributeError:  # non-POSIX
        candidate = ""
    if candidate and os.path.isdir(candidate):
        return candidate
    import tempfile  # a Mac or a container without a per-user runtime dir
    return tempfile.gettempdir().rstrip("/")


def inference_lock() -> str:
    """One inference at a time on the GPU: every engine and the chat loop flock this."""
    return get("MEDIA_LAB_INFERENCE_LOCK") or f"{runtime_dir()}/media-lab-inference.lock"


def gpu_lock() -> str:
    """The residency pool lease (held by the pool service; the supervisor re-takes it at boot)."""
    return get("MEDIA_LAB_GPU_LOCK") or f"{runtime_dir()}/spark-gpu.lock"


def own_addresses() -> set[str]:
    """This machine's own studio addresses: loopback, the bind address and the
    tailnet address when one is configured.

    NEVER an authority. A Host header naming one of these proves nothing (any
    client can send it), and a connection FROM one of these is usually a local
    proxy (cloudflared, ``tailscale serve``) carrying someone else's request.
    The studio uses this set only to recognise its own proxies when it picks a
    throttling identity, and to decide where the local tool token may be sent.
    """
    hosts = {"127.0.0.1", "localhost", bind_host()}
    if tailnet_host():
        hosts.add(tailnet_host())
    hosts.discard("0.0.0.0")
    hosts.discard("")
    return hosts


def models_root() -> Path:
    return path("MEDIA_LAB_MODELS_ROOT")


def runtime_root() -> Path:
    return path("MEDIA_LAB_RUNTIME_ROOT")


def text_upstream() -> str:
    return get("MEDIA_LAB_TEXT_UPSTREAM").rstrip("/") or "http://127.0.0.1:8004"


def studio_url(port: int = 7863) -> str:
    """The studio's own base URL as seen from this machine (watchdogs, canaries)."""
    host = bind_host()
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def int_value(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        for p in env_file_candidates():
            raw = parse_env_file(p).get(key, "").strip()
            if raw:
                break
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def sol() -> dict[str, str]:
    """The SOL_* keys, resolved, for the Sol-H3-Spark engine command."""
    values = load()
    return {k: values[k] for k in values if k.startswith("SOL_")}


def sol_configured() -> bool:
    return bool(sol().get("SOL_PKG"))


def singularity() -> dict[str, str]:
    """The H3_SINGULARITY_* and MEDIA_LAB_AV_SYNC* keys, resolved, for the Real /
    Long engine's per-load runtime environment."""
    values = load()
    return {k: values[k] for k in values
            if k.startswith("H3_SINGULARITY_") or k.startswith("MEDIA_LAB_AV_SYNC")}


def singularity_configured() -> bool:
    values = singularity()
    comfy = values.get("H3_SINGULARITY_COMFY_DIR", "")
    return bool(sol_configured() and comfy and values.get("H3_SINGULARITY_MODELS_ROOT")
                and (Path(comfy) / "main.py").is_file())


def yue2() -> dict[str, str]:
    """The YUE2_* keys, resolved, for the YuE2 music engine command."""
    values = load()
    return {k: values[k] for k in values if k.startswith("YUE2_")}


def yue2_port() -> int:
    return int_value("YUE2_PORT", int(DEFAULTS["YUE2_PORT"]))


def melband() -> dict[str, str]:
    """The MELBAND_ROFORMER_* keys, resolved, for the stem separator."""
    values = load()
    return {k: values[k] for k in values if k.startswith("MELBAND_ROFORMER_")}


def personal_engines() -> set[str]:
    """Engine ids this host has opted into despite a personal / non-commercial
    licence (MEDIA_LAB_PERSONAL_ENGINES). "all" enables every such engine."""
    raw = get("MEDIA_LAB_PERSONAL_ENGINES")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


# The VAPID "sub" claim push services may use to reach the server's operator.
# A public install must never announce a person's inbox, so the default is the
# project's own page; set MEDIA_LAB_VAPID_SUBJECT for a real contact.
DEFAULT_VAPID_SUBJECT = "https://github.com/kerpopule/vibexstudio"


def vapid_subject() -> str:
    value = get("MEDIA_LAB_VAPID_SUBJECT").strip()
    if value.startswith("mailto:") and "@" in value or value.startswith("https://"):
        return value
    return DEFAULT_VAPID_SUBJECT


def qualification_cast() -> set[str]:
    raw = get("MEDIA_LAB_QUALIFICATION_CAST")
    return {n.strip().casefold() for n in raw.split(",") if n.strip()}


def overlay_dirs() -> list[Path]:
    """The per-studio overlay folders (gitignored config/local/), highest
    precedence first: the data root's, then the checkout's. Existing ones only.
    See docs/LOCAL-OVERLAY.md for what may live there."""
    out: list[Path] = []
    for base in (home(), SOURCE_ROOT):
        d = base / "config" / "local"
        if d.is_dir() and d.resolve() not in [o.resolve() for o in out]:
            out.append(d)
    return out


def subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ plus every resolved key: hand this to shell runners."""
    env = dict(os.environ if base is None else base)
    env.update({k: v for k, v in load().items() if v})
    return env


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    values = load()
    if argv and argv[0] == "--sh":
        for key, value in values.items():
            print(f"export {key}='{value}'")
        return 0
    if argv:
        for key in argv:
            print(values.get(key, os.environ.get(key, "")))
        return 0
    print("# resolved Media Lab settings (env > local.env > defaults)")
    for p in env_file_candidates():
        print(f"# {'read' if p.is_file() else 'absent'}: {p}")
    for key, value in values.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
