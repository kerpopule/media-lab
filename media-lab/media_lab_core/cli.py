"""``media-lab`` — the terminal control surface install.sh leaves behind.

    media-lab status [--json]      health, GPU, engines, service state
    media-lab pair   [--json]      the pairing QR, URLs and family code again
    media-lab code   [--rotate]    show / rotate the family code (admin: --admin)
    media-lab code --set-family    set a family code you chose (read from stdin / --from)
    media-lab code --locks         who the code prompt is locked for right now
    media-lab code --unlock [IP]   lift code-prompt lockouts (all, or one address)
    media-lab start|stop|restart   service-aware (systemd --user / launchd),
                                   foreground/detached fallback otherwise
    media-lab logs   [-f]          journal, launchd log file, or the pid-mode log
    media-lab setup  [...]         the catalog planner + where the web wizard is
    media-lab uninstall [--yes]    remove service + venv; models and media stay

Standard library only (plus the ``qrcode`` package for the QR), so it also
runs under the system Python when the venv is gone — ``status`` and
``uninstall`` still work on a half-removed install.

Facts it relies on from app.py (never edited here):
  * the data root is ``~/media-lab-simple`` (``MEDIA_LAB_HOME`` is honoured by
    install.sh via a symlink, see there);
  * ``access-code.txt`` (the family code) / ``admin-pin.txt`` (the admin code)
    under that root are the two door codes, mode 0600. A running server picks up
    a rewritten code within a second, so rotating needs no restart;
  * ``/manifest.json`` is gate-exempt and CORS-open: the liveness probe;
  * ``/api/setup/status`` needs a session cookie from ``POST /api/gate``.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import door_lockout, family_code, pairing, secret_files

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PORT = pairing.DEFAULT_PORT
CONFIG_NAME = "install.json"
PID_NAME = "media-lab.pid"
LOG_REL = Path("logs") / "media-lab.log"
SYSTEMD_UNIT = "media-lab.service"
LEGACY_UNIT = "media-lab-simple.service"     # a Spark deployed by hand
LAUNCHD_LABEL = "com.medialab.server"
MANAGED_MARKER = "managed by media-lab install.sh"


# ---------------------------------------------------------------------------
# paths + config
# ---------------------------------------------------------------------------

def app_root() -> Path:
    """Where app.py keeps everything. ``MEDIA_LAB_HOME`` wins when set (install.sh
    links ``~/media-lab-simple`` to it); otherwise app.py's own default."""
    env = os.environ.get("MEDIA_LAB_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / "media-lab-simple"


def config_path(root: Path | None = None) -> Path:
    return (root or app_root()) / CONFIG_NAME


def load_config(root: Path | None = None) -> dict:
    p = config_path(root)
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict, root: Path | None = None) -> Path:
    p = config_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)
    return p


def effective(cfg: dict, key: str, override=None, default=None):
    if override is not None:
        return override
    return cfg.get(key, default)


def venv_python(cfg: dict | None = None) -> Path:
    cfg = cfg or {}
    venv = Path(cfg.get("venv") or (REPO / ".venv"))
    return venv / "bin" / "python"


def read_code(path: Path) -> str | None:
    """The code as stored (word codes stay lower-case and dashed, which is how
    people read them; the server ignores case and separators)."""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return text or None


def code_paths(root: Path) -> tuple[Path, Path]:
    return root / "access-code.txt", root / "admin-pin.txt"


def mint_access_code(rng=None) -> str:
    """A new FAMILY code: four everyday words (about 41 bits)."""
    return family_code.mint_family(rng)


def mint_admin_code(rng=None) -> str:
    """A new ADMIN code: six words (about 62 bits) — never a 4-digit PIN."""
    return family_code.mint_admin(rng)


def write_code(path: Path, value: str) -> None:
    """Atomically replace a code file; 0600 from birth, never world-readable."""
    secret_files.write_private(path, value + "\n")


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------

def probe_host(bind: str) -> str:
    return "127.0.0.1" if bind in ("", "0.0.0.0", "::", "*") else bind


