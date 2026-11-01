import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from media_lab_core import local_config
from runner import queue_watchdog, service_supervisor

ROOT = Path(__file__).resolve().parents[1]
QUEUE_URL = local_config.studio_url() + "/api/queue"   # config/local.env, never a literal


class WatchdogMaestroSafetyTests(unittest.TestCase):
    def test_queue_probe_uses_configured_studio_url_and_no_proxy(self):
        self.assertEqual(QUEUE_URL, queue_watchdog.QUEUE_URL)
        source = (ROOT / "runner/queue_watchdog.py").read_text(encoding="utf-8")
        self.assertIn("build_opener(urllib.request.ProxyHandler({}))", source)

    def test_queue_probe_proves_it_is_local_with_the_token_not_a_host_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "local-token.txt").write_text("f" * 64 + "\n")
            with mock.patch.object(local_config, "home", return_value=Path(tmp)):
                request = queue_watchdog.queue_request()
        self.assertIsNone(request.get_header("Host"))
        self.assertEqual("f" * 64, request.unredirected_hdrs.get("X-media-lab-local"))
        source = (ROOT / "runner/queue_watchdog.py").read_text(encoding="utf-8")
        self.assertNotIn('"Host": "localhost"', source)

    def test_a_refused_probe_never_restarts_the_studio(self):
        refused = urllib.error.HTTPError(QUEUE_URL, 401, "locked", {}, None)
        with mock.patch.object(queue_watchdog, "load_state", return_value={}), \
             mock.patch.object(queue_watchdog.LOCAL_OPENER, "open", side_effect=refused), \
             mock.patch.object(queue_watchdog, "gpu_recovery_held", return_value=False), \
             mock.patch.object(queue_watchdog.subprocess, "run") as run, \
             mock.patch.object(queue_watchdog, "restart_app") as restart, \
             mock.patch.object(queue_watchdog, "save_state"), \
             mock.patch.object(queue_watchdog, "log") as log:
            queue_watchdog.main()
        restart.assert_not_called()
        self.assertFalse(any("restart" in str(call.args[0]) for call in run.call_args_list))
        self.assertIn("standing clear", log.call_args.args[0])

    def test_supervisor_media_lab_probe_uses_configured_studio_url_and_no_proxy(self):
        # The queue watchdog is the studio's one restart owner (2026-09-26);
        # the supervisor still probes with the same client when asked.
        self.assertNotIn("media-lab-simple.service", [u for u, _, _ in service_supervisor.UNITS])
        fake_response = mock.MagicMock()
        fake_response.__enter__.return_value.getcode.return_value = 200
        with mock.patch.object(service_supervisor.LOCAL_OPENER, "open", return_value=fake_response) as opened, \
             mock.patch.object(service_supervisor.urllib.request, "urlopen") as global_open:
            self.assertTrue(service_supervisor.probe(QUEUE_URL))
        opened.assert_called_once()
        global_open.assert_not_called()

    def test_supervisor_studio_probe_carries_the_local_token(self):
        # The family door answers an unauthenticated probe with 401 (alive, but
        # log noise twice a minute); the supervisor must prove it is local.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "local-token.txt").write_text("e" * 64 + "\n")
            fake_response = mock.MagicMock()
            fake_response.__enter__.return_value.getcode.return_value = 200
            with mock.patch.object(local_config, "home", return_value=Path(tmp)), \
                 mock.patch.object(service_supervisor.LOCAL_OPENER, "open",
                                   return_value=fake_response) as opened:
                self.assertTrue(service_supervisor.probe(QUEUE_URL))
        request = opened.call_args.args[0]
        self.assertEqual(QUEUE_URL, request.full_url)
        self.assertEqual("e" * 64, request.unredirected_hdrs.get("X-media-lab-local"))
        self.assertIsNone(request.get_header("Host"))
        source = (ROOT / "runner/service_supervisor.py").read_text(encoding="utf-8")
        self.assertNotIn('"Host": "localhost"', source)

    def test_supervisor_sends_the_token_to_no_other_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "local-token.txt").write_text("e" * 64 + "\n")
            with mock.patch.object(local_config, "home", return_value=Path(tmp)):
                for url in ("http://127.0.0.1:8295/health", "http://127.0.0.1:8003/v1/models",
                            "http://127.0.0.1:17493/health"):
                    with self.subTest(url=url):
                        request = service_supervisor.probe_request(url)
                        self.assertNotIn("X-media-lab-local", request.unredirected_hdrs)
                        self.assertNotIn("X-media-lab-local", request.headers)

    def test_supervisor_probe_still_counts_a_refusal_as_alive_without_a_token(self):
        # No token on this box (or unreadable): the probe goes out bare and a
        # 401 still proves the server is up, so nothing is restarted over it.
        refused = urllib.error.HTTPError(QUEUE_URL, 401, "locked", {}, None)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(local_config, "home", return_value=Path(tmp)), \
                 mock.patch.object(service_supervisor.LOCAL_OPENER, "open",
                                   side_effect=refused) as opened:
                self.assertTrue(service_supervisor.probe(QUEUE_URL))
        self.assertNotIn("X-media-lab-local", opened.call_args.args[0].unredirected_hdrs)

    def test_active_runner_detection_uses_queue_owned_docker_exec(self):
        for module in (queue_watchdog, service_supervisor):
            with self.subTest(module=module.__name__), mock.patch.object(
                module.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="1905346\n", stderr=""),
            ) as run:
                self.assertTrue(module.maestro_queue_runner_active())
                command = run.call_args.args[0]
                self.assertEqual("pgrep", command[0])
                self.assertIn("media-lab-maestro-runner", command[-1])

    def test_both_watchdogs_stand_clear_during_queue_owned_render(self):
        queue_source = (ROOT / "runner/queue_watchdog.py").read_text(encoding="utf-8")
        supervisor_source = (ROOT / "runner/service_supervisor.py").read_text(encoding="utf-8")
        self.assertIn("API probe failed while a queue-owned Maestro runner is active — standing clear", queue_source)
        self.assertIn("probe failed during a queue-owned Maestro render — standing clear", supervisor_source)

    def test_queue_watchdog_stands_clear_while_durable_gpu_recovery_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "gpu-recovery-hold.json"
            with mock.patch.object(queue_watchdog, "GPU_RECOVERY_HOLD", marker):
                self.assertFalse(queue_watchdog.gpu_recovery_held())
                marker.write_text('{"reason":"boot-changed"}')
                self.assertTrue(queue_watchdog.gpu_recovery_held())

    def test_recovery_hold_suppresses_restart_when_api_is_unreachable(self):
        service = SimpleNamespace(stdout="inactive\n", returncode=3)
        with mock.patch.object(queue_watchdog, "load_state", return_value={}), \
             mock.patch.object(queue_watchdog.LOCAL_OPENER, "open", side_effect=OSError("down")), \
             mock.patch.object(queue_watchdog, "gpu_recovery_held", return_value=True), \
             mock.patch.object(queue_watchdog.subprocess, "run", return_value=service) as run, \
             mock.patch.object(queue_watchdog, "save_state"), \
             mock.patch.object(queue_watchdog, "log"):
            queue_watchdog.main()
        self.assertFalse(any("restart" in call.args[0] for call in run.call_args_list))

    def test_recovery_hold_suppresses_stalled_running_restart(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "active": [{"id": "job-1", "kind": "video", "status": "running", "stage": "loading"}],
        }).encode()
        state = {"run_seen": {"sig": "job-1:loading", "since": 0}}
        with mock.patch.object(queue_watchdog, "load_state", return_value=state), \
             mock.patch.object(queue_watchdog.LOCAL_OPENER, "open", return_value=response), \
             mock.patch.object(queue_watchdog, "gpu_recovery_held", return_value=True), \
             mock.patch.object(queue_watchdog, "ensure_pool_lock"), \
             mock.patch.object(queue_watchdog, "check_tunnel"), \
             mock.patch.object(queue_watchdog, "restart_app") as restart, \
             mock.patch.object(queue_watchdog, "save_state"), \
             mock.patch.object(queue_watchdog, "log"):
            queue_watchdog.main()
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
