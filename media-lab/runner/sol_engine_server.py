#!/usr/bin/env python3
"""Sol-H3-Spark wrapper speaking Media Lab's H3 engine contract on 127.0.0.1:8291.
GET /health -> {ok, engine:"h3", loaded, busy, variant, turbo_preset, fused_combined, attention, cache}
POST /generate -> {ok, file, seed, elapsed, cached}; writes <OUT_DIR>/job-<request_id>.mp4
Task mapping: references/video_references -> ref2va; start_image_b64 -> fl2va (first frame); else t2va.
Output is always 1344x768, 121 frames, 24 fps (Sol-H3-Spark frozen geometry); frames/width/height are ignored.
One resident Pipeline per task family; switching task reloads (minutes).

H3_VARIANT=singularity runs the "Real / Long" engine instead (runner/h3_singularity.py):
one task family ("singularity"), text plus up to 9 pictures / 3 videos / 3 audio
references, portrait/landscape/square, 5-15 s; frames and orientation are honoured.
Its renderer is a child process of this server, inside the same unit and cgroup."""
import os, sys, json, base64, time, threading, re, shutil, traceback, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from media_lab_core.gpu_lease_runtime import authorize_environment, authorize_values, open_protocol
from media_lab_core import local_config   # config/local.env, stdlib only
PKG = os.environ["SOL_PKG"]; SOL_ROOT = os.environ["SOL_ROOT"]; RUNTIME = os.environ["SOL_H3_SPARK_RUNTIME_ROOT"]
OUT_DIR = os.environ.get("SOL_OUT_DIR", str(local_config.home() / "pool/h3-out"))
VARIANT = os.environ.get("H3_VARIANT", "fl2va")
TURBO = os.environ.get("H3_TURBO_PRESET") or None
PORT = int(os.environ.get("SOL_PORT", "8291"))
SINGULARITY = VARIANT == "singularity"
sys.path.insert(0, PKG)
if SINGULARITY:
    import h3_singularity as singularity
    from media_lab_core import av_sync
else:
    from runtime.config import load_paths
    from runtime.pipeline import Pipeline
for d in (OUT_DIR, f"{RUNTIME}/inputs", f"{RUNTIME}/outputs"): os.makedirs(d, exist_ok=True)
STATE = {"pipe": None, "task": None, "busy": False, "loaded": False, "loading": False,
         "started": time.time(), "renders": 0, "errors": 0, "last_error": None}
GPU_PROTOCOL = open_protocol()
# Preload, task switching and generation share one reentrant critical section.
# A request already owns LOCK when it calls ensure_pipeline().
LOCK = threading.RLock()
def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)
def stage1_geometry():
    """'patched' when tools/sol-h3-runtime-patch.py is installed in SOL_PKG (a new
    prompt no longer adds compiled-region cache entries), else 'stock'."""
    return "patched" if (Path(PKG) / "runtime/stage1_ops/geometry.py").is_file() else "stock"
def _png(path, size, color):
    from PIL import Image
    Image.new("RGB", size, color).save(path); return path
def warm_case(task):
    if task == "fl2va":
        fp = _png(f"{RUNTIME}/inputs/warm-first.png", (1344, 768), (32, 32, 32))
        return {"case_id": "warm", "task": task, "seed": 1, "first_frame": fp, "prompt": "integrated_multimodal_description: A calm dark scene. overall_soundscape: silence."}
    if task == "ref2va":
        fp = _png(f"{RUNTIME}/inputs/warm-ref.png", (672, 384), (96, 96, 96))
        return {"case_id": "warm", "task": task, "seed": 1, "references": [{"type": "image", "path": fp}], "prompt": "subject_definitions: <Subject 1> is the shape in <Picture 1>. detailed_description: <Subject 1> stays still. overall_soundscape: silence."}
    return {"case_id": "warm", "task": "t2va", "seed": 1, "prompt": "A calm wide shot of a quiet meadow at dawn. overall_soundscape: soft wind."}
def boot_cleared():
    """Clearance is tied to one Linux boot. Since 2026-09-27 (owner decision) it is
    written after a reboot by runner/hold_autorecover.py once its evidence checks pass
    (or by an operator with spark1-clear-h3); this server only honours it."""
    try:
        boot = Path(os.environ.get('SOL_BOOT_ID_PATH',
                    '/proc/sys/kernel/random/boot_id')).read_text().strip()
        if str(uuid.UUID(boot)) != boot:
            return False
        permit = json.loads((Path(SOL_ROOT) / 'boot-clearance.json').read_text())
        return isinstance(permit, dict) and permit.get('approved') is True and permit.get('boot_id') == boot
    except (OSError, ValueError, TypeError):
        return False