def manifest_up(port: int, bind: str = "0.0.0.0", timeout: float = 3.0) -> bool:
    url = pairing.server_url(probe_host(bind), port) + "/manifest.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def studio_health(port: int, bind: str = "0.0.0.0", timeout: float = 5.0):
    """The studio's own /api/health (level, reasons, facts), or None.

    Proves it runs on this machine with the local tool token, the same way the
    watchdogs do; the token goes only to the studio's own address and port."""
    from . import local_token
    url = pairing.server_url(probe_host(bind), port) + "/api/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(local_token.authorize(urllib.request.Request(url)), timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_for_manifest(port: int, bind: str = "0.0.0.0", timeout: float = 120,
                      alive=None) -> bool:
    """Poll /manifest.json until it answers. ``alive`` (a callable) lets the
    caller stop early when the process it started has died."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if manifest_up(port, bind, 2):
            return True
        if alive is not None and not alive():
            return False
        time.sleep(1)
    return False


def gpu_info() -> dict:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"present": False, "names": [], "note":
                "no NVIDIA GPU — local engines are off; add a fal.ai key inside the app "
                "(theme sheet → Cloud providers) for cloud rendering"}
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10)
        names = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        names = []
    return {"present": True, "names": names, "note":
            "NVIDIA GPU present — the first-run shelf in the web UI can install LTX and "
            "the other local engines"}


def setup_status(port: int, bind: str, access_code: str | None, timeout: float = 8) -> dict | None:
    """``/api/setup/status`` through the front door: POST the family code to
    ``/api/gate`` for a session cookie, then read. None when unreachable."""
    if not access_code:
        return None
    base = pairing.server_url(probe_host(bind), port)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        req = urllib.request.Request(base + "/api/gate", method="POST",
                                     data=json.dumps({"code": access_code}).encode(),
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=timeout) as r:
            if json.loads(r.read().decode() or "{}").get("ok") is not True:
                return None
        with opener.open(base + "/api/setup/status", timeout=timeout) as r:
            data = json.loads(r.read().decode())
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# service management
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 60)
    return subprocess.run(cmd, **kw)


def service_kind() -> str:
    """'systemd' on Linux with a user session, 'launchd' on macOS, else 'none'."""
    if platform.system() == "Darwin" and shutil.which("launchctl"):
        return "launchd"
    if platform.system() == "Linux" and shutil.which("systemctl"):
        try:
            if _run(["systemctl", "--user", "show-environment"], timeout=10).returncode == 0:
                return "systemd"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "none"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _managed(path: Path) -> bool:
    try:
        return MANAGED_MARKER in path.read_text()
    except OSError:
        return False


def render_template(text: str, values: dict) -> str:
    out = text
    for key, val in values.items():
        out = out.replace(f"@@{key}@@", str(val))
    leftover = [tok for tok in out.split("@@")[1::2]]
    if leftover:
        raise ValueError(f"template placeholders left unfilled: {sorted(set(leftover))}")
    return out


def template_values(cfg: dict, root: Path) -> dict:
    venv = Path(cfg.get("venv") or (REPO / ".venv"))
    path_extra = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return {
        "REPO": REPO,
        "VENV": venv,
        "PORT": cfg.get("port", DEFAULT_PORT),
        "BIND": cfg.get("bind", "0.0.0.0"),
        "ROOT": root,
        "LOG": root / LOG_REL,
        "HOME": Path.home(),
        "PATH": f"{venv / 'bin'}:{path_extra}",
        "LABEL": LAUNCHD_LABEL,
        "MARKER": MANAGED_MARKER,
    }


def service_state() -> dict:
    """{'kind', 'name', 'state', 'managed'} — state in running/stopped/absent."""
    kind = service_kind()
    if kind == "systemd":
        for unit in (SYSTEMD_UNIT, LEGACY_UNIT):
            cat = _run(["systemctl", "--user", "cat", unit], timeout=15)
            if cat.returncode != 0:
                continue
            active = _run(["systemctl", "--user", "is-active", unit], timeout=15).stdout.strip()
            return {"kind": kind, "name": unit,
                    "state": "running" if active == "active" else "stopped",
                    "managed": MANAGED_MARKER in cat.stdout}
        return {"kind": kind, "name": SYSTEMD_UNIT, "state": "absent", "managed": False}
    if kind == "launchd":
        plist = launchd_plist_path()
        if not plist.exists():
            return {"kind": kind, "name": LAUNCHD_LABEL, "state": "absent", "managed": False}
        info = _run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=15)
        running = info.returncode == 0 and "state = running" in info.stdout
        return {"kind": kind, "name": LAUNCHD_LABEL,
                "state": "running" if running else "stopped", "managed": _managed(plist)}
    return {"kind": "none", "name": "", "state": "absent", "managed": False}


def pid_path(root: Path) -> Path:
    return root / PID_NAME


def pid_alive(root: Path) -> int | None:
    try:
        pid = int(pid_path(root).read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def install_service(cfg: dict, root: Path, out=print) -> dict:
    kind = service_kind()
    values = template_values(cfg, root)
    (root / LOG_REL).parent.mkdir(parents=True, exist_ok=True)
    if kind == "systemd":
        target = systemd_unit_path()
        if target.exists() and not _managed(target):
            raise RuntimeError(f"{target} exists and was not written by install.sh — "
                               "move it aside or run with --no-service")
        legacy = _run(["systemctl", "--user", "cat", LEGACY_UNIT], timeout=15)
        if legacy.returncode == 0 and MANAGED_MARKER not in legacy.stdout:
            raise RuntimeError(f"{LEGACY_UNIT} already runs this machine's Media Lab — "
                               "this looks like a hand-deployed box; use --no-service, or "
                               "`media-lab status` to see it")
        text = render_template((REPO / "systemd" / SYSTEMD_UNIT).read_text(), values)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _run(["systemctl", "--user", "daemon-reload"])
        if shutil.which("loginctl"):
            user = os.environ.get("USER") or os.getlogin()
            linger = _run(["loginctl", "show-user", user, "--property=Linger", "--value"], timeout=15)
            if linger.stdout.strip() != "yes":
                r = _run(["loginctl", "enable-linger", user], timeout=30)
                if r.returncode == 0:
                    out("  lingering enabled — the studio survives logout and reboot")
                else:
                    out(f"  could not enable lingering (run: sudo loginctl enable-linger {user}) — "
                        "the studio stops when you log out")
        r = _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT], timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"systemctl --user enable --now failed: {r.stderr.strip()}")
        _run(["systemctl", "--user", "restart", SYSTEMD_UNIT], timeout=60)   # pick up code updates
        return {"kind": kind, "name": SYSTEMD_UNIT, "path": str(target)}
    if kind == "launchd":
        target = launchd_plist_path()
        if target.exists() and not _managed(target):
            raise RuntimeError(f"{target} exists and was not written by install.sh — "
                               "move it aside or run with --no-service")
        text = render_template((REPO / "launchd" / f"{LAUNCHD_LABEL}.plist").read_text(), values)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], timeout=30)
        r = _run(["launchctl", "bootstrap", domain, str(target)], timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
        _run(["launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}"], timeout=30)
        return {"kind": kind, "name": LAUNCHD_LABEL, "path": str(target)}
    raise RuntimeError("no service manager here (need systemd --user or launchd); "
                       "use --no-service")


def remove_service(out=print) -> None:
    kind = service_kind()
    if kind == "systemd":
        target = systemd_unit_path()
        if target.exists() and _managed(target):
            _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], timeout=60)
            target.unlink()
            _run(["systemctl", "--user", "daemon-reload"])
            out(f"  removed {target}")
    elif kind == "launchd":
        target = launchd_plist_path()
        if target.exists() and _managed(target):
            _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=30)
            target.unlink()
            out(f"  removed {target}")


def uvicorn_command(cfg: dict) -> list[str]:
    py = venv_python(cfg)
    return [str(py), "-m", "uvicorn", "app:app",
            "--host", str(cfg.get("bind", "0.0.0.0")), "--port", str(cfg.get("port", DEFAULT_PORT))]


def start_detached(cfg: dict, root: Path) -> int:
    """No service manager (or --no-service): nohup-style child with a pidfile."""
    log = root / LOG_REL
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    with open(log, "ab") as fh:
        proc = subprocess.Popen(uvicorn_command(cfg), cwd=str(REPO), stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    pid_path(root).write_text(f"{proc.pid}\n")
    return proc.pid


def stop_pid(root: Path, timeout: float = 20) -> bool:
    pid = pid_alive(root)
    if pid is None:
        pid_path(root).unlink(missing_ok=True)
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline and pid_alive(root) is not None:
        time.sleep(0.5)
    if pid_alive(root) is not None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    pid_path(root).unlink(missing_ok=True)
    return True


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _bind_from_local_env(root: Path) -> str | None:
    try:
        for line in (Path(root) / "config" / "local.env").read_text().splitlines():
            if line.startswith("MEDIA_LAB_BIND_HOST="):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    except OSError:
        pass
    return None


def build_status(cfg: dict, root: Path, with_engines: bool = True) -> dict:
    port = int(cfg.get("port", DEFAULT_PORT))
    # A studio bound to one address (MEDIA_LAB_BIND_HOST, e.g. the tailnet IP)
    # does not answer on 127.0.0.1: probe where it really listens.
    bind = cfg.get("bind") or os.environ.get("MEDIA_LAB_BIND_HOST") or _bind_from_local_env(root) or "0.0.0.0"
    access_path, admin_path = code_paths(root)
    up = manifest_up(port, bind)
    svc = service_state()
    pid = pid_alive(root)
    access_code = read_code(access_path)
    engines = setup_status(port, bind, access_code) if (up and with_engines) else None
    return {
        "ok": up,
        "url": pairing.server_url(probe_host(bind), port),
        "port": port,
        "bind": bind,
        "repo": str(REPO),
        "venv_python": str(venv_python(cfg)),
        "venv_present": venv_python(cfg).exists(),
        "data_root": str(root),
        "config": str(config_path(root)),
        "codes": {"access_code_file": str(access_path), "admin_code_file": str(admin_path),
                  "present": access_code is not None},
        "gpu": gpu_info(),
        "service": {**svc, "pid": pid, "mode": (svc["kind"] if svc["state"] != "absent"
                                                 else ("pid" if pid else "none"))},
        "engines": (engines or {}).get("engines") if engines else None,
        "first_run": (engines or {}).get("first_run") if engines else None,
        "fal_configured": (engines or {}).get("fal_configured") if engines else None,
        "health": studio_health(port, bind) if up else None,
        "platform": {"system": platform.system(), "machine": platform.machine(),
                     "python": platform.python_version()},
    }


def print_status(st: dict) -> None:
    print(f"Media Lab      {'UP' if st['ok'] else 'DOWN'}   {st['url']}")
    print(f"Data root      {st['data_root']}")
    svc = st["service"]
    if svc["mode"] == "pid":
        print(f"Process        pid {svc['pid']} (no service manager; `media-lab stop` ends it)")
    elif svc["mode"] == "none":
        print("Process        not running as a service  (`media-lab start` or ./install.sh)")
    else:
        tag = "" if svc.get("managed") else "  (not managed by install.sh)"
        print(f"Service        {svc['name']} — {svc['state']}{tag}")
    health = st.get("health")
    if health:
        print(f"Health         {health.get('level', '?').upper()}"
              + (f" — {'; '.join(health.get('reasons') or [])}" if health.get("reasons") else ""))
    g = st["gpu"]
    print(f"GPU            {', '.join(g['names']) if g['names'] else ('present' if g['present'] else 'none')}")
    print(f"               {g['note']}")
    if st["engines"] is not None:
        print("Engines")
        for name, e in st["engines"].items():
            detail = f" — {e['detail']}" if e.get("detail") else ""
            print(f"  {name:<10} {e['state']:<11}{detail}")
        if st.get("fal_configured"):
            print("  fal.ai     configured")
    elif st["ok"]:
        print("Engines        (could not read /api/setup/status — is the family code file readable?)")


def cmd_status(args, cfg, root) -> int:
    st = build_status(cfg, root)
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        print_status(st)
    return 0 if st["ok"] else 1


def pairing_payload(cfg: dict, root: Path) -> dict:
    access_path, admin_path = code_paths(root)
    port = int(cfg.get("port", DEFAULT_PORT))
    summary = pairing.pairing_summary(port, pairing.list_ipv4_addresses(),
                                      read_code(access_path), read_code(admin_path),
                                      cfg.get("bind", "0.0.0.0"))
    summary["up"] = manifest_up(port, cfg.get("bind", "0.0.0.0"))
    summary["access_code_file"] = str(access_path)
    summary["admin_code_file"] = str(admin_path)
    return summary


def cmd_pair(args, cfg, root) -> int:
    summary = pairing_payload(cfg, root)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    try:
        qr = pairing.render_qr_text(summary["link"], invert=not args.no_invert)
    except RuntimeError as exc:
        qr = None
        print(f"warning: {exc}", file=sys.stderr)
    text = pairing.format_summary(summary, qr, summary["access_code_file"], summary["admin_code_file"])
    if not summary["up"]:
        text = text.replace("Media Lab is up.",
                            "Media Lab is NOT answering right now (start it with `media-lab start`).")
    print(text)
    return 0


# ---------------------------------------------------------------------------
# the door: a family code you choose, and the lockout
# ---------------------------------------------------------------------------

GATE_MAX_CHARS = 80      # the code box on the gate page takes at most this many
UNLOCK_WAIT_S = 10.0     # the running studio checks for an unlock request every 3 s
_pause = time.sleep      # (a seam for the tests)


def read_chosen_code(source: str | None) -> str:
    """The family code the owner chose, from a file or stdin -- never argv, so
    it never shows up in `ps` or shell history. A terminal gets a hidden
    prompt, asked twice."""
    if source and source != "-":
        text = Path(source).expanduser().read_text(encoding="utf-8")
    elif sys.stdin.isatty():
        import getpass
        text = getpass.getpass("New family code: ")
        if getpass.getpass("The same code again: ") != text:
            raise ValueError("the two entries differ; nothing changed")
    else:
        text = sys.stdin.read(4096)
    code = text.strip()
    if "\n" in code or "\r" in code:
        raise ValueError("the code must be one line")
    if not family_code.normalize(code):
        raise ValueError("the code is empty (spaces, dashes and dots do not count)")
    if len(code) > GATE_MAX_CHARS:
        raise ValueError(f"the code is longer than the gate's {GATE_MAX_CHARS}-character box")
    if not code.isprintable():
        raise ValueError("the code has a control character in it")
    return code


def set_family_code(root: Path, code: str) -> str:
    """Write the owner's chosen family code. Returns "set" or "unchanged".
    Refuses a code that equals the admin code (everyone with it would be
    admin). A short code is allowed -- it is the owner's choice -- and the
    caller warns; the door's lockout is what protects it."""
    access_path, admin_path = code_paths(root)
    admin = read_code(admin_path)
    if admin and family_code.normalize(admin) == family_code.normalize(code):
        raise ValueError("that is the admin code; the family code must be different")
    current = read_code(access_path)
    if current is not None and current == code:
        secret_files.tighten([access_path])
        return "unchanged"
    write_code(access_path, code)
    return "set"


