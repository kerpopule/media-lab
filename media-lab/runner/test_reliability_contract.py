#!/usr/bin/env python3
"""Deterministic regression gates for Media Lab crash/OOM policy.

This test intentionally does not import app.py: importing starts production worker
threads. It verifies the deployed source contract and also mutation-tests each gate
so a vacuous assertion cannot pass.
"""
from pathlib import Path
import re
import unittest
from types import SimpleNamespace
from unittest import mock
from runner import queue_watchdog

ROOT = Path(__file__).resolve().parents[1]


def contract_errors(app: str, image: str, watchdog: str = "", supervisor: str = "",
                    pool: str = "", h3: str = "", engine: str = ""):
    errors = []
    q = re.search(r"^QWEN_GB\s*=\s*(\d+)", app, re.M)
    if not q or int(q.group(1)) < 32:
        errors.append("qwen-residency-underpriced")
    floor = re.search(r"^MEM_FLOOR_GB\s*=\s*(\d+)", app, re.M)
    if not floor or int(floor.group(1)) < 24:
        errors.append("video-memory-floor-too-low")
    if "pause_chat_for_video(j)" not in app or "CHAT_PAUSE_RECEIPT" not in app:
        errors.append("no-crash-safe-chat-pause")
    if 'http_json("http://127.0.0.1:8003/v1/models"' not in app:
        errors.append("chat-restore-health-unverified")
    settle = app[app.find("def restore_warm_ltx_idle"):app.find("def worker")]
    reaper = app[app.find("def reap_idle_engines"):app.find("def comfy_run")]
    if 'RESIDENCY.apply(target' not in settle or \
       'default_profile": "qwen-ltx-default"' not in (ROOT / "config" / "model-residency-policy.json").read_text() or \
       'target = desired["name"]' not in settle or \
       'release_voice_weights()' not in settle or \
       'stop_engine("music")' not in settle or \
       'for name in ("h3", "music", "image")' not in reaper or \
       'restore_warm_ltx_idle()' not in reaper:
        errors.append("ltx-warm-idle-default-missing")
    ensure = app[app.find("def ensure_engine"):app.find("class _ResidencyRuntime")]
    if 'ensure_video_residency(name, j)' not in ensure or "pause_chat_for_video" in ensure:
        errors.append("warm-ltx-idle-repauses-chat")
    marks = re.search(r"INFRA_FAILURE_MARKS\s*=\s*\((.*?)\)\s*\n", app, re.S)
    if not marks or '"try again"' in marks.group(1):
        errors.append("blanket-retry-loop")
    if not marks or '"film crew is down"' not in marks.group(1):
        errors.append("supplied-frame-engine-failure-not-retryable")
    stop_start = app.find("def stop_engine")
    stop_path = app[stop_start:app.find("def engine_up", stop_start)]
    if '"docker", "logs", "--tail", "400"' not in stop_path or \
       'engine-events.log' not in stop_path:
        errors.append("engine-crash-logs-discarded")
    if '@app.post("/release")' not in image:
        errors.append("missing-image-release-endpoint")
    if "proceeding alongside" in image or "proceeded_despite = blocker" in image:
        errors.append("heavy-engine-overlap-enabled")
    if "image/video overlap" not in image or "disabled for reliability" not in image:
        errors.append("missing-image-video-hard-interlock")
    if "jd.mkdir(parents=True, exist_ok=True)" not in app:
        errors.append("job-workdir-not-created")
    if watchdog and '"media-lab-gpu-reservation.service"' not in watchdog:
        errors.append("watchdog-rejects-idle-reservation")
    if watchdog and ("TUNNEL_PROBE_UA" not in watchdog or
                     "cloudflared_tunnel_ha_connections" not in watchdog):
        errors.append("watchdog-blind-to-connectorless-tunnel")
    if watchdog and "and mins < STALL_HARD_MIN" in watchdog:
        errors.append("watchdog-kills-busy-long-render")
    if supervisor and '("media-lab-pool.service", None, None)' in supervisor:
        errors.append("supervisor-restarts-idle-pool")
    if pool and '*"sleep infinity"*' in pool:
        errors.append("broad-orphan-kill")
    if h3 and "--restart unless-stopped" in h3:
        errors.append("h3-self-restarts")
    if engine and "temps = ref_files" not in engine:
        errors.append("engine-temp-leak")
    if "corrupt chat pause receipt; retaining maintenance" not in app:
        errors.append("corrupt-chat-receipt-cleared")
    if "media-lab-inference.lock" not in app or "media-lab-inference.lock" not in image:
        errors.append("no-cross-process-inference-lock")
    chat = app[app.find("def chat(r: ChatReq"):app.find("# ---------- gate endpoint")]
    if "media-lab-inference.lock" not in chat or "fcntl.flock" not in chat:
        errors.append("qwen-chat-bypasses-inference-scheduler")
    if 'return "fallback"' in app:
        errors.append("silent-image-fallback")
    if 'if j.get("cancel"):' not in app:
        errors.append("cancelled-job-auto-requeues")
    if "((768, 1344)" in app or "(1344, 768)))" in app:
        errors.append("h3-talking-cpu-fallback-canvas")
    if '"booting": bool(STATE.get("booting"))' not in image or \
       'and not bool(h.get("booting"))' not in supervisor:
        errors.append("supervisor-kills-cold-image-boot")
    image_path = app[app.find("def image_via_service"):app.find("def run_image")]
    if "pause_chat_for_video(j)" not in image_path:
        errors.append("image-overlaps-qwen")
    if 'release_image_weights("restoring chat after media work")' not in settle:
        errors.append("image-weights-overlap-restored-qwen")
    if 'j.get("kind") in COMPANION_JOB_KINDS' not in app:
        errors.append("settle-interrupts-image-batch")
    image_req = app[app.find("class ImageReq"):app.find("class CharReq")]
    image_path = app[app.find("def image_via_service"):app.find("def run_image")]
    run_image_path = app[app.find("def _run_image"):app.find("def run_character")]
    if "seed: Optional[int]" not in image_req or '"seed": int(r["seed"])' not in image_path:
        errors.append("image-seed-not-reproducible")
    if "scene_place: bool" not in image_req or "face_target: float" not in image_req or \
       "_scene_canvas(src, iw, ih, canvas" not in run_image_path:
        errors.append("identity-scene-frame-not-target-aspect")
    if "reference_source: str" not in image_req or "quality: bool" not in image_req or \
       'body["reference_image_path"]' not in image_path or \
       'body["quality"] = True' not in image_path:
        errors.append("image-composition-identity-quality-contract-missing")
    run_say_start = app.find("def run_say")
    run_say = app[run_say_start:app.find("RUNNERS =", run_say_start)]
    if "audio_signal_metrics(audio_master)" not in run_say or \
       "silent or unreadable" not in run_say:
        errors.append("silent-supplied-audio-reaches-gpu")
    say_req = app[app.find("class SayReq"):app.find("@app.get(\"/api/voices\")")]
    say_route_start = app.find("def character_say")
    say_route = app[say_route_start:app.find("# ---------- song", say_route_start)]
    if "source_scene_complete: bool" not in say_req or "motion: str" not in say_req or \
       'bool(r.get("source_scene_complete"))' not in run_say or \
       '"source_scene_complete": r.source_scene_complete' not in say_route or \
       '"motion": r.motion.strip()' not in say_route:
        errors.append("scene-complete-source-redrawn")
    job_runner_start = app.find("def run_queued_job")
    worker = app[job_runner_start:app.find("mask_sweep()", job_runner_start)]
    boot_start = app.find("def _boot_engine")
    boot = app[boot_start:app.find("def resident_engines", boot_start)]
    if 'if j.get("cancel"):' not in worker or \
       'j is not None and j.get("cancel")' not in boot or \
       'if st == "cancelled":' not in run_say:
        errors.append("cancelled-job-warms-engine")
    return errors


class ReliabilityContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "app.py").read_text()
        cls.image = (ROOT / "image-svc" / "image_service.py").read_text()
        cls.watchdog = (ROOT / "runner" / "queue_watchdog.py").read_text()
        cls.supervisor = (ROOT / "runner" / "service_supervisor.py").read_text()
        cls.pool = (ROOT / "runner" / "pool_lock.sh").read_text()
        cls.h3 = (ROOT / "runner" / "start_h3_engine.sh").read_text()
        cls.engine = (ROOT / "runner" / "engine_server.py").read_text()

    def sources(self, app=None, image=None, watchdog=None, supervisor=None,
                pool=None, h3=None, engine=None):
        return (app or self.app, image or self.image, watchdog or self.watchdog,
                supervisor or self.supervisor, pool or self.pool, h3 or self.h3,
                engine or self.engine)

    def test_current_source_contract(self):
        self.assertEqual([], contract_errors(*self.sources()))

    def test_detector_catches_every_known_regression(self):
        mutations = {
            "qwen-residency-underpriced": (self.app.replace("QWEN_GB = 32", "QWEN_GB = 20"), self.image),
            "video-memory-floor-too-low": (self.app.replace("MEM_FLOOR_GB = 24", "MEM_FLOOR_GB = 12"), self.image),
            "no-crash-safe-chat-pause": (self.app.replace("pause_chat_for_video(j)", "True"), self.image),
            "chat-restore-health-unverified": (self.app.replace("http://127.0.0.1:8003/v1/models", "http://127.0.0.1:9999/health"), self.image),
            "ltx-warm-idle-default-missing": (self.app.replace('RESIDENCY.apply(target', 'RESIDENCY.plan(target'), self.image),
            "warm-ltx-idle-repauses-chat": (self.app.replace('st = ensure_video_residency(name, j)', 'pause_chat_for_video(j); st = "up"'), self.image),
            "blanket-retry-loop": (self.app.replace('"gpu reserved",', '"gpu reserved", "try again",'), self.image),
            "supplied-frame-engine-failure-not-retryable": (self.app.replace('"film crew is down",', ''), self.image),
            "engine-crash-logs-discarded": (self.app.replace('engine-events.log', 'discarded-engine.log'), self.image),
            "missing-image-release-endpoint": (self.app, self.image.replace('@app.post("/release")', '@app.post("/gone")')),
            "heavy-engine-overlap-enabled": (self.app, self.image + "\nproceeded_despite = blocker\n"),
            "missing-image-video-hard-interlock": (self.app, self.image.replace("disabled for reliability", "allowed for speed")),
            "job-workdir-not-created": (self.app.replace("jd.mkdir(parents=True, exist_ok=True)", "pass  # regression"), self.image),
            "image-seed-not-reproducible": (self.app.replace("seed: Optional[int] = None", "seed_dropped: Optional[int] = None"), self.image),
            "identity-scene-frame-not-target-aspect": (self.app.replace("scene_place: bool = False", "scene_place_dropped: bool = False"), self.image),
            "silent-supplied-audio-reaches-gpu": (self.app.replace("audio_signal_metrics(audio_master)", "{'ok': True}"), self.image),
            "scene-complete-source-redrawn": (self.app.replace("source_scene_complete: bool = False", "source_scene_complete_dropped: bool = False"), self.image),
            "cancelled-job-warms-engine": (self.app.replace(
                'if j.get("cancel"):\n            j["status"] = "error"; j["stage"] = "error"',
                'if False:\n            j["status"] = "error"; j["stage"] = "error"'), self.image),
            "qwen-chat-bypasses-inference-scheduler": (self.app.replace(
                'with open("/run/user/1000/media-lab-inference.lock", "a+") as gate:',
                'with open("/tmp/not-the-inference-lock", "a+") as gate:'), self.image),
        }
        for expected, pair in mutations.items():
            with self.subTest(expected=expected):
                self.assertIn(expected, contract_errors(*pair))

    def test_detector_catches_independent_review_regressions(self):
        mutations = {
            "watchdog-rejects-idle-reservation": self.sources(watchdog=self.watchdog.replace('"media-lab-gpu-reservation.service"', '"gone"')),
            "watchdog-blind-to-connectorless-tunnel": self.sources(watchdog=self.watchdog.replace("TUNNEL_PROBE_UA", "OLD_PROBE_UA").replace("cloudflared_tunnel_ha_connections", "missing_ha_metric")),
            "watchdog-kills-busy-long-render": self.sources(watchdog=self.watchdog.replace("and an_engine_is_working():", "and an_engine_is_working() and mins < STALL_HARD_MIN:")),
            "supervisor-restarts-idle-pool": self.sources(supervisor=self.supervisor.replace("# media-lab-pool.service is intentionally inactive in cold-idle mode.", '("media-lab-pool.service", None, None),')),
            "broad-orphan-kill": self.sources(pool=self.pool.replace('*flock*"$LOCK"*sleep*)', '*flock*"$LOCK"*sleep*|*"sleep infinity"*)')),
            "h3-self-restarts": self.sources(h3=self.h3.replace("--restart no", "--restart unless-stopped")),
            "engine-temp-leak": self.sources(engine=self.engine.replace("temps = ref_files", "temps = []", 1)),
            "corrupt-chat-receipt-cleared": self.sources(app=self.app.replace("corrupt chat pause receipt; retaining maintenance", "ignored corrupt receipt")),
            "no-cross-process-inference-lock": self.sources(app=self.app.replace("media-lab-inference.lock", "old-video.lock")),
            "silent-image-fallback": self.sources(app=self.app + '\nreturn "fallback"\n'),
        }
        for expected, sources in mutations.items():
            with self.subTest(expected=expected):
                self.assertIn(expected, contract_errors(*sources))

    def test_idle_reservation_is_accepted_without_pool_acquire(self):
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(argv)
            unit = argv[-1]
            return SimpleNamespace(returncode=0 if unit == "media-lab-gpu-reservation.service" else 1,
                                   stdout="", stderr="")
        with mock.patch.object(queue_watchdog.os.path, "exists", return_value=True), \
             mock.patch.object(queue_watchdog.subprocess, "run", side_effect=fake_run):
            queue_watchdog.ensure_pool_lock()
        self.assertFalse(any(c and c[0] == "bash" for c in calls), calls)

    def test_tunnel_probe_uses_nonblocked_user_agent(self):
        fake = SimpleNamespace(getcode=lambda: 401)
        with mock.patch.object(queue_watchdog.urllib.request, "urlopen", return_value=fake) as opened:
            self.assertEqual(401, queue_watchdog.public_tunnel_code())
        request = opened.call_args.args[0]
        self.assertEqual(queue_watchdog.TUNNEL_PROBE_UA,
                         request.get_header("User-agent"))

    def test_connectorless_403_restarts_after_two_strikes(self):
        state = {}
        with mock.patch.object(queue_watchdog, "public_tunnel_code", return_value=403), \
             mock.patch.object(queue_watchdog, "tunnel_ha_connections", return_value=0), \
             mock.patch.object(queue_watchdog.subprocess, "run") as run:
            queue_watchdog.check_tunnel(state)
            self.assertEqual(1, state["tunnel_strikes"])
            run.assert_not_called()
            queue_watchdog.check_tunnel(state)
        run.assert_called_once_with(
            ["systemctl", "--user", "restart", "media-lab-tunnel.service"],
            env=queue_watchdog.ENV, check=False,
        )
        self.assertEqual(0, state["tunnel_strikes"])

    def test_cloudflare_403_with_live_connectors_does_not_restart(self):
        state = {"tunnel_strikes": 1}
        with mock.patch.object(queue_watchdog, "public_tunnel_code", return_value=403), \
             mock.patch.object(queue_watchdog, "tunnel_ha_connections", return_value=4), \
             mock.patch.object(queue_watchdog.subprocess, "run") as run:
            queue_watchdog.check_tunnel(state)
        run.assert_not_called()
        self.assertEqual(0, state["tunnel_strikes"])

    def test_public_530_is_unhealthy_even_with_stale_connector_metric(self):
        state = {}
        with mock.patch.object(queue_watchdog, "public_tunnel_code", return_value=530), \
             mock.patch.object(queue_watchdog, "tunnel_ha_connections", return_value=4), \
             mock.patch.object(queue_watchdog.subprocess, "run") as run:
            queue_watchdog.check_tunnel(state)
        run.assert_not_called()
        self.assertEqual(1, state["tunnel_strikes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
