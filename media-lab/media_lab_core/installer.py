"""Background install orchestrator for the first-run engine shelf.

The design contract (docs/ONBOARDING.md, "Build order #3"): each engine
installs on its own background thread, progress is persisted so a page can
poll it, and an engine flipping to ready fires a callback (the app wires
webpush into it). Steps come from a data file — config/engine-installs.json —
never from code, so keeping installs honest is an edit, not a release.

This module deliberately reuses the fail-closed philosophy of
engine_registry/model_catalog: a step either succeeds, or the engine is
marked failed with the exact command tail that broke — nothing is guessed,
no download URL is invented. Engines the repo has no automated path for are
marked ``requires_manual`` in the data file and are never started here.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

STATE_NAME = "setup-state.json"
LOG_TAIL_CHARS = 600
STEP_TIMEOUT_S = 1800

_state_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}


def load_install_config(paths) -> dict:
    """First readable engine-installs.json wins; {} when none exists."""
    for candidate in paths:
        p = Path(candidate)
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                return {k: v for k, v in data.items()
                        if isinstance(v, dict) and not k.startswith("_")}
    return {}


def _state_file(root: Path) -> Path:
    return Path(root) / STATE_NAME


def load_state(root: Path) -> dict:
    try:
        data = json.loads(_state_file(root).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _update_state(root: Path, engine: str, **fields) -> dict:
    """Atomic read-modify-write of one engine's install record."""
    with _state_lock:
        state = load_state(root)
        rec = state.get(engine)
        if not isinstance(rec, dict):
            rec = {}
        rec.update(fields)
        state[engine] = rec
        f = _state_file(root)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(f)
        return rec


def install_running(engine: str) -> bool:
    t = _threads.get(engine)
    return bool(t and t.is_alive())


def engine_install_state(root: Path, engine: str) -> dict | None:
    """The persisted record, downgraded honestly when the process restarted:
    a record that says "installing" with no live thread is an interrupted
    install, not a running one."""
    rec = load_state(root).get(engine)
    if not isinstance(rec, dict):
        return None
    if rec.get("state") == "installing" and not install_running(engine):
        rec = _update_state(root, engine, state="failed",
                            detail="install was interrupted (server restarted) — run it again",
                            finished=time.time())
    return rec


def _health_url(spec: dict) -> str:
    port = spec.get("port")
    if not port:
        return ""
    return f"http://127.0.0.1:{int(port)}{spec.get('health', '/health')}"


def _health_up(spec: dict) -> bool:
    url = _health_url(spec)
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _run_install(engine: str, spec: dict, root: Path, base_dir: Path, on_ready) -> None:
    log_tail = ""
    try:
        for step in spec.get("steps", []):
            label = str(step.get("label") or step.get("run") or "step")
            _update_state(root, engine, state="installing", step=label,
                          detail=f"{label}…", log_tail=log_tail)
            r = subprocess.run(["bash", "-c", str(step.get("run") or "true")],
                               cwd=str(base_dir), capture_output=True, text=True,
                               timeout=STEP_TIMEOUT_S)
            out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
            if out:
                log_tail = out[-LOG_TAIL_CHARS:]
            if r.returncode != 0:
                _update_state(root, engine, state="failed", step=label,
                              detail=f"failed at: {label}",
                              log_tail=log_tail, finished=time.time())
                return
        # steps done — wait for the engine's health endpoint (when it has one)
        if _health_url(spec):
            deadline = time.time() + int(spec.get("boot_wait", 300))
            _update_state(root, engine, state="installing", step="warming up",
                          detail="engine started — waiting for it to answer…",
                          log_tail=log_tail)
            while time.time() < deadline:
                if _health_up(spec):
                    break
                time.sleep(5)
            else:
                _update_state(root, engine, state="failed", step="warming up",
                              detail="engine never answered its health check",
                              log_tail=log_tail, finished=time.time())
                return
        _update_state(root, engine, state="ready", step="done",
                      detail="ready", log_tail=log_tail, finished=time.time())
        if on_ready:
            try:
                on_ready(engine, spec)
                _update_state(root, engine, notified=True)
            except Exception:
                pass
    except subprocess.TimeoutExpired:
        _update_state(root, engine, state="failed",
                      detail=f"a step ran longer than {STEP_TIMEOUT_S // 60} minutes",
                      log_tail=log_tail, finished=time.time())
    except Exception as exc:  # noqa: BLE001 — a crashed thread must leave a receipt
        _update_state(root, engine, state="failed", detail=str(exc)[:300],
                      log_tail=log_tail, finished=time.time())


def start_install(engine: str, spec: dict, root: Path, base_dir: Path, on_ready=None) -> bool:
    """Kick one engine's install on a daemon thread. False when one is
    already running or the data file says this engine needs a human."""
    if spec.get("blocked") or spec.get("requires_manual"):
        return False
    if install_running(engine):
        return False
    _update_state(root, engine, state="installing", step="starting",
                  detail="starting…", started=time.time(), finished=None,
                  log_tail="", notified=False)
    t = threading.Thread(target=_run_install, args=(engine, spec, root, base_dir, on_ready),
                         daemon=True, name=f"install-{engine}")
    _threads[engine] = t
    t.start()
    return True