def _auth_file(root: Path) -> Path:
    return root / "auth-attempts.json"


def unlock_saved(root: Path, target: str) -> int:
    """Lift lockouts in the saved state directly (for a studio that is not
    running; a running one holds the table in memory and uses the request)."""
    path = _auth_file(root)
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(state, dict):
        return 0
    n = door_lockout.unlock(door_lockout.load_tables(state), target)
    if n:
        secret_files.write_private(path, json.dumps(state))
    return n


def cmd_code_unlock(root: Path, raw: str, wait_s: float | None = None) -> int:
    try:
        target = door_lockout.target_key(raw)
    except ValueError as exc:
        print(f"media-lab code --unlock: {exc}", file=sys.stderr)
        return 2
    who = "every client" if target == "all" else (
        "this machine's own scripts" if target == "anon" else target.removeprefix("ip:"))
    wait_s = UNLOCK_WAIT_S if wait_s is None else wait_s
    req = door_lockout.request_unlock(root, target)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not req.exists():
            print(f"lockouts lifted for {who} (the running studio applied it; see `media-lab logs`)")
            return 0
        _pause(0.25)
    if not req.exists():
        print(f"lockouts lifted for {who} (the running studio applied it; see `media-lab logs`)")
        return 0
    n = unlock_saved(root, target)
    print(f"the studio did not pick the request up within {wait_s:.0f} s (is it running?): "
          f"cleared {n} saved lockout record(s) for {who} directly; the request stays in "
          f"{req.name} and is applied at the next start")
    return 0


