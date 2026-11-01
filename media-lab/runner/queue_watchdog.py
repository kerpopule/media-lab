#!/usr/bin/env python3
"""Media Lab queue watchdog — detects a stalled queue and self-heals.

Run by media-lab-queue-watchdog.timer every 2 minutes. Stdlib only.

What it protects against (each one has actually happened):
  A. Worker thread dead/blocked: jobs queued, nothing running, for 2+ checks.
     -> restart media-lab-simple.service (safe: nothing is running).
     (2026-08-16: a hung Apple web-push with no timeout blocked the worker;
      16 jobs sat "queued" until a human noticed.)
  B. A running job wedged: same job id + stage unchanged for > STALL_RUNNING_MIN.
     -> restart the app; the app marks the job "studio restarted mid-take — hit
     Retry". The interruption is logged loudly.
  C. App unreachable while the service claims active -> restart.
  D. GPU pool lock not held after a reboot -> pool_lock.sh acquire (idempotent).

State between runs lives in watchdog-state.json next to this script's data dir.
Never restarts more often than RESTART_COOLDOWN_MIN.

This watchdog is the ONE owner of studio restarts (2026-09-26): the service
supervisor no longer restarts media-lab-simple, so two watchers can never both
"repair" it. Limits, because on 2026-09-02 a probe failure that was not the
studio's fault caused 189 restarts in 15.5 hours:
  * "unreachable" needs UNREACHABLE_STRIKES consecutive failed probes;
  * at most MAX_RESTARTS_PER_HOUR restarts in any hour, then it stops, sets
    restart_capped in its state (the health check raises it) and waits;
  * a queue still stalled after one restart is marked stuck for a person
    (/api/health says "stuck"); it is never restarted a second time for it.
A restart is a normal systemctl restart: with MEDIA_LAB_GRACEFUL_HANDOFF=1 an
idle warm engine is handed to the new process instead of leaving a hold.
"""
import json, os, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from media_lab_core import local_config   # config/local.env, stdlib only
from media_lab_core import local_token    # local-token.txt, stdlib only

HOME = os.path.expanduser("~")
ROOT = str(local_config.home())
STATE = os.path.join(ROOT, "watchdog-state.json")
QUEUE_URL = local_config.studio_url() + "/api/queue"
# First public hostname, if the studio is published (MEDIA_LAB_PUBLIC_HOSTS).
_PUBLIC = sorted(local_config.public_hosts())
# The public-edge probe asks for a path the family door lets through without a
# pass (/manifest.json), so it never adds a 401 to the studio's log.
TUNNEL_URL = f"https://{_PUBLIC[0]}/manifest.json" if _PUBLIC else ""
TUNNEL_METRICS_URL = "http://127.0.0.1:20241/metrics"
TUNNEL_PROBE_UA = "MediaLabWatchdog/1.0"
TUNNEL_ORIGIN_FAILURE_CODES = (0, 502, 503, 504, 521, 522, 523, 530)
POOL_LOCK = "/run/user/1000/spark-gpu.lock"
POOL_SH = os.path.join(ROOT, "runner", "pool_lock.sh")
GPU_RECOVERY_HOLD = Path(ROOT) / "pool" / "gpu-recovery-hold.json"

STALL_IDLE_CHECKS = 2        # consecutive checks with queued>0, running==0
STALL_RUNNING_MIN = 45       # minutes a running job may sit on one stage
# ...unless the engine says it is mid-generation. H3 at its TRAINED canvas
# (1344x768) packs 2.2x the tokens of the old 864x480 preview preset and a take
# legitimately runs 45-70 min. Killing that is worse than waiting: judge by the
# engine's own busy flag, and only fall back to the clock if it cannot answer.
ENGINE_HEALTH = ("http://127.0.0.1:8290/health", "http://127.0.0.1:8291/health")
STALL_HARD_MIN = 120
RESTART_COOLDOWN_MIN = 10
UNREACHABLE_STRIKES = 3
MAX_RESTARTS_PER_HOUR = 3
LEASE_PROBE = os.path.join(ROOT, "runner", "lease_owner_probe.py")

