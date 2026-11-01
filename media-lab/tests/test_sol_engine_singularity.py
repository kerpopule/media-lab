"""The H3 engine server with H3_VARIANT=singularity (Real / Long): same safety
latch and lease checks as Sol, one task family, its own error classes."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import h3_singularity as sing  # noqa: E402


class FakePipeline:
    made = []

    def __init__(self, env, runtime, log=print, media_dir=None):
        self.env, self.runtime, self.media_dir = env, runtime, media_dir
        self.comfy = type("C", (), {"dirs": {"out": Path(runtime) / "singularity" / "out"}})()
        self.warm_s = 1.5
        self.closed = False
        self.requests = []
        self.fail = None
        FakePipeline.made.append(self)

    @property
    def process(self):
        return None

    def start(self):
        if self.env.get("FAKE_START_FAIL") == "config":
            raise sing.SingularityConfigError("runtime missing")
        if self.env.get("FAKE_START_FAIL") == "render":
            raise sing.SingularityError("CUDA out of memory during warm-up")
        return self

    def close(self):
        self.closed = True

    def generate(self, req, rid):
        self.requests.append((req, rid))
        if self.fail:
            raise self.fail
        out = Path(self.runtime) / f"{rid}.mp4"
        out.write_bytes(b"take")
        return {"output": str(out), "frames": 311, "orientation": "portrait", "render_s": 12.0}


class SingularityServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        env = {"SOL_PKG": base, "SOL_ROOT": base, "SOL_H3_SPARK_RUNTIME_ROOT": base,
               "SOL_OUT_DIR": base, "XDG_RUNTIME_DIR": base, "H3_VARIANT": "singularity"}
        self.environ = patch.dict(os.environ, env)
        self.environ.start(); self.addCleanup(self.environ.stop)
        # Sol's own runtime must not even be importable for this variant
        modules = patch.dict(sys.modules, {"runtime.config": None, "runtime.pipeline": None})
        modules.start(); self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location("sol_singularity_under_test",
                                                      ROOT / "runner/sol_engine_server.py")
        self.s = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.s)
        boot_id = "11111111-1111-4111-8111-111111111111"
        (Path(base) / "boot-id").write_text(boot_id)
        os.environ["SOL_BOOT_ID_PATH"] = str(Path(base) / "boot-id")
        (Path(base) / "boot-clearance.json").write_text('{"boot_id":"' + boot_id + '","approved":true}')
        FakePipeline.made.clear()
        pipeline = patch.object(self.s.singularity, "SingularityPipeline", FakePipeline)
        pipeline.start(); self.addCleanup(pipeline.stop)
        self.sync = patch.object(self.s.av_sync, "sync_trim",
                                 return_value={"checked": True, "applied": True, "shift_ms": -28.0})
        self.sync.start(); self.addCleanup(self.sync.stop)

    def _post(self, body, authorized=True):
        raw = json.dumps(body).encode(); reply = []
        handler = self.s.H.__new__(self.s.H); handler.path = "/generate"
        handler.headers = {"Content-Length": str(len(raw))}; handler.rfile = io.BytesIO(raw)
        handler._send = lambda status, data: reply.append((status, data))
        with patch.object(self.s, "authorize_values", return_value=authorized) as auth:
            handler.do_POST()
        return reply[0], auth

    def _health(self):
        reply = []
        handler = self.s.H.__new__(self.s.H); handler.path = "/health"
        handler._send = lambda status, data: reply.append((status, data))
        handler.do_GET()
        return reply[0]

    def test_health_names_the_variant_task_and_limits(self):
        self.s.ensure_pipeline("singularity")
        status, body = self._health()
        self.assertEqual(200, status)
        self.assertEqual(("singularity", "singularity"), (body["variant"], body["task"]))
        self.assertTrue(body["loaded"])
        self.assertEqual("Real / Long", body["label"])
        self.assertEqual(362, body["max_frames"])
        self.assertEqual(1.5, body["warm_s"])

    def test_generate_needs_the_singularity_lease_and_returns_the_sync_receipt(self):
        (status, body), auth = self._post({"prompt": "hi", "request_id": "r1", "frames": 300,
                                           "orientation": "portrait", "seed": 3})
        self.assertEqual(200, status, body)
        self.assertEqual("singularity", auth.call_args.kwargs["task"])
        self.assertEqual("h3", auth.call_args.kwargs["engine"])
        self.assertEqual("job-r1.mp4", body["file"])
        self.assertEqual(b"take", (Path(self.s.OUT_DIR) / "job-r1.mp4").read_bytes())
        self.assertFalse((Path(self.s.OUT_DIR) / "job-r1.mp4.work.mp4").exists())
        self.assertEqual(-28.0, body["av_sync"]["shift_ms"])
        self.assertEqual((311, "portrait"), (body["frames"], body["orientation"]))
        # a reconnect gets the finished take, never a second render
        (status, again), _ = self._post({"prompt": "hi", "request_id": "r1"})
        self.assertTrue(again["cached"]); self.assertEqual(1, len(FakePipeline.made[0].requests))

    def test_without_the_exact_lease_nothing_runs(self):
        (status, body), _ = self._post({"prompt": "hi", "request_id": "r2"}, authorized=False)
        self.assertEqual(403, status)
        self.assertEqual([], FakePipeline.made)

    def test_a_refused_request_is_400_and_does_not_latch(self):
        self.s.ensure_pipeline("singularity")
        FakePipeline.made[0].fail = sing.SingularityRequestError("Real / Long takes at most 9 reference pictures")
        (status, body), _ = self._post({"prompt": "hi", "request_id": "r3"})
        self.assertEqual(400, status)
        self.assertFalse(self.s.safety_latched())

    def test_a_render_failure_latches_the_safety_stop_like_sol(self):
        self.s.ensure_pipeline("singularity")
        FakePipeline.made[0].fail = sing.SingularityError("render failed in SamplerCustomAdvanced: OOM")
        (status, body), _ = self._post({"prompt": "hi", "request_id": "r4"})
        self.assertEqual(500, status)
        self.assertTrue(self.s.safety_latched())
        self.assertTrue((Path(self.s.SOL_ROOT) / "safety-stop.json").exists())

    def test_a_missing_runtime_is_503_without_latching(self):
        os.environ["FAKE_START_FAIL"] = "config"
        (status, body), _ = self._post({"prompt": "hi", "request_id": "r5"})
        self.assertEqual(503, status)
        self.assertFalse(self.s.safety_latched())

    def test_a_failed_warm_up_latches(self):
        os.environ["FAKE_START_FAIL"] = "render"
        with self.assertRaises(sing.SingularityError):
            self.s.ensure_pipeline("singularity")
        self.assertTrue(self.s.safety_latched())

    def test_only_its_own_task_family_is_served(self):
        with self.assertRaises(RuntimeError):
            self.s.ensure_pipeline("t2va")

    def test_closing_uses_the_pipelines_own_process_proof(self):
        pipe = self.s.ensure_pipeline("singularity")
        self.s.close_pipeline(pipe)
        self.assertTrue(pipe.closed)


if __name__ == "__main__":
    unittest.main()