def cmd_code_locks(root: Path) -> int:
    try:
        state = json.loads(_auth_file(root).read_text())
    except (OSError, ValueError):
        state = {}
    rows = door_lockout.describe(door_lockout.load_tables(state if isinstance(state, dict) else {}),
                                 time.time())
    if not rows:
        print("no lockouts: the code prompt is open for everyone")
        return 0
    h = door_lockout.human
    for r in rows:
        state_txt = f"LOCKED for {h(r['locked_for'])}" if r["locked_for"] else "open"
        print(f"{r['counter']:5}  {r['client']:<32}  {state_txt:<22}  lockouts so far {r['level']}"
              f"  wrong in the last 10 min {r['recent_wrong']}  next lockout {h(r['next_lock'])}")
    print("(up to 3 s behind the running studio)  lift one: media-lab code --unlock <address>;"
          "  lift all: media-lab code --unlock")
    return 0


def cmd_code(args, cfg, root) -> int:
    """Show, create or rotate the door codes.

    ``--rotate`` replaces the family code (``--admin``: the admin code). The
    running server notices within a second and every device signed in with the
    old code is signed out — each one enters the new code once. ``--quiet``
    never prints a code (for automation and remote shells): it names the file.
    ``--ensure`` creates whichever code files are missing and changes nothing
    that exists (install.sh uses it).

    ``--set-family`` sets a family code the owner chose (read from ``--from
    FILE`` or stdin, never the command line; never printed). ``--locks`` and
    ``--unlock [ADDRESS]`` show and lift the code prompt's lockouts."""
    access_path, admin_path = code_paths(root)
    if args.locks:
        return cmd_code_locks(root)
    if args.unlock is not None:
        return cmd_code_unlock(root, args.unlock)
    if args.set_family:
        try:
            outcome = set_family_code(root, read_chosen_code(args.source))
        except (OSError, ValueError) as exc:
            print(f"media-lab code --set-family: {exc}", file=sys.stderr)
            return 2
        if outcome == "unchanged":
            print(f"the family code in {access_path} is already that code; nothing changed")
            return 0
        print(f"new family code written to {access_path} (mode 0600; not shown)")
        print("The server picks it up within a second: every device signed in with the "
              "old family code is signed out and needs the new one once.", file=sys.stderr)
        if family_code.is_weak(read_code(access_path)):
            print("warning: this family code is short and guessable. The door's lockout is what "
                  "protects it (3 wrong codes in 10 minutes shut the prompt for that network for "
                  "1 h, then 2 h, 4 h, 8 h ...). A longer code is safer: `media-lab code --rotate`.",
                  file=sys.stderr)
        return 0
    if args.ensure:
        made = []
        if secret_files.ensure(access_path, lambda: mint_access_code() + "\n"):
            made.append(("family", access_path))
        if secret_files.ensure(admin_path, lambda: mint_admin_code() + "\n"):
            made.append(("admin", admin_path))
        secret_files.tighten([access_path, admin_path])
        for name, path in made:
            print(f"{name} code created in {path}")
        if not made:
            print(f"codes kept ({access_path.name}, {admin_path.name})")
        return 0
    path = admin_path if args.admin else access_path
    name = "admin" if args.admin else "family"
    if args.rotate:
        new = mint_admin_code() if args.admin else mint_access_code()
        write_code(path, new)
        if args.quiet:
            print(f"new {name} code written to {path} (mode 0600; not shown — "
                  f"`media-lab code{' --admin' if args.admin else ''}` prints it)")
        else:
            print(new)
        print(f"The server picks it up within a second: every device signed in with the "
              f"old {name} code is signed out and needs the new one once.", file=sys.stderr)
        if args.restart:
            return cmd_restart(args, cfg, root)
        return 0
    code = read_code(path)
    if code is None:
        print(f"no code at {path} yet — run ./install.sh or `media-lab code --ensure`", file=sys.stderr)
        return 1
    secret_files.tighten([path])
    if args.quiet:
        print(f"{name} code is in {path}")
    else:
        print(code)
    if family_code.is_weak(code):
        print(f"warning: this {name} code is short and guessable — replace it with "
              f"`media-lab code --rotate{' --admin' if args.admin else ''}`", file=sys.stderr)
    return 0


