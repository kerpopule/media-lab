"""A memory refusal inside the GPU lease, before anything loaded, releases the
lease cleanly; a failure after loading still quarantines (2026-09-26 22:57:
"h3 decode requires 117.0 GiB ...; 116.7 GiB would be available" left an
operation-uncertain hold that needed a person, although nothing had started).

These drive the real ``gpu_operation``/``ensure_engine`` against the real
durable protocol in a temporary database; only the engine and process probes
are simulated. No memory pressure is involved.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent
TEST_HOME = Path(tempfile.mkdtemp(prefix="media-lab-preload-refusal-"))
ROOT = TEST_HOME / "media-lab-simple"
ROOT.mkdir(parents=True)
os.environ["HOME"] = str(TEST_HOME)
for name in ("config", "static"):
    shutil.copytree(SRC / name, ROOT / name, dirs_exist_ok=True)
shutil.copy(SRC / "chat-system-prompt.md", ROOT / "chat-system-prompt.md")

import app as studio
from media_lab_core.durable_gpu_protocol import DurableGpuProtocol, LeaseBusy
from residency import ResidencyRefused, plan_residency

REFUSAL = "h3 decode requires 117.0 GiB including the 3.0 GiB floor; 116.7 GiB would be available"
BOOT = "boot-sim"


def tearDownModule():
    shutil.rmtree(TEST_HOME, ignore_errors=True)


def clean_proof(**extra):
    return {"processes_gone": True, "memory_recovered": True, "available_gib": 116.8,
            "survivors": [], "boot_id": BOOT, **extra}


class PreloadRefusalLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gpu-proto-"))
        self.protocol = DurableGpuProtocol(self.tmp / "gpu.sqlite3", self.tmp / "gpu.lock",
                                           boot_id=lambda: BOOT, available_gib=lambda: 120.0,
                                           pid_alive=lambda _pid: True)
        self.protocol.qualify("h3", "t2va", peak_gib=104.0, reserve_gib=12.0, evidence="fixture")
        # never the real pool/: app may already be imported by another module
        hold = mock.patch.object(studio, "GPU_RECOVERY_HOLD", self.tmp / "gpu-recovery-hold.json")
        hold.start()
        self.addCleanup(hold.stop)
        self._reset_studio()

    def tearDown(self):
        self._reset_studio()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _reset_studio():
        studio.GPU_RECOVERY_HOLD.unlink(missing_ok=True)
        studio._gpu_recovery_blocked = False
        studio._gpu_active_lease = None
        for name in ("lease", "job_context", "job_target", "preload_refusal"):
            setattr(studio._gpu_thread, name, None)

    def _run(self, under_lease, *, reclaim, job=True):
        j = {"id": "job-sim", "kind": "video", "request": {"model": "h3"}} if job else None
        with mock.patch.object(studio, "gpu_protocol", return_value=self.protocol), \
             mock.patch.object(studio, "pool_cmd", return_value="OK") as pool, \
             mock.patch.object(studio, "_gpu_task_for_engine", return_value="t2va"), \
             mock.patch.object(studio, "_gpu_warm_proof", return_value={"healthy": False, "busy": False}), \
             mock.patch.object(studio, "_gpu_reclaim_all", side_effect=reclaim) as reclaim_all, \
             mock.patch.object(studio, "_ensure_engine_under_lease", side_effect=under_lease), \
             mock.patch.object(studio, "save_state"):
            state = studio.ensure_engine("h3", j)
        return state, j, pool, reclaim_all

    @staticmethod
    def _residency_refuses(name, j=None):
        # exactly what ensure_video_residency does on a phase-floor plan refusal
        studio._gpu_thread.preload_refusal = None
        studio._note_preload_refusal(ResidencyRefused(REFUSAL, [{"kind": "phase-floor"}]))
        return "busy"

    def test_memory_refusal_before_load_releases_lease_without_hold(self):
        state, j, pool, reclaim_all = self._run(self._residency_refuses,
                                                reclaim=[clean_proof(), clean_proof()])
        self.assertEqual("busy", state)
        self.assertIsNone(self.protocol.snapshot()["lease"])
        self.assertIsNone(studio._gpu_active_lease)
        self.assertFalse(studio.GPU_RECOVERY_HOLD.exists())
        self.assertFalse(studio.gpu_recovery_pending())
        self.assertNotIn("recovery_required", j)
        # a fresh reclaim proof after the refusal, then the legacy pool restored
        self.assertEqual(2, reclaim_all.call_count)
        self.assertEqual(mock.call("acquire"), pool.call_args_list[-1])

    def test_non_job_admission_refusal_also_releases(self):
        state, _j, _pool, _reclaim = self._run(self._residency_refuses,
                                               reclaim=[clean_proof(), clean_proof()], job=False)
        self.assertEqual("busy", state)
        self.assertIsNone(self.protocol.snapshot()["lease"])
        self.assertFalse(studio.gpu_recovery_pending())

    def test_refusal_with_unproven_box_still_holds(self):
        survivor = clean_proof(processes_gone=False, memory_recovered=False, survivors=["h3"])
        state, j, _pool, _reclaim = self._run(self._residency_refuses, reclaim=[clean_proof(), survivor])
        self.assertEqual("busy", state)
        self.assertTrue(studio.GPU_RECOVERY_HOLD.exists())
        lease = self.protocol.snapshot()["lease"]
        self.assertEqual("recovery", lease["state"])
        self.assertTrue(lease["reason"].startswith("preload-refusal-cleanup-uncertain:"))
        self.assertTrue(j["recovery_required"])

    def test_refusal_with_memory_not_back_still_holds(self):
        low = clean_proof(memory_recovered=False, available_gib=20.0)
        self._run(self._residency_refuses, reclaim=[clean_proof(), low])
        self.assertTrue(studio.GPU_RECOVERY_HOLD.exists())
        self.assertEqual("recovery", self.protocol.snapshot()["lease"]["state"])

    def test_unexplained_busy_keeps_the_existing_uncertain_hold(self):
        # no recorded pre-load refusal: outcome unknown, safety path unchanged
        state, j, _pool, reclaim_all = self._run(lambda name, j=None: "busy",
                                                 reclaim=[clean_proof(), clean_proof()])
        self.assertEqual("busy", state)
        self.assertEqual(1, reclaim_all.call_count)
        lease = self.protocol.snapshot()["lease"]
        self.assertEqual("recovery", lease["state"])
        self.assertEqual("operation-uncertain:RuntimeError", lease["reason"])
        self.assertTrue(studio.GPU_RECOVERY_HOLD.exists())
        self.assertTrue(j["recovery_required"])

    def test_failure_after_loading_keeps_the_existing_safety_path(self):
        def loads_then_trips(name, j=None):
            studio.gpu_render_ready(name, "t2va")      # the engine came up
            studio._note_preload_refusal("stale marker must not excuse a later failure")
            raise RuntimeError("guard trip mid-render")
        with mock.patch.object(studio, "_gpu_process_identity", return_value=None), \
             pytest.raises(RuntimeError, match="guard trip"):
            self._run(loads_then_trips, reclaim=[clean_proof(), clean_proof()])
        lease = self.protocol.snapshot()["lease"]
        self.assertEqual("recovery", lease["state"])
        self.assertEqual("operation-uncertain:RuntimeError", lease["reason"])
        self.assertTrue(studio.GPU_RECOVERY_HOLD.exists())

    def test_marker_does_not_leak_into_the_next_admission(self):
        self._run(self._residency_refuses, reclaim=[clean_proof(), clean_proof()])
        self._reset_studio()
        self._run(lambda name, j=None: "busy", reclaim=[clean_proof(), clean_proof()])
        self.assertEqual("operation-uncertain:RuntimeError", self.protocol.snapshot()["lease"]["reason"])


def test_planner_refusal_is_typed_and_memory_only_for_phase_floors():
    policy = studio.RESIDENCY.policy
    actual = {"models": {}, "memory": {"available_gb": 50.0}, "inference": {}}
    plan = plan_residency(policy, actual, "qwen-h3")
    assert not plan["admitted"]
    kinds = {b["kind"] for b in plan["blockers"]}
    assert "phase-floor" in kinds
    refused = ResidencyRefused("x", plan["blockers"])
    assert refused.memory_only is (kinds == {"phase-floor"})
    assert ResidencyRefused("x", [{"kind": "busy-engine"}]).memory_only is False
    assert ResidencyRefused("x", []).memory_only is False


def test_apply_raises_refusal_before_any_mutation():
    source = (SRC / "residency.py").read_text()
    body = source[source.index("    def apply("):]
    refuse = body.index("raise ResidencyRefused(")
    assert refuse < body.index("begin_residency_transaction")
    assert refuse < body.index("self._save_receipt(receipt)")


def test_protocol_lets_a_refused_load_unload_but_not_skip_reclaim(tmp_path):
    p = DurableGpuProtocol(tmp_path / "g.sqlite3", tmp_path / "g.lock", boot_id=lambda: BOOT,
                           available_gib=lambda: 120.0, pid_alive=lambda _pid: True)
    p.qualify("h3", "t2va", peak_gib=104.0, reserve_gib=12.0, evidence="fixture")
    lease = p.acquire(job_id="j", engine="h3", task="t2va", owner="o")
    for phase in ("unload", "reclaim", "load"):
        p.advance(lease, phase)
    with pytest.raises((LeaseBusy, ValueError)):
        p.release(lease, proof=clean_proof())          # still needs unload + reclaim
    p.advance(lease, "unload")
    with pytest.raises((LeaseBusy, ValueError)):
        p.release(lease, proof=clean_proof())
    p.advance(lease, "reclaim")
    with pytest.raises(ValueError):
        p.release(lease, proof=clean_proof(memory_recovered=False))
    p.release(lease, proof=clean_proof())
    assert p.snapshot()["lease"] is None