ENV = dict(os.environ,
           XDG_RUNTIME_DIR="/run/user/1000",
           DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus")
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print(f"[queue-watchdog] {msg}", flush=True)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def restart_app(st, reason, now=None):
    """Restart the studio, within the cooldown and the hourly cap. True if done."""
    now = time.time() if now is None else now
    last = st.get("last_restart", 0)
    if now - last < RESTART_COOLDOWN_MIN * 60:
        log(f"WOULD restart ({reason}) but cooldown active — skipping")
        return False
    recent = [t for t in st.get("restarts", []) if now - float(t) < 3600]
    if len(recent) >= MAX_RESTARTS_PER_HOUR:
        if not st.get("restart_capped"):
            log(f"RESTART CAP: {len(recent)} restarts in the last hour — not restarting "
                f"({reason}); a person must look")
        st["restart_capped"] = True
        st["restarts"] = recent
        return False
    log(f"RESTARTING media-lab-simple.service — {reason}")
    subprocess.run(["systemctl", "--user", "restart", "media-lab-simple.service"],
                   env=ENV, check=False)
    st["last_restart"] = now
    st["restarts"] = recent + [now]
    st["restart_capped"] = False
    st["idle_strikes"] = 0
    st.pop("run_seen", None)
    return True


def controller_holds_gpu_lock():
    """Read-only: does our own live studio controller hold the canonical lock?

    Since the controller owns the lock itself for every fenced GPU operation
    (and while a warm engine is parked), neither legacy holder unit is active in
    the normal warm state. Asking pool_lock.sh then logged "pool holder was
    missing — re-acquired: OK" every run (~720 misleading lines a day)."""
    if not os.path.exists(LEASE_PROBE):
        return False
    r = subprocess.run(["/usr/bin/python3", LEASE_PROBE, "--lock", POOL_LOCK], env=ENV,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def ensure_pool_lock():
    # pool_lock.sh acquire is idempotent: OK / BUSY(62) / FAIL(63).
    # BUSY means someone (possibly an outside production) holds it — that is fine.
    if not os.path.exists(POOL_SH):
        return
    for unit in ("media-lab-pool.service", "media-lab-gpu-reservation.service"):
        up = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], env=ENV)
        if up.returncode == 0:
            return                  # warm pool OR intended idle reservation is healthy
    if controller_holds_gpu_lock():
        return                      # the studio itself owns the GPU: nothing is missing
    r = subprocess.run(["bash", POOL_SH, "acquire"], env=ENV,
                       capture_output=True, text=True)
    if r.returncode == 0:
        log(f"pool holder was missing — re-acquired: {(r.stdout or '').strip() or 'OK'}")
    elif r.returncode == 63:
        log(f"pool lock acquire FAILED: {r.stdout} {r.stderr}")


def gpu_recovery_held():
    """A durable recovery hold explains a queued/no-running row.

    The worker deliberately leaves the row queued until an exact reconciliation.
    Restarting the controller cannot clear that fence and only disrupts a load or
    destroys incident chronology.
    """
    return GPU_RECOVERY_HOLD.is_file()


def an_engine_is_working():
    """True when a video engine reports busy — real evidence of progress."""
    for url in ENGINE_HEALTH:
        try:
            with LOCAL_OPENER.open(url, timeout=5) as r:
                if json.load(r).get("busy"):
                    return True
        except Exception:
            continue
    return False


def queue_request():
    """Build the authenticated local queue probe without DNS or proxy routing.

    The studio no longer trusts a "Host: localhost" header (any client could send
    one). The watchdog proves it runs on this machine the only unfakeable way: it
    reads local-token.txt (0600, written by the app at start) and sends it."""
    return local_token.authorize(urllib.request.Request(QUEUE_URL))


def api_refused(exc):
    """The API answered but refused the probe (401/403): the app is ALIVE. That
    is a credentials problem — a missing or rotated local token — never a reason
    to restart the studio and kill a render."""
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403)


def public_tunnel_code():
    """Return the edge status without Cloudflare blocking Python's default UA.

    Cloudflare error 1010 returns HTTP 403 to Python's stock urllib user-agent.
    Treating every 403 as proof of a healthy connector masked a real 1033/530
    outage on 2026-08-21, so connector metrics are checked independently below.
    """
    try:
        req = urllib.request.Request(
            TUNNEL_URL,
            headers={"User-Agent": TUNNEL_PROBE_UA, "Accept": "text/html"},
            method="GET",
        )
        return urllib.request.urlopen(req, timeout=15).getcode()
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def tunnel_ha_connections():
    """Return cloudflared's active connector count, or None if unobservable."""
    try:
        with LOCAL_OPENER.open(TUNNEL_METRICS_URL, timeout=5) as resp:
            text = resp.read().decode("utf-8", "replace")
        prefix = "cloudflared_tunnel_ha_connections "
        for line in text.splitlines():
            if line.startswith(prefix):
                return int(float(line[len(prefix):].strip()))
    except Exception:
        pass
    return None