def cmd_start(args, cfg, root) -> int:
    port, bind = int(cfg.get("port", DEFAULT_PORT)), cfg.get("bind", "0.0.0.0")
    if manifest_up(port, bind):
        print(f"already up at {pairing.server_url(probe_host(bind), port)}")
        return 0
    if not venv_python(cfg).exists():
        print(f"venv missing at {venv_python(cfg)} — run ./install.sh first", file=sys.stderr)
        return 2
    svc = service_state()
    if svc["state"] != "absent" and svc["managed"] and not args.foreground:
        if svc["kind"] == "systemd":
            _run(["systemctl", "--user", "start", svc["name"]])
        else:
            _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        print(f"starting {svc['name']} …")
    elif args.foreground:
        os.chdir(REPO)
        os.execv(str(venv_python(cfg)), uvicorn_command(cfg))
    else:
        pid = start_detached(cfg, root)
        print(f"started pid {pid} (log: {root / LOG_REL}) …")
    if wait_for_manifest(port, bind, args.timeout):
        print(f"up at {pairing.server_url(probe_host(bind), port)}")
        return 0
    print("did not answer /manifest.json in time — see `media-lab logs`", file=sys.stderr)
    return 1


def cmd_stop(args, cfg, root) -> int:
    svc = service_state()
    stopped = False
    if svc["state"] == "running" and svc["managed"]:
        if svc["kind"] == "systemd":
            _run(["systemctl", "--user", "stop", svc["name"]])
        else:
            _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=30)
            # bootout unloads; re-bootstrap so `start` works — but disabled from
            # auto-relaunch until then is exactly what "stop" means on launchd.
        stopped = True
        print(f"stopped {svc['name']}")
    if stop_pid(root):
        stopped = True
        print("stopped the detached process")
    if not stopped:
        if svc["state"] == "running" and not svc["managed"]:
            print(f"{svc['name']} is running but was not installed by install.sh — "
                  "stop it with your own tooling", file=sys.stderr)
            return 1
        print("nothing to stop")
    return 0


