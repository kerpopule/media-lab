#!/usr/bin/env python3
"""YuE2 music engine shim speaking Media Lab's engine contract on 127.0.0.1:$YUE2_PORT.
Modelled on runner/sol_engine_server.py; started by runner/start_yue2_engine.sh as
the transient user unit media-lab-yue2.service (config/media-lab-yue2.service).

GET  /health     -> {ok, engine:"music", impl:"yue2", model:"YuE2-3B", loaded, busy, phase,
                     cache{renders,errors,last_error}, ...}
POST /generate   {style, lyrics, cot: full|melody|off, abc?, seed, max_seconds, cfg_scale?, request_id}
                 -> {ok, file (absolute .flac, 48 kHz stereo), abc, seconds, elapsed, seed,
                     sample_rate, truncated, dir, timing}; a re-request of the same
                     request_id answers from the job directory with cached:true
POST /plan       {style, lyrics, cot?, seed, request_id?} -> {ok, abc, elapsed}
POST /transcribe {audio_path, task: full|melody-full|melody-vocal, max_seconds?} -> {ok, abc, dir, elapsed}
POST /interrupt  -> {ok, interrupted}

Paths come from config/local.env through the start script (YUE2_KIT, YUE2_MODELS_ROOT,
YUE2_PORT, MEDIA_LAB_HOME); nothing here names a machine. Renders land in
$MEDIA_LAB_HOME/pool/music-out/job-<request_id>/.

The pipeline stays resident between requests; the package itself moves the AR model to CPU
while the VAE decodes and back (see yue2/pipeline.py decode()) - we do not add our own swap.
Every render/plan holds the Media Lab inference transaction lock
the inference lock is held by the CALLER (app.py), never here; returns 409 only if
it is already held - so app.py must NOT hold that lock while it calls this engine (it does
for Sol/LTX, whose shims take no lock). Transcription runs in the separate SheetSage2 venv
as a subprocess.

Weights licence: the YuE2 weights are CC BY-NC 4.0 - non-commercial use only.
"""
import os, json, time, threading, re, fcntl, subprocess, traceback, sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTROLLER_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROLLER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_ROOT))

def _env_path(*names, default):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return os.path.expanduser(value)
    return os.path.expanduser(default)

ROOT = _env_path("YUE2_KIT", "YUE2_ROOT", default="~/runtime/yue2-iso")
MODELS = _env_path("YUE2_MODELS_ROOT", "YUE2_MODELS", default="~/.local/share/media-lab-p3-models/yue2")
OUT_DIR = _env_path("YUE2_OUT_DIR",
                    default=os.path.join(_env_path("MEDIA_LAB_HOME", default="~/media-lab-simple"),
                                         "pool", "music-out"))
PORT = int(os.environ.get("YUE2_PORT", "8197"))
BUDGET = float(os.environ.get("YUE2_BUDGET_GIB", "40"))
PRELOAD = os.environ.get("YUE2_PRELOAD", "1") == "1"
LOCK_PATH = os.environ.get("MEDIA_LAB_INFERENCE_LOCK", "/run/user/1000/media-lab-inference.lock")
SHEETSAGE_PY = os.environ.get("SHEETSAGE_PY", f"{ROOT}/.venv-sheetsage/bin/python")
SHEETSAGE_MODEL = os.environ.get("SHEETSAGE_MODEL", f"{MODELS}/SheetSage2")
MERT_MODEL = os.environ.get("MERT_MODEL", f"{MODELS}/MERT-v2-FullSong")  # SheetSage2 parent; offline needs the local dir
SKILL_SCRIPTS = f"{ROOT}/YuE/skills/yue2-music/scripts"
TOKENS_PER_SECOND = 25  # 48000 Hz / 1920 VAE downsampling = one semantic token per latent frame
MAX_TOKENS_DEFAULT = 9000
os.environ.setdefault("HF_HUB_OFFLINE", "1"); os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.makedirs(OUT_DIR, exist_ok=True)

STATE = {"pipe": None, "loaded": False, "busy": False, "phase": None, "started": time.time(),
         "renders": 0, "errors": 0, "last_error": None, "cancel": False, "load_seconds": None, "current": None}
LOCK = threading.Lock()   # in-process serialisation
def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

