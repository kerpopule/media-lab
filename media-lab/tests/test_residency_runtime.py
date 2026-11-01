"""Integration seam tests for app.py's activity-aware residency runtime.

These tests exercise the actual _ResidencyRuntime.snapshot() wiring without
starting a service or contacting a live Qwen endpoint.  The pure parser tests
cover metric semantics; this module proves the application snapshot consumes
one injected activity result, fails closed for resident UNKNOWN, and treats
:8003 as a shim rather than a second health authority.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

# app.py intentionally binds runtime state to $HOME/media-lab-simple. Use a
# disposable complete fixture on developer machines rather than depending on
# or mutating any real Media Lab directory.
SRC = Path(__file__).resolve().parent.parent
TEST_HOME = Path(tempfile.mkdtemp(prefix="media-lab-residency-tests-"))
ROOT = TEST_HOME / "media-lab-simple"
ROOT.mkdir(parents=True)
os.environ["HOME"] = str(TEST_HOME)
for name in ("config", "static"):
    shutil.copytree(SRC / name, ROOT / name, dirs_exist_ok=True)
shutil.copy(SRC / "chat-system-prompt.md", ROOT / "chat-system-prompt.md")

import app as studio


def tearDownModule():
    shutil.rmtree(TEST_HOME, ignore_errors=True)


class ResidencyRuntimeSnapshotTests(unittest.TestCase):
    def _runtime(self, activity):
        probe = mock.Mock(return_value=activity)
        runtime = studio._ResidencyRuntime(activity_probe=probe)
        return runtime, probe

    def _patch_runtime(self, runtime, healthy):
        return mock.patch.multiple(
            studio,
            _chat_containers_running=mock.Mock(return_value=[]),
            engine_up=mock.Mock(return_value=False),
            engine_busy=mock.Mock(return_value=False),
            _mem_available_gb=mock.Mock(return_value=58.0),
        ), mock.patch.object(
            runtime, "_healthy",
            side_effect=lambda url: url == "http://127.0.0.1:8004/v1/models"
            if healthy else False,
        ), mock.patch.object(
            studio.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        )

    def test_resident_unknown_activity_is_consumed_once_and_blocks_eviction(self):
        detail = {
            "state": "unknown", "source": None, "probes": [],
            "skipped_shims": {"8003": "8004"},
            "probe_status": {"8004": {"status": "unreachable"}},
            "detail": "no canonical text endpoint answered /metrics",
            "running": 0.0, "waiting": 0.0,
        }
        runtime, probe = self._runtime(("unknown", detail))
        patches = self._patch_runtime(runtime, healthy=True)
        with patches[0], patches[1], patches[2]:
            state = runtime.snapshot()

        qwen = state["models"]["qwen"]
        self.assertTrue(qwen["resident"])
        self.assertTrue(qwen["healthy"])
        self.assertTrue(qwen["busy"])
        self.assertEqual(qwen["activity"]["state"], "unknown")
        self.assertIsNone(qwen["activity"]["source"])
        probe.assert_called_once_with()

    def test_cold_qwen_does_not_probe_and_exposes_unknown_diagnostic(self):
        runtime, probe = self._runtime(("busy", {"state": "busy", "source": "8004"}))
        patches = self._patch_runtime(runtime, healthy=False)
        with patches[0], patches[1], patches[2]:
            state = runtime.snapshot()

        qwen = state["models"]["qwen"]
        self.assertFalse(qwen["resident"])
        self.assertFalse(qwen["busy"])
        self.assertEqual(qwen["activity"]["state"], "unknown")
        self.assertIn("not resident", qwen["activity"]["detail"])
        probe.assert_not_called()

    def test_qwen_health_uses_authoritative_8004_only(self):
        runtime, _probe = self._runtime(("idle", {"state": "idle"}))
        healthy = mock.Mock(side_effect=lambda url: url == "http://127.0.0.1:8004/v1/models")
        with mock.patch.object(runtime, "_healthy", healthy):
            self.assertTrue(runtime.model_healthy("qwen"))
        healthy.assert_called_once_with("http://127.0.0.1:8004/v1/models")


class ResidencyDiagnosticsAuthTests(unittest.TestCase):
    @staticmethod
    def _request(role=None):
        request = studio.Request({
            "type": "http", "method": "GET", "path": "/api/residency",
            "headers": [], "query_string": b"", "scheme": "http",
            "server": ("test", 80), "client": ("test", 1),
        })
        if role:
            request.state.role = role
        return request

    def test_residency_diagnostics_are_not_public(self):
        response = studio.residency_state(self._request(), None)
        self.assertEqual(response.status_code, 403)

    def test_signed_user_session_can_read_non_secret_diagnostics(self):
        expected = {"status": "healthy", "actual": {"models": {}}}
        with mock.patch.object(studio.RESIDENCY, "state", return_value=expected):
            result = studio.residency_state(self._request("user"), None)
        self.assertEqual(result, expected)


class VideoResidencyAdmissionTests(unittest.TestCase):
    def test_cold_video_releases_image_weights_before_memory_planning(self):
        calls = []
        job = {}

        def release(why):
            calls.append(("release", why))
            return 27.5

        def apply(profile, slots, commit_desired):
            calls.append(("apply", profile, slots, commit_desired))
            return {"status": "committed"}

        with mock.patch.object(studio.RESIDENCY, "desired", return_value={
                "name": "qwen-ltx-default", "models": ["qwen", "ltx"], "slots": {}}), \
             mock.patch.object(studio.RESIDENCY, "snapshot", return_value={
                "models": {"h3": {"resident": False, "healthy": False}}}), \
             mock.patch.object(studio.RESIDENCY.hooks, "release_image_weights",
                               side_effect=release), \
             mock.patch.object(studio.RESIDENCY, "apply", side_effect=apply), \
             mock.patch.object(studio, "save_state"):
            result = studio.ensure_video_residency("h3", job)

        self.assertEqual("up", result)
        self.assertEqual("release", calls[0][0])
        self.assertEqual(("apply", "qwen-h3", None, False), calls[1])
        self.assertIn("preflight for h3", calls[0][1])

    def test_healthy_resident_video_does_not_release_image_weights(self):
        with mock.patch.object(studio.RESIDENCY, "desired", return_value={
                "name": "qwen-ltx-default", "models": ["qwen", "ltx"], "slots": {}}), \
             mock.patch.object(studio.RESIDENCY, "snapshot", return_value={
                "models": {"ltx": {"resident": True, "healthy": True}}}), \
             mock.patch.object(studio.RESIDENCY.hooks, "release_image_weights") as release, \
             mock.patch.object(studio.RESIDENCY, "apply", return_value={"status": "committed"}), \
             mock.patch.object(studio, "save_state"):
            result = studio.ensure_video_residency("ltx", {})

        self.assertEqual("up", result)
        release.assert_not_called()

    def test_release_failure_refuses_video_admission_loudly(self):
        job = {}
        with mock.patch.object(studio.RESIDENCY, "desired", return_value={
                "name": "qwen-ltx-default", "models": ["qwen", "ltx"], "slots": {}}), \
             mock.patch.object(studio.RESIDENCY, "snapshot", return_value={
                "models": {"h3": {"resident": False, "healthy": False}}}), \
             mock.patch.object(studio.RESIDENCY.hooks, "release_image_weights",
                               return_value=None), \
             mock.patch.object(studio.RESIDENCY, "apply") as apply, \
             mock.patch.object(studio, "save_state"):
            result = studio.ensure_video_residency("h3", job)

        self.assertEqual("busy", result)
        self.assertIn("would not release", job["detail"])
        apply.assert_not_called()


class ExactWarmVideoAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.previous_lease = studio._gpu_active_lease
        self.previous_thread_lease = getattr(studio._gpu_thread, "lease", None)
        studio._gpu_thread.lease = None

    def tearDown(self):
        studio._gpu_active_lease = self.previous_lease
        studio._gpu_thread.lease = self.previous_thread_lease

    @staticmethod
    def _lease(**changes):
        values = dict(state="active", phase="parked", job_id="prior",
                      engine="h3", task="t2va", fence=41, owner="controller",
                      pid=123, boot_id="boot-a", _fd=object())
        values.update(changes)
        return SimpleNamespace(**values)

    def test_parked_exact_warm_h3_releases_idle_image_before_retarget(self):
        lease = self._lease()
        studio._gpu_active_lease = lease
        events = []
        gate = object()

        class Protocol:
            def capacity_deficit_gib(self, engine, task, *, warm=False):
                events.append(("capacity", engine, task, warm))
                return 11.2

            def retarget(self, current, **kwargs):
                events.append("retarget")
                self_outer.assertIn("release-image", events)
                current.job_id = kwargs["job_id"]
                current.phase = "render"

            def park(self, current, proof):
                events.append("park")
                current.phase = "parked"

        self_outer = self
        with mock.patch.object(studio, "gpu_protocol", return_value=Protocol()), \
             mock.patch.object(studio, "_gpu_warm_proof", return_value={
                 "engine": "h3", "task": "t2va", "healthy": True, "busy": False}), \
             mock.patch.object(studio, "engine_up", side_effect=lambda name: name == "image"), \
             mock.patch.object(studio, "_gpu_exact_idle", side_effect=lambda name: name == "image"), \
             mock.patch.object(studio.RESIDENCY.hooks, "begin_residency_transaction",
                               side_effect=lambda: events.append("inference-lock") or gate), \
             mock.patch.object(studio.RESIDENCY.hooks, "end_residency_transaction",
                               side_effect=lambda actual: events.append(("unlock", actual))), \
             mock.patch.object(studio.RESIDENCY.hooks, "release_image_weights",
                               side_effect=lambda _why: events.append("release-image") or 11.5), \
             mock.patch.object(studio, "save_state"):
            with studio.gpu_operation("h3", "t2va", {"id": "next"}):
                events.append("body")

        self.assertLess(events.index("inference-lock"), events.index("release-image"))
        self.assertLess(events.index("release-image"), events.index("retarget"))
        self.assertLess(events.index("retarget"), events.index(("unlock", gate)))
        self.assertIn("body", events)

    def test_busy_image_fails_closed_before_release_or_retarget(self):
        lease = self._lease()
        studio._gpu_active_lease = lease

        class Protocol:
            def capacity_deficit_gib(self, *_args, **_kwargs):
                return 11.2

            def retarget(self, *_args, **_kwargs):
                raise AssertionError("must not retarget")

        with mock.patch.object(studio, "gpu_protocol", return_value=Protocol()), \
             mock.patch.object(studio, "_gpu_warm_proof", return_value={
                 "engine": "h3", "task": "t2va", "healthy": True, "busy": False}), \
             mock.patch.object(studio, "engine_up", return_value=True), \
             mock.patch.object(studio, "_gpu_exact_idle", return_value=False), \
             mock.patch.object(studio.RESIDENCY.hooks, "begin_residency_transaction",
                               return_value=object()), \
             mock.patch.object(studio.RESIDENCY.hooks, "end_residency_transaction"), \
             mock.patch.object(studio.RESIDENCY.hooks, "release_image_weights") as release:
            with self.assertRaisesRegex(studio.LeaseBusy, "not proven idle"):
                with studio.gpu_operation("h3", "t2va", {"id": "next"}):
                    self.fail("busy image must block warm admission")
        release.assert_not_called()
        self.assertEqual("parked", lease.phase)

    def test_idle_image_release_failure_stays_parked_and_never_retargets(self):
        lease = self._lease()
        studio._gpu_active_lease = lease

        protocol = mock.Mock()
        protocol.capacity_deficit_gib.return_value = 11.2
        warm = {"engine": "h3", "task": "t2va", "healthy": True, "busy": False}
        with mock.patch.object(studio, "gpu_protocol", return_value=protocol), \
             mock.patch.object(studio, "_gpu_warm_proof", return_value=warm), \
             mock.patch.object(studio, "engine_up", return_value=True), \
             mock.patch.object(studio, "_gpu_exact_idle", return_value=True), \
             mock.patch.object(studio.RESIDENCY.hooks, "begin_residency_transaction",
                               return_value=object()), \
             mock.patch.object(studio.RESIDENCY.hooks, "end_residency_transaction"), \
             mock.patch.object(studio.RESIDENCY.hooks, "release_image_weights",
                               return_value=None), \
             mock.patch.object(studio, "save_state"):
            with self.assertRaisesRegex(studio.LeaseBusy, "would not release"):
                with studio.gpu_operation("h3", "t2va", {"id": "next"}):
                    self.fail("failed release must block warm admission")
        protocol.retarget.assert_not_called()
        self.assertEqual("parked", lease.phase)

    def test_cold_unrelated_and_recovery_paths_do_not_release_image(self):
        exact = {"engine": "h3", "task": "t2va", "healthy": True, "busy": False}
        cases = (
            (None, exact, "h3", "t2va"),
            (self._lease(), {**exact, "healthy": False}, "h3", "fl2va"),
            (self._lease(state="recovery"), exact, "h3", "t2va"),
        )
        for lease, proof, engine, task in cases:
            with self.subTest(lease=lease, engine=engine, task=task):
                protocol = mock.Mock()
                with mock.patch.object(studio.RESIDENCY.hooks,
                                       "release_image_weights") as release:
                    with studio._exact_warm_video_admission(
                            protocol, lease, engine, task, proof, {}):
                        pass
                release.assert_not_called()
                protocol.capacity_deficit_gib.assert_not_called()


class OneCompanionContractTests(unittest.TestCase):
    def test_music_stands_down_every_other_idle_companion_but_never_pplx(self):
        up = {"ltx", "h3", "music", "image"}
        stopped = []
        with mock.patch.object(studio, "pplx_primary_healthy", return_value=True), \
             mock.patch.object(studio, "release_voice_weights", return_value=True) as voice, \
             mock.patch.object(studio, "engine_up", side_effect=lambda name: name in up), \
             mock.patch.object(studio, "engine_busy", return_value=False), \
             mock.patch.object(studio, "stop_engine", side_effect=stopped.append):
            result = studio.stand_down_other_companions("music", {})

        self.assertEqual("up", result)
        self.assertEqual(["ltx", "h3", "image"], stopped)
        voice.assert_called_once_with()
        self.assertNotIn("qwen", stopped)

    def test_unhealthy_pplx_refuses_companion_mutation(self):
        job = {}
        with mock.patch.object(studio, "pplx_primary_healthy", return_value=False), \
             mock.patch.object(studio, "stop_engine") as stop:
            result = studio.stand_down_other_companions("image", job)

        self.assertEqual("busy", result)
        self.assertIn("PPLX-27B", job["detail"])
        stop.assert_not_called()

    def test_busy_non_target_companion_is_never_interrupted(self):
        job = {}
        with mock.patch.object(studio, "pplx_primary_healthy", return_value=True), \
             mock.patch.object(studio, "release_voice_weights", return_value=True), \
             mock.patch.object(studio, "engine_up", return_value=True), \
             mock.patch.object(studio, "engine_busy", side_effect=lambda name: name == "h3"), \
             mock.patch.object(studio, "stop_engine") as stop:
            result = studio.stand_down_other_companions("ltx", job)

        self.assertEqual("busy", result)
        self.assertIn("h3", job["detail"])
        stop.assert_not_called()

    def test_music_and_speech_block_idle_ltx_restore(self):
        previous = studio.jobs
        try:
            for kind in ("music", "speak"):
                studio.jobs = {kind: {"kind": kind, "status": "running", "request": {}}}
                self.assertTrue(studio.video_work_pending(), kind)
        finally:
            studio.jobs = previous

    def test_pplx_is_the_only_idle_text_runtime(self):
        self.assertEqual("pplx", studio.preferred_text_runtime())

    def test_failed_post_load_pplx_probe_unwinds_idle_target(self):
        job = {}
        with mock.patch.object(studio, "pplx_primary_healthy", return_value=False), \
             mock.patch.object(studio, "engine_up", return_value=True), \
             mock.patch.object(studio, "engine_busy", return_value=False), \
             mock.patch.object(studio, "stop_engine") as stop:
            result = studio.verify_pplx_after_companion_load("music", job)

        self.assertEqual("fail", result)
        stop.assert_called_once_with("music")
        self.assertIn("became unhealthy", job["detail"])


class IdleReaperSafetyTests(unittest.TestCase):
    def test_stale_busy_h3_is_never_stopped(self):
        with mock.patch.object(studio, "engine_idle_s", return_value=studio.IDLE_REAP_S + 1), \
             mock.patch.object(studio, "engine_up", return_value=True), \
             mock.patch.object(studio, "engine_busy", side_effect=lambda name: name == "h3"), \
             mock.patch.object(studio, "stop_engine") as stop:
            studio.reap_idle_engines()

        self.assertNotIn(mock.call("h3"), stop.call_args_list)

    def test_stale_idle_h3_is_stopped(self):
        ltx_idle = {"name": "qwen-ltx-default", "slots": {}, "models": ["qwen", "ltx"]}
        with mock.patch.object(studio.RESIDENCY, "desired", return_value=ltx_idle), \
             mock.patch.object(studio, "engine_idle_s",
                               side_effect=lambda name: studio.IDLE_REAP_S + 1
                               if name == "h3" else None), \
             mock.patch.object(studio, "engine_up", return_value=True), \
             mock.patch.object(studio, "engine_busy", return_value=False), \
             mock.patch.object(studio, "stop_engine") as stop:
            studio.reap_idle_engines()

        stop.assert_called_once_with("h3")


def _pressure(available_gib=110.0, psi_full=0.0, psi_some=0.0, swap_used_gib=1.0):
    """A PressureSample as the guard reads it from /proc."""
    from media_lab_core.solh3_control_guard import PressureSample
    gib = 1024 * 1024
    return PressureSample(available_kib=int(available_gib * gib),
                          swap_total_kib=16 * gib,
                          swap_free_kib=int((16 - swap_used_gib) * gib),
                          psi_some_avg10=psi_some, psi_full_avg10=psi_full)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class AlwaysWarmH3Tests(unittest.TestCase):
    """Idle profile qwen-h3 must keep H3 warm instead of reloading it hourly."""

    QWEN_H3 = {"name": "qwen-h3", "slots": {}, "models": ["qwen", "h3"]}
    LTX_IDLE = {"name": "qwen-ltx-default", "slots": {}, "models": ["qwen", "ltx"]}

    def _reap(self, desired):
        patch_desired = (mock.patch.object(studio.RESIDENCY, "desired", side_effect=desired)
                         if isinstance(desired, Exception)
                         else mock.patch.object(studio.RESIDENCY, "desired", return_value=desired))
        with patch_desired, \
             mock.patch.object(studio, "engine_idle_s", return_value=studio.IDLE_REAP_S + 1), \
             mock.patch.object(studio, "engine_up", return_value=True), \
             mock.patch.object(studio, "engine_busy", return_value=False), \
             mock.patch.object(studio, "stop_engine") as stop:
            studio.reap_idle_engines()
        return [c.args[0] for c in stop.call_args_list]

    def test_reaper_never_unloads_h3_the_idle_profile_keeps_warm(self):
        stopped = self._reap(self.QWEN_H3)
        self.assertNotIn("h3", stopped)
        # Engines the profile does not keep are still reaped after an idle hour.
        self.assertEqual(["music", "yue2", "image"], stopped)

    def test_reaper_still_unloads_stale_h3_under_the_ltx_idle_profile(self):
        self.assertIn("h3", self._reap(self.LTX_IDLE))

    def test_unreadable_idle_profile_keeps_the_old_reaping(self):
        self.assertIn("h3", self._reap(studio.ResidencyError("desired residency state unreadable")))

    def test_idle_task_setting_accepts_only_preloadable_tasks(self):
        self.assertEqual("t2va", studio.H3_IDLE_TASK)
        self.assertEqual("fl2va", studio._h3_idle_task(" FL2VA "))
        self.assertEqual("t2va", studio._h3_idle_task("ref2va"))
        self.assertEqual("t2va", studio._h3_idle_task(""))
        self.assertEqual("t2va", studio._h3_idle_task(None))

    def test_idle_h3_preload_boots_the_text_only_task(self):
        runtime = studio._ResidencyRuntime(activity_probe=mock.Mock())
        seen = {}
        previous = getattr(studio._gpu_thread, "lease", None)

        @contextmanager
        def fake_operation(engine, task, j=None, **kwargs):
            seen["operation"] = (engine, task, j["id"], j["_gpu_task"])
            studio._gpu_thread.lease = SimpleNamespace(phase="load", engine=engine, task=task)
            try:
                yield
            finally:
                studio._gpu_thread.lease = None

        studio._gpu_thread.lease = None
        try:
            with mock.patch.object(studio, "gpu_operation", fake_operation), \
                 mock.patch.object(studio, "gpu_render_ready") as ready, \
                 mock.patch.object(studio, "_boot_engine", return_value=True) as boot:
                self.assertTrue(runtime.start_model("h3", {}))
        finally:
            studio._gpu_thread.lease = previous
        self.assertEqual(("h3", "t2va", "idle-restore-h3", "t2va"), seen["operation"])
        self.assertEqual("t2va", boot.call_args.kwargs["task"])
        ready.assert_called_once_with("h3", "t2va")

    def test_plain_text_h3_job_reuses_the_warm_idle_engine_without_reload(self):
        warm = {**studio._h3ref.required_runtime_config({}), "task": studio.H3_IDLE_TASK}
        job = {"id": "t2va-job", "kind": "video",
               "request": {"prompt": "a lighthouse at dusk", "model": "h3", "h3_turbo": False}}
        with mock.patch.object(studio, "gpu_recovery_pending", return_value=False), \
             mock.patch.object(studio, "h3_resident_config", return_value=warm), \
             mock.patch.object(studio, "stop_engine") as stop, \
             mock.patch.object(studio, "_boot_engine") as boot:
            self.assertEqual("up", studio.ensure_h3_variant(job))
        stop.assert_not_called()
        boot.assert_not_called()

    def _restore(self, desired, job_list, h3_up=False, stand_down="up"):
        previous = studio.jobs
        studio.jobs = {j["id"]: j for j in job_list}
        try:
            with mock.patch.object(studio.RESIDENCY, "desired", return_value=desired), \
                 mock.patch.object(studio.RESIDENCY, "apply", return_value={"id": "r1"}) as apply, \
                 mock.patch.object(studio, "gpu_recovery_pending", return_value=False), \
                 mock.patch.object(studio, "engine_up", side_effect=lambda name: h3_up and name == "h3"), \
                 mock.patch.object(studio, "engine_busy", return_value=False), \
                 mock.patch.object(studio, "release_voice_weights", return_value=True), \
                 mock.patch.object(studio, "release_image_weights", return_value=0.0), \
                 mock.patch.object(studio, "stand_down_other_companions",
                                   return_value=stand_down) as companions, \
                 mock.patch.object(studio, "stop_engine"):
                result = studio.restore_warm_ltx_idle()
        finally:
            studio.jobs = previous
        self.stand_down_calls = [c.args for c in companions.call_args_list]
        return result, apply

    @staticmethod
    def _image_job(finished_ago_s, **extra):
        return {"id": "img", "kind": "image", "status": "done", "request": {},
                "finished": time.time() - finished_ago_s, **extra}

    def test_h3_reload_waits_for_a_quiet_studio_after_other_gpu_work(self):
        result, apply = self._restore(self.QWEN_H3, [self._image_job(60)])
        self.assertFalse(result)
        apply.assert_not_called()

    def test_h3_reloads_once_the_quiet_period_has_passed(self):
        result, apply = self._restore(
            self.QWEN_H3, [self._image_job(studio.H3_RESTORE_QUIET_S + 5)])
        self.assertTrue(result)
        apply.assert_called_once_with("qwen-h3", None, commit_desired=False)

    def test_first_h3_restore_with_no_recent_jobs_is_not_delayed(self):
        result, apply = self._restore(self.QWEN_H3, [])
        self.assertTrue(result)
        apply.assert_called_once_with("qwen-h3", None, commit_desired=False)

    def test_quiet_period_does_not_delay_the_ltx_idle_profile(self):
        result, apply = self._restore(self.LTX_IDLE, [self._image_job(60)])
        self.assertTrue(result)
        apply.assert_called_once_with("qwen-ltx-default", None, commit_desired=False)

    def test_quiet_period_does_not_apply_while_h3_is_already_warm(self):
        result, apply = self._restore(self.QWEN_H3, [self._image_job(60)], h3_up=True)
        self.assertTrue(result)
        apply.assert_called_once_with("qwen-h3", None, commit_desired=False)

    def test_cloud_jobs_do_not_count_as_local_gpu_work(self):
        cloud = {"id": "fal", "kind": "video", "status": "done", "engine": "fal-kling",
                 "request": {"engine": "fal-kling"}, "finished": time.time() - 30}
        result, apply = self._restore(self.QWEN_H3, [cloud])
        self.assertTrue(result)
        apply.assert_called_once_with("qwen-h3", None, commit_desired=False)

    def test_h3_restore_stands_idle_companions_down_like_an_h3_job(self):
        result, apply = self._restore(self.QWEN_H3, [])
        self.assertTrue(result)
        self.assertEqual([("h3",)], self.stand_down_calls)
        apply.assert_called_once_with("qwen-h3", None, commit_desired=False)

    def test_h3_restore_waits_when_a_companion_will_not_stand_down(self):
        result, apply = self._restore(self.QWEN_H3, [], stand_down="busy")
        self.assertFalse(result)
        apply.assert_not_called()

    def test_ltx_idle_restore_does_not_stand_companions_down(self):
        result, apply = self._restore(self.LTX_IDLE, [])
        self.assertTrue(result)
        self.assertEqual([], self.stand_down_calls)

    def test_warm_h3_restore_does_not_stand_companions_down(self):
        result, apply = self._restore(self.QWEN_H3, [], h3_up=True)
        self.assertTrue(result)
        self.assertEqual([], self.stand_down_calls)

    def test_queued_local_work_blocks_the_h3_restore(self):
        queued = {"id": "img2", "kind": "image", "status": "queued", "request": {}}
        result, apply = self._restore(self.QWEN_H3, [queued])
        self.assertFalse(result)
        apply.assert_not_called()


class H3QueueBatchingTests(unittest.TestCase):
    """Run the real pick_next_job source: other test modules stub the attribute."""

    @staticmethod
    def _source_function(name, **namespace):
        import ast
        tree = ast.parse((SRC / "app.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SRC / "app.py"), "exec"),
             namespace)
        return namespace[name]

    @staticmethod
    def _jobs(now):
        return {
            "img": {"id": "img", "kind": "image", "status": "queued", "request": {}, "ts": now - 30},
            "h3a": {"id": "h3a", "kind": "video", "status": "queued",
                    "request": {"model": "h3"}, "ts": now - 20},
            "ltx": {"id": "ltx", "kind": "video", "status": "queued",
                    "request": {"model": "ltx"}, "ts": now - 15},
            "h3b": {"id": "h3b", "kind": "video", "status": "queued",
                    "request": {"model": "h3"}, "ts": now - 10},
        }

    def _drain(self, resident, keep_h3_warm, jobs, order, variant=None):
        queue = list(order)
        job_engine = self._source_function("job_engine")
        pick = self._source_function(
            "pick_next_job", queue=queue, jobs=jobs, VIDEO_ENGINE_NAMES=("ltx", "h3"),
            engine_up=lambda name: name == resident, job_engine=job_engine,
            h3_kept_warm=lambda: keep_h3_warm, time=time,
            H3_BATCH_MAX_WAIT_S=studio.H3_BATCH_MAX_WAIT_S,
            h3_resident_config=lambda: ({"variant": variant} if variant else None),
            _h3ref=studio._h3ref)
        picked = []
        for _ in range(len(order) + 1):
            if not queue:
                break
            picked.append(pick())
        self.assertEqual([], queue, "picker did not drain the queue")
        return picked

    def test_warm_h3_runs_every_queued_h3_take_before_anything_that_evicts_it(self):
        picked = self._drain("h3", True, self._jobs(time.time()),
                             ["img", "h3a", "ltx", "h3b"])
        self.assertEqual(["h3a", "h3b"], picked[:2])

    def test_resident_real_long_runs_its_own_takes_before_switching_back_to_sol(self):
        now = time.time()
        jobs = self._jobs(now)
        jobs["real1"] = {"id": "real1", "kind": "video", "status": "queued", "ts": now - 12,
                         "request": {"model": "h3", "h3_engine": "singularity"}}
        jobs["real2"] = {"id": "real2", "kind": "video", "status": "queued", "ts": now - 5,
                         "request": {"model": "h3", "h3_engine": "singularity"}}
        picked = self._drain("h3", True, jobs, ["h3a", "real1", "img", "h3b", "real2"],
                             variant="singularity")
        self.assertEqual(["real1", "real2", "h3a", "h3b"], picked[:4])
        # and a resident Sol keeps its own takes first
        picked = self._drain("h3", True, jobs, ["real1", "h3a", "real2", "h3b"], variant="fl2va")
        self.assertEqual(["h3a", "h3b", "real1", "real2"], picked)

    def test_pushed_out_h3_finishes_other_work_before_reloading(self):
        picked = self._drain(None, True, self._jobs(time.time()),
                             ["h3a", "img", "h3b", "ltx"])
        self.assertEqual(["img", "ltx", "h3a", "h3b"], picked)

    def test_an_h3_take_that_waited_too_long_goes_next(self):
        now = time.time()
        jobs = self._jobs(now)
        jobs["h3a"]["ts"] = now - studio.H3_BATCH_MAX_WAIT_S - 1
        picked = self._drain(None, True, jobs, ["img", "h3a", "ltx"])
        self.assertEqual("h3a", picked[0])

    def test_ltx_idle_profile_keeps_plain_queue_order_when_nothing_is_resident(self):
        picked = self._drain(None, False, self._jobs(time.time()),
                             ["h3a", "img", "h3b", "ltx"])
        self.assertEqual(["h3a", "img", "h3b", "ltx"], picked)

    def test_resident_ltx_still_groups_ltx_takes_first(self):
        picked = self._drain("ltx", True, self._jobs(time.time()),
                             ["h3a", "img", "ltx", "h3b"])
        self.assertEqual(["img", "ltx"], picked[:2])

    def test_h3_kept_warm_follows_the_idle_profile(self):
        qwen_h3 = {"name": "qwen-h3", "slots": {}, "models": ["qwen", "h3"]}
        ltx = {"name": "qwen-ltx-default", "slots": {}, "models": ["qwen", "ltx"]}
        with mock.patch.object(studio.RESIDENCY, "desired", return_value=qwen_h3):
            self.assertTrue(studio.h3_kept_warm())
        with mock.patch.object(studio.RESIDENCY, "desired", return_value=ltx):
            self.assertFalse(studio.h3_kept_warm())
        with mock.patch.object(studio.RESIDENCY, "desired",
                               side_effect=studio.ResidencyError("unreadable")):
            self.assertFalse(studio.h3_kept_warm())


class H3LoadHeadroomTests(unittest.TestCase):
    def _wait(self, samples, max_wait=120.0, j=None, avg60=None):
        clock = _Clock()
        feed = iter(samples)
        last = {}
        feed60 = iter(avg60 or [])
        last60 = {"v": None}

        def read():
            try:
                last["s"] = next(feed)
            except StopIteration:
                pass
            return last["s"]

        def read60():
            try:
                last60["v"] = next(feed60)
            except StopIteration:
                pass
            return last60["v"]

        with mock.patch.object(studio, "H3_LOAD_SETTLE_MAX_WAIT_S", max_wait):
            return studio.wait_for_h3_load_headroom(j, read=read, sleep=clock.sleep,
                                                    clock=clock, read_avg60=read60,
                                                    flush=lambda: None), clock

    def test_calm_steady_memory_admits_after_a_few_samples(self):
        result, clock = self._wait([_pressure()] * 20)
        self.assertTrue(result["settled"])
        self.assertLessEqual(clock.now - 1000.0, studio.H3_LOAD_SETTLE_SAMPLES + 2)

    def test_needs_ten_steady_seconds_by_default(self):
        self.assertGreaterEqual(studio.H3_LOAD_SETTLE_SAMPLES, 10)
        result, clock = self._wait([_pressure()] * 20)
        self.assertGreaterEqual(clock.now - 1000.0, studio.H3_LOAD_SETTLE_SAMPLES)

    def test_a_busy_last_minute_is_waited_out_even_when_avg10_is_calm(self):
        # 2026-09-26 02:41: image jobs just finished; avg10 calm, avg60 still high.
        result, clock = self._wait([_pressure()] * 60, avg60=[12.0] * 30 + [1.0] * 30)
        self.assertTrue(result["settled"])
        self.assertGreaterEqual(clock.now - 1000.0, 30)
        self.assertEqual(result["psi_full_avg60"], 1.0)

    def test_no_avg60_data_does_not_block(self):
        result, _clock = self._wait([_pressure()] * 20, avg60=[None])
        self.assertTrue(result["settled"])

    def test_dirty_pages_are_flushed_once_before_waiting(self):
        calls = []
        clock = _Clock()
        studio.wait_for_h3_load_headroom(read=lambda: _pressure(), sleep=clock.sleep, clock=clock,
                                         read_avg60=lambda: 0.0, flush=lambda: calls.append(1))
        self.assertEqual(calls, [1])

    def test_memory_still_being_returned_is_waited_out(self):
        rising = [_pressure(available_gib=60 + 10 * i) for i in range(6)]
        result, clock = self._wait(rising + [_pressure(available_gib=115)] * 5)
        self.assertTrue(result["settled"])
        self.assertGreaterEqual(clock.now - 1000.0, 6)

    def test_high_pressure_is_waited_out(self):
        busy = [_pressure(psi_full=30.0)] * 8
        result, clock = self._wait(busy + [_pressure(psi_full=1.0)] * 5)
        self.assertTrue(result["settled"])
        self.assertGreaterEqual(clock.now - 1000.0, 8)

    def test_wait_is_bounded_and_says_it_did_not_settle(self):
        result, clock = self._wait([_pressure(psi_full=40.0)], max_wait=30.0)
        self.assertFalse(result["settled"])
        self.assertLessEqual(clock.now - 1000.0, 31)

    def test_missing_pressure_data_does_not_block(self):
        def broken():
            raise FileNotFoundError("/proc/pressure/memory")
        result = studio.wait_for_h3_load_headroom(read=broken, sleep=lambda _s: None)
        self.assertIsNone(result["settled"])

    def test_cancelled_job_stops_waiting(self):
        result, _clock = self._wait([_pressure(psi_full=40.0)], j={"cancel": True})
        self.assertIsNone(result["settled"])


class H3LoadPressureRecordTests(unittest.TestCase):
    def test_record_tracks_peaks_floor_swap_and_runs_at_the_guard_limit(self):
        recorder = studio.H3LoadPressureRecorder(task="t2va", job_id="idle-restore-h3",
                                                 read=lambda: _pressure())
        recorder.row["guard_psi_limit"] = 50.0
        for full, avail, swap in ((0, 110, 1.0), (55, 40, 2.0), (60, 25, 3.5),
                                  (20, 30, 3.0), (51, 26, 2.5), (52, 27, 2.5)):
            recorder.observe(_pressure(available_gib=avail, psi_full=full,
                                       psi_some=full + 1, swap_used_gib=swap))
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "h3-load-pressure.jsonl"
            row = recorder.finish("ready", settle={"settled": True}, path=log)
            written = [line for line in log.read_text().splitlines() if line]
        self.assertEqual(1, len(written))
        self.assertEqual(60.0, row["peak_psi_full_avg10"])
        self.assertEqual(25.0, row["min_available_gib"])
        self.assertEqual(2.5, row["max_swap_growth_gib"])
        self.assertEqual(2, row["longest_run_at_guard_psi"])
        self.assertEqual("ready", row["outcome"])
        self.assertEqual({"settled": True}, row["settle"])

    def _boot_h3(self, guard_answers, engine_answers):
        recorded = []

        class FakeRecorder:
            def __init__(self, task=None, job_id=None, **kwargs):
                self.task = task

            def start(self):
                return self

            def finish(self, outcome, settle=None, path=None):
                recorded.append((self.task, outcome, settle))

        previous = getattr(studio._gpu_thread, "lease", None)
        studio._gpu_thread.lease = SimpleNamespace(phase="load", engine="h3", task="t2va")
        guard = iter(guard_answers)
        up = iter(engine_answers)
        live = {"variant": "fl2va", "task": "t2va", "turbo_preset": None}
        sol = {"SOL_PKG": "/opt/sol", "SOL_ROOT": "/opt/sol-root",
               "SOL_H3_SPARK_QWEN_IMAGE": "qwen-image", "SOL_H3_SPARK_QWEN_WEIGHTS_ROOT": "/w"}
        try:
            with mock.patch.object(studio, "H3LoadPressureRecorder", FakeRecorder), \
                 mock.patch.object(studio, "gpu_recovery_pending", return_value=False), \
                 mock.patch.object(studio, "sol_h3_control_guard_ready",
                                   side_effect=lambda: next(guard)), \
                 mock.patch.object(studio, "engine_up", side_effect=lambda _n: next(up)), \
                 mock.patch.object(studio, "wait_for_h3_load_headroom",
                                   return_value={"settled": True}), \
                 mock.patch.object(studio, "delegation_env", return_value={}), \
                 mock.patch.object(studio.local_config, "sol", return_value=sol), \
                 mock.patch.object(studio, "write_runtime_environment"), \
                 mock.patch.object(studio.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0)), \
                 mock.patch.object(studio, "http_json", return_value={"loaded": True, **live}), \
                 mock.patch.object(studio, "h3_resident_config", return_value=live), \
                 mock.patch.object(studio, "touch_engine"), \
                 mock.patch.object(studio, "stop_engine") as stop, \
                 mock.patch.object(studio, "maybe_release_pool"), \
                 mock.patch.object(studio.time, "sleep"):
                result = studio._boot_engine("h3", None, variant="fl2va", task="t2va")
        finally:
            studio._gpu_thread.lease = previous
        return result, recorded, stop

    def test_h3_cold_load_is_recorded_when_it_becomes_ready(self):
        # guard: admission, post-settle recheck, first loop pass; engine: cold, then up
        result, recorded, stop = self._boot_h3([True, True, True], [False, True])
        self.assertTrue(result)
        self.assertEqual([("t2va", "ready", {"settled": True})], recorded)
        stop.assert_not_called()

    def test_h3_cold_load_is_recorded_when_the_guard_stops_it(self):
        result, recorded, stop = self._boot_h3([True, True, False], [False])
        self.assertFalse(result)
        self.assertEqual([("t2va", "guard-lost", {"settled": True})], recorded)
        stop.assert_called_once_with("h3")

    def test_settle_runs_before_the_h3_unit_starts_and_rechecks_the_guard(self):
        previous = getattr(studio._gpu_thread, "lease", None)
        studio._gpu_thread.lease = SimpleNamespace(phase="load", engine="h3", task="t2va")
        order = []
        guard = iter([True, False])
        try:
            with mock.patch.object(studio, "gpu_recovery_pending", return_value=False), \
                 mock.patch.object(studio, "engine_up", return_value=False), \
                 mock.patch.object(studio, "sol_h3_control_guard_ready",
                                   side_effect=lambda: order.append("guard") or next(guard)), \
                 mock.patch.object(studio, "wait_for_h3_load_headroom",
                                   side_effect=lambda j=None: order.append("settle") or
                                   {"settled": True}), \
                 mock.patch.object(studio, "H3LoadPressureRecorder") as recorder, \
                 mock.patch.object(studio.subprocess, "run") as run:
                self.assertFalse(studio._boot_engine("h3", None, task="t2va"))
        finally:
            studio._gpu_thread.lease = previous
        self.assertEqual(["guard", "settle", "guard"], order)
        run.assert_not_called()
        recorder.assert_not_called()


class RestoreRenderSerializationTests(unittest.TestCase):
    def test_queued_job_cannot_start_inside_active_idle_restore_transaction(self):
        previous_jobs = studio.jobs
        previous_runner = studio.RUNNERS.get("race-test")
        started = threading.Event()
        finished = threading.Event()
        restore_has_lock = threading.Event()
        release_restore = threading.Event()
        job = {"id": "race", "kind": "race-test", "status": "queued", "request": {}}

        def runner(j):
            started.set()
            j["status"] = "done"
            j["stage"] = "done"

        def hold_restore_transaction():
            with studio._idle_restore_mutex:
                restore_has_lock.set()
                release_restore.wait(2)

        def run_job():
            studio.run_queued_job("race")
            finished.set()

        try:
            studio.jobs = {"race": job}
            studio.RUNNERS["race-test"] = runner
            # This test proves mutex ordering, not live GPU admission. Supply an
            # admitted disposable fixture even when background workers are off.
            with mock.patch.object(studio, "_gpu_cutover_ready", True), \
                 mock.patch.object(studio, "gpu_recovery_pending", return_value=False), \
                 mock.patch.object(studio, "_gpu_finish_job_operation"), \
                 mock.patch.object(studio, "save_state"), \
                 mock.patch.object(studio, "eta_record"), \
                 mock.patch.object(studio, "notify_done"), \
                 mock.patch.object(studio, "settle_video_transaction"):
                restore = threading.Thread(target=hold_restore_transaction)
                restore.start()
                self.assertTrue(restore_has_lock.wait(1))
                render = threading.Thread(target=run_job)
                render.start()
                self.assertFalse(started.wait(0.1), "render entered while restore held the transaction")
                release_restore.set()
                self.assertTrue(started.wait(1))
                self.assertTrue(finished.wait(1))
                restore.join(1)
                render.join(1)
        finally:
            studio.jobs = previous_jobs
            if previous_runner is None:
                studio.RUNNERS.pop("race-test", None)
            else:
                studio.RUNNERS["race-test"] = previous_runner


class MaestroExclusiveResidencyTests(unittest.TestCase):
    def tearDown(self):
        studio.CHAT_PAUSE_RECEIPT.unlink(missing_ok=True)
        studio.CHAT_MAINTENANCE.unlink(missing_ok=True)

    def test_stale_pause_receipt_rechecks_and_stops_live_qwen(self):
        studio.CHAT_PAUSE_RECEIPT.parent.mkdir(parents=True, exist_ok=True)
        studio.CHAT_PAUSE_RECEIPT.write_text(
            '{"containers":["qwen38-vllm"],"ts":1}')
        live = [["qwen38-vllm"], ["qwen38-vllm"], []]
        with mock.patch.object(studio, "_chat_containers_running",
                               side_effect=lambda: live.pop(0) if live else []), \
             mock.patch.object(studio, "save_state"), \
             mock.patch.object(studio.subprocess, "run") as run:
            self.assertTrue(studio.pause_chat_for_video({}))
        self.assertTrue(studio.CHAT_MAINTENANCE.exists())
        run.assert_called_once_with(
            ["docker", "stop", "--time", "45", "qwen38-vllm"],
            stdout=studio.subprocess.DEVNULL, stderr=studio.subprocess.DEVNULL)

    def test_chat_container_inspection_failure_is_not_treated_as_absent(self):
        failed = subprocess.CompletedProcess(
            ["docker", "ps"], 2, stdout="", stderr="daemon unavailable"
        )
        with mock.patch.object(studio.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "inspection failed"):
                studio._chat_containers_running()

    def test_maestro_pause_is_inside_fenced_unload_before_reclaim_and_load(self):
        source = (SRC / "app.py").read_text()
        block = source[source.index("def gpu_operation"):source.index("def gpu_render_ready")]
        unload_at = block.index('protocol.advance(lease, "unload")')
        pause_at = block.index("pause_chat_for_video(j)")
        reclaim_at = block.index("reclaim_proof = reclaim_operation()")
        load_at = block.index('protocol.advance(lease, "load")')
        self.assertLess(unload_at, pause_at)
        self.assertLess(pause_at, reclaim_at)
        self.assertLess(reclaim_at, load_at)

        maestro = source[source.index("def run_maestro"):source.index("def run_maestro_fenced")]
        self.assertNotIn("pause_chat_for_video(j)", maestro)

    def test_maestro_capacity_failure_restores_managed_qwen(self):
        lease = SimpleNamespace(
            state="active", phase="drain", job_id="maestro-job",
            engine="maestro", task="generate", _fd=None,
        )
        events = []

        class Protocol:
            def acquire(self, **_kwargs):
                return lease

            def advance(self, current, phase):
                events.append(phase)
                if phase == "load":
                    raise studio.CapacityUnqualified("fixture capacity")
                current.phase = phase
                return current

            def release(self, current, proof):
                events.append("release")
                current.state = "released"

        previous = studio._gpu_active_lease
        studio._gpu_active_lease = None
        try:
            with mock.patch.object(studio, "gpu_protocol", return_value=Protocol()), \
                 mock.patch.object(studio, "pool_cmd", return_value="OK"), \
                 mock.patch.object(studio, "_gpu_warm_proof", return_value={
                     "healthy": False, "busy": False}), \
                 mock.patch.object(studio, "pause_chat_for_video", return_value=True) as pause, \
                 mock.patch.object(studio, "reap_orphan_maestro_runners", return_value={
                     "status": "clean", "returncode": 1, "detail": ""}), \
                 mock.patch.object(studio, "_gpu_reclaim_all", return_value={
                     "processes_gone": True, "memory_recovered": True,
                     "available_gib": 120.0, "survivors": []}), \
                 mock.patch.object(studio, "restore_chat_after_video", return_value=True) as restore:
                with self.assertRaises(studio.CapacityUnqualified):
                    with studio.gpu_operation("maestro", "generate", {"id": "maestro-job"},
                                              ephemeral=True):
                        self.fail("capacity failure must happen before render admission")
        finally:
            studio._gpu_active_lease = previous

        pause.assert_called_once()
        restore.assert_called_once_with()
        self.assertEqual(["unload", "reclaim", "load", "release"], events)

    def test_maestro_pause_failure_restores_receipt_and_never_reclaims_or_loads(self):
        lease = SimpleNamespace(
            state="active", phase="drain", job_id="maestro-job",
            engine="maestro", task="generate", _fd=None,
        )
        events = []

        class Protocol:
            def acquire(self, **_kwargs):
                return lease

            def advance(self, current, phase):
                events.append(phase)
                current.phase = phase
                return current

            def mark_recovery(self, current, _reason):
                current.state = "recovery"

            def release(self, current, proof):
                events.append("release")
                current.state = "released"

        previous = studio._gpu_active_lease
        studio._gpu_active_lease = None
        try:
            with mock.patch.object(studio, "gpu_protocol", return_value=Protocol()), \
                 mock.patch.object(studio, "pool_cmd", return_value="OK"), \
                 mock.patch.object(studio, "_gpu_warm_proof", return_value={
                     "healthy": False, "busy": False}), \
                 mock.patch.object(studio, "pause_chat_for_video", return_value=False), \
                 mock.patch.object(studio, "reap_orphan_maestro_runners", return_value={
                     "status": "clean", "returncode": 1, "detail": ""}), \
                 mock.patch.object(studio, "_gpu_reclaim_all", return_value={
                     "processes_gone": True, "memory_recovered": True,
                     "available_gib": 120.0, "survivors": []}) as reclaim, \
                 mock.patch.object(studio, "restore_chat_after_video", return_value=True) as restore, \
                 mock.patch.object(studio, "hold_gpu_recovery"):
                with self.assertRaisesRegex(studio.LeaseBusy, "pause"):
                    with studio.gpu_operation("maestro", "generate", {"id": "maestro-job"},
                                              ephemeral=True):
                        self.fail("pause failure must happen before render admission")
        finally:
            studio._gpu_active_lease = previous

        restore.assert_called_once_with()
        reclaim.assert_called_once_with({"id": "maestro-job"})
        self.assertEqual(["unload", "reclaim", "release"], events)

    def test_maestro_render_exception_reaps_runner_before_qwen_restore_and_release(self):
        lease = SimpleNamespace(
            state="active", phase="drain", job_id="maestro-job",
            engine="maestro", task="generate", _fd=None,
        )
        events = []

        class Protocol:
            def acquire(self, **_kwargs):
                return lease

            def advance(self, current, phase):
                events.append(phase)
                current.phase = phase
                return current

            def bind_process(self, *_args, **_kwargs):
                return None

            def mark_recovery(self, current, _reason):
                events.append("recovery")
                current.state = "recovery"

            def release(self, current, proof):
                events.append("release")
                current.state = "released"

        previous = studio._gpu_active_lease
        studio._gpu_active_lease = None
        try:
            with mock.patch.object(studio, "gpu_protocol", return_value=Protocol()), \
                 mock.patch.object(studio, "pool_cmd", return_value="OK"), \
                 mock.patch.object(studio, "_gpu_warm_proof", return_value={
                     "healthy": False, "busy": False}), \
                 mock.patch.object(studio, "pause_chat_for_video", return_value=True), \
                 mock.patch.object(studio, "_gpu_reclaim_all", side_effect=lambda _job=None: (
                     events.append("reclaim-proof") or {
                         "processes_gone": True, "memory_recovered": True,
                         "available_gib": 120.0, "survivors": []})), \
                 mock.patch.object(studio, "_gpu_process_identity", return_value=None), \
                 mock.patch.object(studio, "reap_orphan_maestro_runners",
                                   side_effect=lambda: (events.append("maestro-reap") or {
                                       "status": "clean", "returncode": 1, "detail": ""})), \
                 mock.patch.object(studio, "restore_chat_after_video",
                                   side_effect=lambda: (events.append("restore") or True)) as restore, \
                 mock.patch.object(studio, "hold_gpu_recovery"):
                with self.assertRaisesRegex(RuntimeError, "fixture render failure"):
                    with studio.gpu_operation("maestro", "generate", {"id": "maestro-job"},
                                              ephemeral=True):
                        studio.gpu_render_ready("maestro", "generate")
                        raise RuntimeError("fixture render failure")
        finally:
            studio._gpu_active_lease = previous

        restore.assert_called_once_with()
        self.assertEqual("released", lease.state)
        self.assertLess(len(events) - 1 - events[::-1].index("maestro-reap"), events.index("restore"))
        self.assertNotIn("recovery", events)

    def test_maestro_invalid_final_reclaim_never_restores_qwen(self):
        lease = SimpleNamespace(
            state="active", phase="drain", job_id="maestro-job",
            engine="maestro", task="generate", _fd=None,
        )

        class Protocol:
            def acquire(self, **_kwargs):
                return lease

            def advance(self, current, phase):
                current.phase = phase
                return current

            def bind_process(self, *_args, **_kwargs):
                return None

            def mark_recovery(self, current, _reason):
                current.state = "recovery"

        good = {
            "processes_gone": True, "memory_recovered": True,
            "available_gib": 120.0, "survivors": [],
        }
        bad = {
            "processes_gone": False, "memory_recovered": False,
            "available_gib": 12.0, "survivors": ["h3"],
        }
        previous = studio._gpu_active_lease
        studio._gpu_active_lease = None
        try:
            with mock.patch.object(studio, "gpu_protocol", return_value=Protocol()), \
                 mock.patch.object(studio, "pool_cmd", return_value="OK"), \
                 mock.patch.object(studio, "_gpu_warm_proof", return_value={
                     "healthy": False, "busy": False}), \
                 mock.patch.object(studio, "pause_chat_for_video", return_value=True), \
                 mock.patch.object(studio, "reap_orphan_maestro_runners", return_value={
                     "status": "clean", "returncode": 1, "detail": ""}), \
                 mock.patch.object(studio, "_gpu_reclaim_all", side_effect=[good, bad]), \
                 mock.patch.object(studio, "_gpu_process_identity", return_value=None), \
                 mock.patch.object(studio, "restore_chat_after_video") as restore, \
                 mock.patch.object(studio, "hold_gpu_recovery"):
                with self.assertRaisesRegex(RuntimeError, "survived final reclaim"):
                    with studio.gpu_operation("maestro", "generate", {"id": "maestro-job"},
                                              ephemeral=True):
                        studio.gpu_render_ready("maestro", "generate")
        finally:
            studio._gpu_active_lease = previous

        restore.assert_not_called()
        self.assertEqual("recovery", lease.state)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class WarmSettleTests(unittest.TestCase):
    """A small post-render memory deficit is waited out, not failed."""

    def test_small_deficit_is_waited_out_before_retarget(self):
        protocol = mock.Mock()
        protocol.capacity_deficit_gib.side_effect = [1.3, 0.8, 0.0]
        lease = SimpleNamespace(state="active", phase="parked", engine="h3", task="t2va")
        warm = {"engine": "h3", "task": "t2va", "healthy": True, "busy": False}
        sleeps = []
        job = {"id": "next"}
        with mock.patch.object(studio, "engine_up", return_value=False), \
             mock.patch.object(studio, "_settle_sleep", side_effect=sleeps.append), \
             mock.patch.object(studio, "save_state"), \
             mock.patch.object(studio.RESIDENCY.hooks, "release_image_weights") as release:
            with studio._exact_warm_video_admission(protocol, lease, "h3", "t2va", warm, job):
                pass
        self.assertEqual(sleeps, [2.0, 2.0])
        self.assertEqual(protocol.capacity_deficit_gib.call_count, 3)
        release.assert_not_called()
        self.assertIn("settle", job["stage"])

    def test_wait_is_bounded_and_large_deficits_are_not_waited(self):
        protocol = mock.Mock()
        protocol.capacity_deficit_gib.return_value = 1.0
        clock = iter([0.0, 10.0, 200.0])
        with mock.patch.object(studio, "_settle_sleep"), \
             mock.patch.object(studio, "_settle_clock", side_effect=lambda: next(clock)), \
             mock.patch.object(studio, "save_state"):
            self.assertEqual(studio._wait_for_warm_settle(protocol, "h3", "t2va", 1.0, {}), 1.0)
        protocol.capacity_deficit_gib.reset_mock()
        self.assertEqual(studio._wait_for_warm_settle(protocol, "h3", "t2va", 11.2, None), 11.2)
        protocol.capacity_deficit_gib.assert_not_called()