def cmd_restart(args, cfg, root) -> int:
    svc = service_state()
    if svc["state"] != "absent" and svc["managed"]:
        if svc["kind"] == "systemd":
            _run(["systemctl", "--user", "restart", svc["name"]], timeout=90)
        else:
            plist = launchd_plist_path()
            domain = f"gui/{os.getuid()}"
            _run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], timeout=30)
            _run(["launchctl", "bootstrap", domain, str(plist)], timeout=30)
            _run(["launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}"], timeout=30)
        print(f"restarting {svc['name']} …")
    else:
        stop_pid(root)
        pid = start_detached(cfg, root)
        print(f"restarted as pid {pid} …")
    port, bind = int(cfg.get("port", DEFAULT_PORT)), cfg.get("bind", "0.0.0.0")
    if wait_for_manifest(port, bind, getattr(args, "timeout", 120)):
        print(f"up at {pairing.server_url(probe_host(bind), port)}")
        return 0
    print("did not answer /manifest.json in time — see `media-lab logs`", file=sys.stderr)
    return 1


def cmd_logs(args, cfg, root) -> int:
    svc = service_state()
    if svc["kind"] == "systemd" and svc["state"] != "absent":
        cmd = ["journalctl", "--user", "-u", svc["name"], "-n", str(args.lines), "--no-pager"]
        if args.follow:
            cmd.append("-f")
        return subprocess.call(cmd)
    log = root / LOG_REL
    if not log.exists():
        print(f"no log yet at {log}", file=sys.stderr)
        return 1
    cmd = ["tail", "-n", str(args.lines)] + (["-f"] if args.follow else []) + [str(log)]
    return subprocess.call(cmd)