def ensure_pipeline():
    if STATE["pipe"] is not None: return STATE["pipe"]
    from yue2 import YuE2Pipeline
    t0 = time.time(); STATE["phase"] = "loading"
    p = YuE2Pipeline.from_pretrained(f"{MODELS}/YuE2-3B", vae=f"{MODELS}/YuE2-Vae", device="cuda",
                                     memory_budget_gib=BUDGET, local_files_only=True, progress=False)
    STATE.update(pipe=p, loaded=True, load_seconds=round(time.time()-t0, 1), phase=None)
    log(f"pipeline ready in {STATE['load_seconds']}s"); return p

class Gate:
    """flock on Media Lab's inference transaction lock; non-blocking."""
    def __init__(self): self.f = None
    def __enter__(self):
        self.f = open(LOCK_PATH, "a+")
        try: fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close(); self.f = None; raise
        return self
    def __exit__(self, *e):
        if self.f:
            try: fcntl.flock(self.f, fcntl.LOCK_UN)
            finally: self.f.close()

def cancelled(): return STATE["cancel"]

def req_kwargs(req):
    style = (req.get("style") or "").strip(); lyrics = req.get("lyrics")
    if lyrics is None: lyrics = "[Instrumental]"
    cot = req.get("cot") or "full"
    if cot not in ("full", "melody", "off"): raise ValueError("cot must be full|melody|off")
    kw = {"style": style, "lyrics": lyrics, "cot": cot, "seed": int(req.get("seed") or 0)}
    if req.get("abc"): kw["abc"] = req["abc"]
    if req.get("cfg_scale") is not None: kw["cfg_scale"] = float(req["cfg_scale"])
    ms = req.get("max_seconds")
    if ms:
        mt = max(8 * TOKENS_PER_SECOND, min(MAX_TOKENS_DEFAULT, int(float(ms) * TOKENS_PER_SECOND)))
        kw["semantic_sampling"] = {"max_tokens": mt, "min_tokens": min(200, mt)}
    return kw

def valid_rid(rid): return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,128}", rid))

def do_generate(req):
    rid = str(req.get("request_id") or f"r{int(time.time())}")
    if not valid_rid(rid): raise ValueError("bad request_id")
    kw = req_kwargs(req)
    if not kw["style"]: raise ValueError("style required")
    out_dir = f"{OUT_DIR}/job-{rid}"; out = f"{out_dir}/audio.flac"
    if os.path.exists(out):
        meta = json.load(open(f"{out_dir}/result.json")) if os.path.exists(f"{out_dir}/result.json") else {}
        abc = open(f"{out_dir}/score.abc").read() if os.path.exists(f"{out_dir}/score.abc") else None
        return {"ok": True, "file": out, "abc": abc, "seconds": meta.get("audio_seconds"), "elapsed": 0,
                "seed": kw["seed"], "cached": True, "sample_rate": meta.get("sample_rate")}
    p = ensure_pipeline(); t0 = time.time(); STATE["phase"] = "generate"; STATE["current"] = rid
    kw["id"] = rid[:180]
    result = p(cancelled=cancelled, **kw)
    os.makedirs(out_dir, exist_ok=True); summary = result.save_artifacts(out_dir)
    STATE["renders"] += 1
    return {"ok": True, "file": out, "abc": result.abc, "seconds": round(summary["audio_seconds"], 2),
            "elapsed": round(time.time()-t0, 1), "seed": kw["seed"], "cached": False,
            "sample_rate": summary["sample_rate"], "truncated": summary["truncated"], "dir": out_dir,
            "timing": {k: v for k, v in result.timing.items() if k in ("nar_seconds", "vae_seconds", "e2e_seconds")}}

def do_plan(req):
    kw = req_kwargs(req); kw.pop("semantic_sampling", None)
    if not kw["style"]: raise ValueError("style required")
    if kw["cot"] == "off": raise ValueError("plan needs cot=full|melody")
    p = ensure_pipeline(); t0 = time.time(); STATE["phase"] = "plan"
    plan = p.plan(cancelled=cancelled, **kw)
    return {"ok": True, "abc": plan.abc, "truncated": plan.truncated, "elapsed": round(time.time()-t0, 1), "seed": kw["seed"]}

