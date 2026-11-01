import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class MaestroSparkSafetyTests(unittest.TestCase):
    def test_rejects_h3_above_qualified_frame_ceiling(self):
        from runner.maestro_safety import admission_error

        error = admission_error({
            "model_type": "minimax_h3_ref2va",
            "video_length": 364,
            "force_fps": 24,
        })
        self.assertIn("124", error)
        self.assertIn("364", error)

    def test_accepts_qualified_five_second_h3_take(self):
        from runner.maestro_safety import admission_error

        self.assertIsNone(admission_error({
            "model_type": "minimax_h3_ref2va",
            "video_length": 124,
            "force_fps": 24,
        }))

    def test_rejects_duration_only_h3_request_above_ceiling(self):
        from runner.maestro_safety import admission_error

        error = admission_error({
            "model_type": "minimax_h3_fl2va",
            "duration_seconds": 15,
            "force_fps": 24,
        })
        self.assertIn("360", error)

    def test_other_maestro_models_are_not_subject_to_h3_ceiling(self):
        from runner.maestro_safety import admission_error

        self.assertIsNone(admission_error({
            "model_type": "wan_2_2",
            "video_length": 364,
        }))

    def test_startup_reaper_targets_only_media_lab_maestro_runners(self):
        from runner.maestro_safety import reap_orphan_runners

        killed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        absent = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with mock.patch("runner.maestro_safety.subprocess.run",
                        side_effect=[killed, absent]) as run:
            result = reap_orphan_runners()
        kill_command = run.call_args_list[0].args[0]
        verify_command = run.call_args_list[1].args[0]
        self.assertEqual(kill_command[:4], ["docker", "exec", "maestro-gui", "pkill"])
        self.assertEqual(verify_command[:4], ["docker", "exec", "maestro-gui", "pgrep"])
        self.assertIn("media-lab-maestro-runner", kill_command[-1])
        self.assertEqual(result["status"], "reaped")

    def test_reaper_reports_missing_docker_without_crashing_startup(self):
        from runner.maestro_safety import reap_orphan_runners

        with mock.patch("runner.maestro_safety.subprocess.run", side_effect=FileNotFoundError("docker")):
            result = reap_orphan_runners()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["returncode"])

    def test_app_enforces_guard_on_both_maestro_routes_and_worker(self):
        app_path = Path(os.environ.get("MEDIA_LAB_APP_UNDER_TEST", ROOT / "app.py"))
        source = app_path.read_text(encoding="utf-8")
        raw_route = source[source.index('@app.post("/api/maestro")'):source.index('@app.get("/api/maestro/models")')]
        model_route = source[source.index('@app.post("/api/maestro/model")'):source.index("def run_maestro") if source.index("def run_maestro") > source.index('@app.post("/api/maestro/model")') else len(source)]
        worker = source[source.index("def run_maestro"):source.index("RUNNERS =")]
        self.assertIn("maestro_admission_error(settings)", raw_route)
        self.assertIn("maestro_admission_error(settings)", model_route)
        self.assertIn("maestro_admission_error(settings)", worker)
        self.assertIn("reap_orphan_maestro_runners()", source)

    def test_durable_container_limit_guard_and_timer(self):
        guard = (ROOT / "runner/maestro_memory_guard.sh").read_text(encoding="utf-8")
        timer = (ROOT / "config/media-lab-maestro-memory-guard.timer").read_text(encoding="utf-8")
        self.assertIn("--memory 64g --memory-swap 64g", guard)
        self.assertIn("HostConfig.Memory", guard)
        self.assertIn("OnUnitActiveSec=60", timer)
        self.assertIn("Persistent=true", timer)


if __name__ == "__main__":
    unittest.main()