def cmd_setup(args, cfg, root) -> int:
    from . import setup_wizard
    rc = setup_wizard.main(args.wizard_args)
    if rc or "--json" in args.wizard_args:
        return rc
    port, bind = int(cfg.get("port", DEFAULT_PORT)), cfg.get("bind", "0.0.0.0")
    best = pairing.pairing_summary(port, pairing.list_ipv4_addresses(), None, None, bind)["best"]["url"]
    print(f"\nThe web wizard (first-run engine shelf, fal.ai key) lives in the app itself:\n"
          f"  {best}/  → it opens on first visit, and any time from the theme sheet → "
          f"\"Set up engines\".")
    return rc


def cmd_uninstall(args, cfg, root) -> int:
    if not args.yes:
        ans = input("Remove the Media Lab service and .venv? Models, media, jobs and codes under "
                    f"{root} are kept. [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted")
            return 1
    remove_service()
    if stop_pid(root):
        print("  stopped the detached process")
    venv = Path(cfg.get("venv") or (REPO / ".venv"))
    if venv.exists() and not args.keep_venv:
        shutil.rmtree(venv, ignore_errors=True)
        print(f"  removed {venv}")
    cp = config_path(root)
    if cp.exists():
        cp.unlink()
        print(f"  removed {cp}")
    print(f"done — {root} (models, media, jobs, codes) was left in place")
    return 0


def cmd_wait(args, cfg, root) -> int:
    port = int(effective(cfg, "port", args.port, DEFAULT_PORT))
    bind = effective(cfg, "bind", args.bind, "0.0.0.0")
    alive = None
    if args.pid:
        def alive():
            try:
                os.kill(args.pid, 0)
                return True
            except OSError:
                return False
    return 0 if wait_for_manifest(port, bind, args.timeout, alive) else 1


def cmd_config(args, cfg, root) -> int:
    """Used by install.sh to record what it chose."""
    new = dict(cfg)
    if args.port is not None:
        new["port"] = int(args.port)
    if args.bind is not None:
        new["bind"] = args.bind
    if args.service is not None:
        new["service"] = args.service == "yes"
    if args.venv is not None:
        new["venv"] = str(Path(args.venv).resolve())
    new["repo"] = str(REPO)
    new["data_root"] = str(root)
    p = save_config(new, root)
    if args.show:
        print(json.dumps(new, indent=2))
    else:
        print(p)
    return 0