def do_transcribe(req):
    audio = req.get("audio_path") or ""
    if not os.path.isabs(audio) or not os.path.isfile(audio): raise ValueError("audio_path must be an existing absolute path")
    task = req.get("task") or "melody-full"
    if task not in ("full", "melody-full", "melody-vocal"): raise ValueError("task must be full|melody-full|melody-vocal")
    rid = str(req.get("request_id") or f"t{int(time.time())}")
    if not valid_rid(rid): raise ValueError("bad request_id")
    out = f"{OUT_DIR}/transcribe-{rid}"
    cmd = [SHEETSAGE_PY, f"{SKILL_SCRIPTS}/transcribe.py", audio, "--task", task, "--output", out,
           "--model", SHEETSAGE_MODEL, "--base-model", MERT_MODEL, "--offline"]
    if req.get("max_seconds"): cmd += ["--max-seconds", str(int(req["max_seconds"]))]
    t0 = time.time(); STATE["phase"] = "transcribe"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=int(req.get("timeout") or 1800))
    if r.returncode != 0: raise RuntimeError(f"sheetsage rc={r.returncode}: {r.stderr[-600:]}")
    abc = open(f"{out}/score.abc").read()
    return {"ok": True, "abc": abc, "dir": out, "elapsed": round(time.time()-t0, 1)}

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"ok": True, "engine": "music", "impl": "yue2", "model": "YuE2-3B", "vae": "YuE2-Vae",
                "loaded": STATE["loaded"], "busy": STATE["busy"], "phase": STATE["phase"], "current": STATE["current"],
                "port": PORT, "out_dir": OUT_DIR, "budget_gib": BUDGET, "load_seconds": STATE["load_seconds"],
                "cache": {"renders": STATE["renders"], "errors": STATE["errors"], "last_error": STATE["last_error"]},
                "uptime": int(time.time()-STATE["started"])})
        self._send(404, {"ok": False, "error": "not found"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try: req = json.loads(self.rfile.read(n) or b"{}")
        except Exception: return self._send(400, {"ok": False, "error": "bad json"})
        if self.path.startswith("/interrupt"):
            STATE["cancel"] = True; return self._send(200, {"ok": True, "interrupted": STATE["busy"]})
        fn = {"/generate": do_generate, "/plan": do_plan, "/transcribe": do_transcribe}.get(self.path.split("?")[0])
        if fn is None: return self._send(404, {"ok": False, "error": "not found"})
        from media_lab_core.gpu_lease_runtime import authorize_values, open_protocol
        task = self.path.split("?", 1)[0].lstrip("/")
        if not authorize_values(open_protocol(), dict(self.headers.items()), engine="yue2", task=task):
            return self._send(403, {"ok": False, "error": "exact GPU lease delegation required"})
        if not LOCK.acquire(blocking=False): return self._send(409, {"ok": False, "error": "busy"})
        STATE["busy"] = True; STATE["cancel"] = False
        try:
            # The app holds /run/user/1000/media-lab-inference.lock around this call
            # (same contract as the Sol/LTX engines). Taking it here too would deadlock
            # against the caller, so the shim serialises only its own requests (LOCK).
            log(self.path, json.dumps({k: v for k, v in req.items() if k not in ("lyrics", "abc")})[:300])
            res = fn(req); log("done", self.path, res.get("elapsed"), res.get("seconds")); self._send(200, res)
        except InterruptedError as e:
            STATE["last_error"] = "interrupted"; self._send(499, {"ok": False, "error": f"interrupted: {e}"})
        except ValueError as e:
            self._send(400, {"ok": False, "error": f"bad request: {e}"})
        except Exception as e:
            STATE["errors"] += 1; STATE["last_error"] = str(e)[:300]; log("ERROR", traceback.format_exc())
            self._send(500, {"ok": False, "error": str(e)[:500]})
        finally:
            STATE["busy"] = False; STATE["phase"] = None; STATE["current"] = None; LOCK.release()

if __name__ == "__main__":
    log(f"yue2 engine server :{PORT} models={MODELS} out={OUT_DIR} budget={BUDGET}GiB")
    if PRELOAD:
        from media_lab_core.gpu_lease_runtime import ENV_TASK, authorize_environment, open_protocol
        delegated_task = os.environ.get(ENV_TASK, "")
        if delegated_task in {"generate", "plan", "transcribe"} and authorize_environment(
                open_protocol(), engine="yue2", task=delegated_task, phases=("load",)):
            threading.Thread(target=ensure_pipeline, daemon=True).start()
        else:
            log("preload denied: exact load/render delegation is absent")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