def safety_latched():
    # Legacy watchdog marker is intentionally honored until explicit recovery.
    # The persistent marker also survives controller/service restarts and reboot.
    return (not boot_cleared()
            or STATE.get("blocked", False)
            or (Path(SOL_ROOT) / "safety-stop.json").exists()
            or (Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
                / "flashnext-memwatch.latch").exists())

def trip_safety(reason, error):
    STATE.update(blocked=True, loaded=False)
    record = {"reason": reason, "error_type": type(error).__name__,
              "pid": os.getpid(), "task": STATE.get("task"), "time": time.time()}
    log("SAFETY_STOP", json.dumps(record, sort_keys=True))
    target = Path(SOL_ROOT) / "safety-stop.json"
    temporary = target.with_name(f".safety-stop-{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(record, out); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, target)
        fd = os.open(target.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError as e:
        # Remain blocked in memory even when the disk itself has failed.
        log("SAFETY_STOP persistence failed", type(e).__name__)
    finally:
        temporary.unlink(missing_ok=True)

def close_pipeline(pipe):
    if SINGULARITY:
        pipe.close()  # raises if the renderer's process group survives
        return
    workers = [getattr(pipe, name, None) for name in ("qwen", "stage1", "stage2")]
    processes = [w.process for w in workers if w is not None and w.process is not None]
    pipe.close()
    # Pipeline.close() can return with a surviving process. Never load over it.
    for process in processes:
        if process.poll() is None:
            raise RuntimeError("pipeline worker survived close")
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError("pipeline process group survived close")

def ensure_pipeline(task):
    with LOCK:
        if safety_latched():
            raise RuntimeError("H3 safety stop latched; operator inspection required")
        STATE["loading"] = True
        try:
            return _ensure_pipeline(task)
        except Exception as e:
            # A missing or misconfigured Real / Long runtime fails before any GPU
            # work; it is a configuration fault, not a box-safety event.
            if not (SINGULARITY and isinstance(e, singularity.SingularityConfigError)):
                trip_safety("pipeline_transition_failed", e)
            raise
        finally:
            STATE["loading"] = False

def _ensure_singularity(task):
    if task != singularity.TASK:
        raise RuntimeError(f"the Real / Long engine serves only task {singularity.TASK!r}, not {task!r}")
    if STATE["pipe"] is not None:
        return STATE["pipe"]
    p = singularity.SingularityPipeline(os.environ, RUNTIME, log=log, media_dir=OUT_DIR); t0 = time.time()
    p.start()
    log(f"pipeline {task} ready in {time.time()-t0:.0f}s")
    STATE.update(pipe=p, task=task, loaded=True, outdir=str(p.comfy.dirs["out"])); return p

def _ensure_pipeline(task):
    if SINGULARITY:
        return _ensure_singularity(task)
    if STATE["pipe"] is not None and STATE["task"] == task: return STATE["pipe"]
    if STATE["pipe"] is not None:
        log("switching task", STATE["task"], "->", task, "(reload)")
        try:
            STATE["pipe"].finish()
        except Exception as e:
            # Reporting an incomplete batch must never skip resource cleanup.
            log("finish report", e)
        close_pipeline(STATE["pipe"])
        STATE["pipe"] = None; STATE["loaded"] = False
    paths = load_paths(f"{SOL_ROOT}/paths-{task}.json", task=task)
    outdir = f"{RUNTIME}/outputs/svc-{task}-{int(time.time())}"
    p = Pipeline(paths, outdir, task=task); t0 = time.time()
    try:
        p.start(warm_case(task))
    except Exception:
        close_pipeline(p)
        raise
    log(f"pipeline {task} ready in {time.time()-t0:.0f}s")
    STATE.update(pipe=p, task=task, loaded=True, outdir=outdir); return p
def release_idle_cache(pipe):
    """Hand back stage 2's CUDA cache as soon as a take is finished.

    Sol's Pipeline trims stage 2 at the START of the next take, so a warm
    engine kept ~5 GiB of stage-2 cache between takes. The studio measures the
    next take's warm admission (22 GiB envelope + 2 GiB reserve) in exactly that
    gap and refused the following take at ~23.4 GiB (seen 2026-09-18, 09-26 and
    09-27). This is the same idle trim the Pipeline itself runs (gc + empty_cache
    on an idle session), only earlier; nothing is relaxed. A failed trim is
    logged, not a safety event: the next take runs the same call first and any
    real session fault trips the breaker there."""
    worker = getattr(pipe, "stage2", None)
    if worker is None:
        return None
    try:
        result = worker.call("release_idle_cache", qwen_resident=True)
        STATE["idle_trims"] = STATE.get("idle_trims", 0) + 1
        return result
    except Exception as e:
        log("stage2 idle trim failed", type(e).__name__, str(e)[:200])
        return None
def b64_to_file(b64, name):
    fp = f"{RUNTIME}/inputs/{name}"; open(fp, "wb").write(base64.b64decode(b64)); return fp
def find_mp4(rid):
    for root in ([STATE.get("outdir")] if STATE.get("outdir") else []) + [f"{RUNTIME}/outputs"]:
        for dp, dn, fn in os.walk(root):
            if dp.endswith(f"/{rid}/stage2"):
                for f in fn:
                    if f.endswith(".mp4"): return os.path.join(dp, f)
    return None
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/health"):
            blocked = safety_latched()
            loading = STATE.get("loading", False)
            return self._send(503 if blocked else 200, {"ok": not blocked, "blocked": blocked, "loading": loading, "engine": "h3", "impl": "sol-h3-spark", "loaded": STATE["loaded"] and not blocked, "busy": STATE["busy"] or loading, "variant": VARIANT, "task": STATE["task"], "turbo_preset": TURBO, "fused_combined": False, "attention": "sol", "cache": {"renders": STATE["renders"], "errors": STATE["errors"], "last_error": STATE["last_error"]}, "uptime": int(time.time()-STATE["started"]), "stage1_geometry": None if SINGULARITY else stage1_geometry(), **({"label": singularity.LABEL, "max_frames": int(os.environ.get("H3_SINGULARITY_MAX_FRAMES") or singularity.TRAINED_MAX_FRAMES), "warm_s": getattr(STATE["pipe"], "warm_s", None)} if SINGULARITY else {})})
        self._send(404, {"ok": False, "error": "not found"})
    def do_POST(self):
        if not self.path.startswith("/generate"): return self._send(404, {"ok": False, "error": "not found"})
        n = int(self.headers.get("Content-Length") or 0); req = json.loads(self.rfile.read(n) or b"{}")
        prompt = (req.get("prompt") or "").strip(); rid = str(req.get("request_id") or f"r{int(time.time())}")
        if not prompt: return self._send(400, {"ok": False, "error": "bad request: prompt required"})
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", rid): return self._send(400, {"ok": False, "error": "bad request: request_id"})
        task = (singularity.TASK if SINGULARITY else
                "ref2va" if (req.get("references") or req.get("video_references")) else
                "fl2va" if req.get("start_image_b64") else "t2va")
        if not authorize_values(GPU_PROTOCOL, dict(self.headers.items()), engine="h3", task=task):
            return self._send(403, {"ok": False, "error": "exact GPU lease delegation required"})
        out = f"{OUT_DIR}/job-{rid}.mp4"
        if os.path.exists(out): return self._send(200, {"ok": True, "file": os.path.basename(out), "seed": req.get("seed"), "elapsed": 0, "cached": True})
        if not LOCK.acquire(blocking=False): return self._send(409, {"ok": False, "error": "busy"})
        STATE["busy"] = True; t0 = time.time()
        if SINGULARITY:
            try:
                return self._generate_singularity(req, rid, out, t0)
            finally:
                STATE["busy"] = False; LOCK.release()
        try:
            refs = req.get("references") or []; vrefs = req.get("video_references") or []; seed = int(req.get("seed") or 0)
            if refs or vrefs:
                task = "ref2va"; rl = [{"type": "image", "path": b64_to_file(r["b64"], f"{rid}-ref{i}.png")} for i, r in enumerate(refs)]
                rl += [{"type": "video", "path": v["file"]} for v in vrefs[:3]]
                if req.get("audio_wav_b64"): rl.append({"type": "audio", "path": b64_to_file(req["audio_wav_b64"], f"{rid}-audio.wav")})
                subj = " ".join(f"<Subject {i+1}> is the person in <Picture {i+1}>." for i in range(len(refs)))
                case = {"case_id": rid, "task": task, "seed": seed, "references": rl, "prompt": f"subject_definitions: {subj} detailed_description: {prompt}"}
            elif req.get("start_image_b64"):
                task = "fl2va"; case = {"case_id": rid, "task": task, "seed": seed, "first_frame": b64_to_file(req["start_image_b64"], f"{rid}-first.png"), "prompt": f"integrated_multimodal_description: {prompt}"}
                if req.get("end_image_b64"): case["last_frame"] = b64_to_file(req["end_image_b64"], f"{rid}-last.png")
            else:
                task = "t2va"; case = {"case_id": rid, "task": task, "seed": seed, "prompt": prompt}
            log("generate", rid, task); p = ensure_pipeline(task)
            try:
                row = p.generate(case)
            except Exception as e:
                trip_safety("generation_failed", e)
                raise
            src = None
            if isinstance(row, dict):
                for k in ("output", "file", "video", "mp4"):
                    if row.get(k) and os.path.exists(str(row[k])): src = str(row[k]); break
            src = src or find_mp4(rid)
            if not src: raise RuntimeError(f"no mp4 produced for {rid}: {str(row)[:300]}")
            shutil.copyfile(src, out); STATE["renders"] += 1
            release_idle_cache(p)
            self._send(200, {"ok": True, "file": os.path.basename(out), "seed": seed, "elapsed": round(time.time()-t0, 1), "cached": False, "task": task})
        except Exception as e:
            STATE["errors"] += 1; STATE["last_error"] = str(e)[:300]; log("ERROR", traceback.format_exc()); self._send(500, {"ok": False, "error": str(e)[:500]})
        finally:
            STATE["busy"] = False; LOCK.release()
    def _generate_singularity(self, req, rid, out, t0):
        try:
            log("generate", rid, singularity.TASK, f"frames={req.get('frames')}", f"orientation={req.get('orientation')}")
            p = ensure_pipeline(singularity.TASK)
            try:
                row = p.generate(req, rid)
            except (singularity.SingularityRequestError, singularity.SingularityConfigError) as e:
                # a refused request or graph never reached the GPU: not a box-safety event
                STATE["errors"] += 1; STATE["last_error"] = str(e)[:300]
                code = 400 if isinstance(e, singularity.SingularityRequestError) else 503
                return self._send(code, {"ok": False, "error": str(e)[:500]})
            except Exception as e:
                trip_safety("generation_failed", e)
                raise
            # finish in a work file: a reconnecting caller must never be handed a
            # half-finished take as the cached result
            work = f"{out}.work.mp4"
            singularity.copy_output(row["output"], work)
            sync = av_sync.sync_trim(work, os.environ, fps=singularity.FPS)
            os.replace(work, out)
            log("lip-sync", rid, json.dumps(sync, sort_keys=True))
            STATE["renders"] += 1
            self._send(200, {"ok": True, "file": os.path.basename(out), "seed": req.get("seed"),
                             "elapsed": round(time.time()-t0, 1), "render_s": row.get("render_s"),
                             "cached": False, "task": singularity.TASK, "variant": VARIANT,
                             "frames": row.get("frames"), "orientation": row.get("orientation"),
                             "av_sync": sync})
        except Exception as e:
            STATE["errors"] += 1; STATE["last_error"] = str(e)[:300]; log("ERROR", traceback.format_exc())
            code = 503 if isinstance(e, singularity.SingularityConfigError) else 500
            self._send(code, {"ok": False, "error": str(e)[:500]})
        finally:
            singularity.sweep_inputs(RUNTIME, rid)

if __name__ == "__main__":
    log(f"sol engine server :{PORT} variant={VARIANT} pkg={PKG}"
        + ("" if SINGULARITY else f" stage1_geometry={stage1_geometry()}"))
    pre = os.environ.get("SOL_PRELOAD", "").strip()
    if pre:
        if authorize_environment(GPU_PROTOCOL, engine="h3", task=pre, phases=("load",)):
            threading.Thread(target=lambda: ensure_pipeline(pre), daemon=True).start()
        else:
            log("preload denied: exact load/render delegation is absent")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