def cmd_service(args, cfg, root) -> int:
    if args.action == "install":
        info = install_service(cfg, root)
        print(f"  {info['kind']}: {info['name']} ({info['path']})")
        return 0
    if args.action == "remove":
        remove_service()
        return 0
    print(json.dumps(service_state(), indent=2))
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="media-lab", description=__doc__.split("\n\n")[0])
    p.add_argument("--home", help="data root (default: $MEDIA_LAB_HOME or ~/media-lab-simple)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("studio", help="independent development host: init, pair, serve")
    s.add_argument("studio_args", nargs=argparse.REMAINDER)

    s = sub.add_parser("status", help="health, GPU, engines, service state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("pair", help="print the pairing QR, URLs and family code")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-invert", action="store_true", help="QR for a light terminal theme")
    s.set_defaults(fn=cmd_pair)

    s = sub.add_parser("code", help="show or rotate the family code (--admin: the admin code)")
    s.add_argument("--rotate", action="store_true",
                   help="replace the code; every device that used the old one is signed out")
    s.add_argument("--admin", action="store_true", help="the admin code instead")
    s.add_argument("--set-family", action="store_true",
                   help="set the family code to one you choose, read from --from FILE or "
                        "stdin (never the command line); it is not printed")
    s.add_argument("--from", dest="source", metavar="FILE",
                   help="with --set-family: read the code from FILE ('-' = stdin)")
    s.add_argument("--locks", action="store_true",
                   help="list the clients the code prompt is locked for")
    s.add_argument("--unlock", nargs="?", const="all", metavar="ADDRESS",
                   help="lift code-prompt lockouts: all of them, or one address "
                        "('anon' = this machine's own scripts)")
    s.add_argument("--quiet", action="store_true",
                   help="never print a code; name the file that holds it")
    s.add_argument("--ensure", action="store_true",
                   help="create missing code files (0600); change nothing that exists")
    s.add_argument("--restart", action="store_true",
                   help="also restart the server (not needed: it picks the code up live)")
    s.add_argument("--timeout", type=float, default=120)
    s.set_defaults(fn=cmd_code)

    s = sub.add_parser("start", help="start the server (service if installed)")
    s.add_argument("--foreground", action="store_true", help="run uvicorn in this terminal")
    s.add_argument("--timeout", type=float, default=120)
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("stop", help="stop the server")
    s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("restart", help="restart the server")
    s.add_argument("--timeout", type=float, default=120)
    s.set_defaults(fn=cmd_restart)

    s = sub.add_parser("logs", help="server log")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-n", "--lines", type=int, default=100)
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("setup", add_help=False, help="catalog planner; supports --list, --json and --select")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("uninstall", help="remove service + venv; keep models and media")
    s.add_argument("--yes", "-y", action="store_true")
    s.add_argument("--keep-venv", action="store_true")
    s.set_defaults(fn=cmd_uninstall)

    # internal helpers install.sh calls
    s = sub.add_parser("wait", help=argparse.SUPPRESS)
    s.add_argument("--port", type=int)
    s.add_argument("--bind")
    s.add_argument("--pid", type=int)
    s.add_argument("--timeout", type=float, default=120)
    s.set_defaults(fn=cmd_wait)

    s = sub.add_parser("config", help=argparse.SUPPRESS)
    s.add_argument("--port", type=int)
    s.add_argument("--bind")
    s.add_argument("--service", choices=["yes", "no"])
    s.add_argument("--venv")
    s.add_argument("--show", action="store_true")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("service", help=argparse.SUPPRESS)
    s.add_argument("action", choices=["install", "remove", "state"])
    s.set_defaults(fn=cmd_service)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, forwarded = parser.parse_known_args(argv)
    if args.cmd == "setup":
        args.wizard_args = forwarded[1:] if forwarded[:1] == ["--"] else forwarded
        if "--json" in args.wizard_args:
            return cmd_setup(args, {}, None)
    elif forwarded:
        parser.error("unrecognized arguments: " + " ".join(forwarded))
    if args.cmd == "studio":
        from .studio_cli import main as studio_main
        return studio_main(args.studio_args)
    if args.home:
        os.environ["MEDIA_LAB_HOME"] = args.home
    root = app_root()
    cfg = load_config(root)
    try:
        return int(args.fn(args, cfg, root) or 0)
    except RuntimeError as exc:
        print(f"media-lab: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