def maestro_queue_runner_active():
    """True while the API owns an in-container Maestro process.

    A failed health probe must never restart the API around this process: doing
    so orphans or kills an otherwise healthy render. Once the runner exits, the
    normal next watchdog pass may repair a genuinely dead API.
    """
    result = subprocess.run(
        ["pgrep", "-f", r"docker exec .*media-lab-maestro-runner\.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def check_tunnel(st):
    """Restart a connectorless or origin-unreachable public tunnel after 2 hits.

    Public status alone is ambiguous because Cloudflare can reject an automated
    probe before it reaches the tunnel. The local HA metric is authoritative for
    connector residency; the public response verifies the edge-to-origin path.
    """
    code = public_tunnel_code()
    ha = tunnel_ha_connections()
    unhealthy = code in TUNNEL_ORIGIN_FAILURE_CODES or ha == 0
    if not unhealthy:
        st["tunnel_strikes"] = 0
        return
    st["tunnel_strikes"] = st.get("tunnel_strikes", 0) + 1
    log(f"public tunnel unhealthy (HTTP {code}, HA connectors={ha}) — "
        f"strike {st['tunnel_strikes']}/2")
    if st["tunnel_strikes"] >= 2:
        log("RESTARTING media-lab-tunnel.service")
        subprocess.run(["systemctl", "--user", "restart", "media-lab-tunnel.service"],
                       env=ENV, check=False)
        st["tunnel_strikes"] = 0

def main():
    st = load_state()

    # --- reach the app ---
    try:
        with LOCAL_OPENER.open(queue_request(), timeout=15) as resp:
            d = json.load(resp)
    except Exception as e:
        if api_refused(e):
            st["unreachable_strikes"] = 0
            st["idle_strikes"] = 0
            log(f"/api/queue answered {e.code}: the studio is up but refused the watchdog's "
                f"local token ({local_token.token_path()}) — standing clear, no restart")
            save_state(st)
            return
        if gpu_recovery_held():
            st["idle_strikes"] = 0
            st.pop("run_seen", None)
            log(f"/api/queue unreachable ({e}) but durable GPU recovery is held — standing clear")
            save_state(st)
            return
        active_state = subprocess.run(
            ["systemctl", "--user", "is-active", "media-lab-simple.service"],
            env=ENV, capture_output=True, text=True).stdout.strip()
        log(f"/api/queue unreachable ({e}); service is '{active_state}'")
        if active_state == "active" and maestro_queue_runner_active():
            log("API probe failed while a queue-owned Maestro runner is active — standing clear")
        elif active_state == "active":
            st["unreachable_strikes"] = st.get("unreachable_strikes", 0) + 1
            if st["unreachable_strikes"] >= UNREACHABLE_STRIKES:
                if restart_app(st, f"service active but API unreachable for "
                                   f"{st['unreachable_strikes']} checks"):
                    st["unreachable_strikes"] = 0
            else:
                log(f"unreachable strike {st['unreachable_strikes']}/{UNREACHABLE_STRIKES} — waiting")
        else:
            subprocess.run(["systemctl", "--user", "restart",
                            "media-lab-simple.service"], env=ENV, check=False)
            st["last_restart"] = time.time()
            log("service was not active — started it")
        save_state(st)
        return

    st["unreachable_strikes"] = 0
    active = d.get("active", [])
    running = [j for j in active if j.get("status") == "running"]
    queued = [j for j in active if j.get("status") == "queued"]

    # A durable recovery hold means the prior GPU ownership outcome is unknown.
    # No watchdog path may restart the controller until an operator reconciles
    # that evidence, including API-failure and stalled-running paths.
    if gpu_recovery_held():
        st["idle_strikes"] = 0
        st.pop("run_seen", None)
        log(f"queued={len(queued)} running={len(running)} but durable GPU recovery is held — standing clear")
        save_state(st)
        return

    # --- case D: make sure the GPU pool lock has a holder (post-reboot gap) ---
    ensure_pool_lock()

    # --- case E: the public tunnel can die while the app is healthy ---
    check_tunnel(st)

    # --- case A: queued work but the worker is doing nothing ---
    if queued and not running:
        st["idle_strikes"] = st.get("idle_strikes", 0) + 1
        log(f"queued={len(queued)} running=0 — idle strike "
            f"{st['idle_strikes']}/{STALL_IDLE_CHECKS}")
        if st.get("stuck"):
            log("queue is still stuck after one restart — left for a person (/api/health: stuck)")
        elif st["idle_strikes"] >= STALL_IDLE_CHECKS:
            if st.get("stall_restarted"):
                # One restart did not help: a second one would not either, and
                # each one costs a warm engine. Hand it to a person.
                st["stuck"] = True
                log("QUEUE STUCK: still stalled after a restart — not restarting again")
            elif restart_app(st, f"worker stalled: {len(queued)} queued, none running "
                                 f"for {st['idle_strikes']} checks"):
                st["stall_restarted"] = True
    else:
        st["idle_strikes"] = 0
        st.pop("stall_restarted", None)
        st.pop("stuck", None)

    # --- case B: a running job frozen on one stage too long ---
    if running:
        j = running[0]
        sig = f"{j.get('id')}:{j.get('stage')}"
        seen = st.get("run_seen")
        if seen and seen.get("sig") == sig:
            mins = (time.time() - seen["since"]) / 60
            if mins > STALL_RUNNING_MIN and an_engine_is_working():
                log(f"job {j.get('id')} has been on '{j.get('stage')}' for {mins:.0f} min, "
                    f"but an engine reports busy — it is rendering, not wedged")
            elif mins > STALL_RUNNING_MIN:
                log(f"job {j.get('id')} ({j.get('kind')}) stuck on stage "
                    f"'{j.get('stage')}' for {mins:.0f} min — treating as wedged. "
                    f"INTERRUPTING IT: the app will mark it for Retry.")
                restart_app(st, f"wedged running job {j.get('id')}")
        else:
            st["run_seen"] = {"sig": sig, "since": time.time()}
    else:
        st.pop("run_seen", None)

    save_state(st)


if __name__ == "__main__":
    main()
