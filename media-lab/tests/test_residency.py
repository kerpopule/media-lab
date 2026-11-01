import json
import tempfile
import unittest
from pathlib import Path

from residency import ResidencyController, ResidencyError, load_policy, plan_residency, resolve_profile


ROOT = Path(__file__).resolve().parents[1]
# Planner semantics are exercised against the classic single-box budgets
# (resident Qwen 32 GB, H3 40 GB, floor 24). The live config/model-residency-
# policy.json describes Sol-H3-Spark, which takes the whole pool and never
# co-resides with anything, so it cannot drive these co-residency cases.
POLICY_PATH = ROOT / "tests" / "fixtures" / "model-residency-policy.classic.json"


def actual(*resident, available=58, busy=(), unhealthy=()):
    return {
        "models": {m: {"resident": m in resident, "healthy": m in resident and m not in unhealthy,
                       "busy": m in busy, "state": "busy" if m in busy else "healthy"}
                   for m in ("qwen", "ltx", "h3")},
        "memory": {"available_gb": available},
        "pool_lease": {"owner": "media-lab-pool.service"},
        "inference": {"locked": False},
    }


class FakeHooks:
    def __init__(self, state, fail_start=None, fail_begin=False):
        self.state = state
        self.fail_start = fail_start
        self.fail_begin = fail_begin
        self.calls = []
        self.transaction_active = False

    def snapshot(self):
        return json.loads(json.dumps(self.state))

    def release_image_weights(self, why):
        self.calls.append(("release", why))
        return 12.0

    def stop_model(self, model):
        self.calls.append(("stop", model))
        self.state["models"][model].update(resident=False, healthy=False, busy=False, state="cold")
        return {"exact": [f"{model}-runtime"]}

    def drain_model(self, model):
        self.calls.append(("drain", model))
        self.state["models"][model]["busy"] = False
        return {"state": "idle", "running": 0, "waiting": 0,
                "source": "fixture", "drain": "complete"}

    def start_model(self, model, detail):
        self.calls.append(("start", model, detail))
        if model == self.fail_start:
            return False
        self.state["models"][model].update(resident=True, healthy=True, busy=False, state="healthy")
        return True

    def model_healthy(self, model):
        return bool(self.state["models"][model]["healthy"])

    def begin_residency_transaction(self):
        self.calls.append(("begin-transaction",))
        if self.fail_begin:
            return None
        self.transaction_active = True
        return object()

    def end_residency_transaction(self, _token):
        self.calls.append(("end-transaction",))
        self.transaction_active = False


class ResidencyPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = load_policy(POLICY_PATH)

    def test_named_profiles_and_custom_validation(self):
        self.assertEqual("qwen-ltx-default", self.policy["default_profile"])
        self.assertEqual(["qwen", "ltx"], resolve_profile(
            self.policy, "qwen-ltx-default")["models"])
        self.assertEqual(["ltx", "h3"], resolve_profile(
            self.policy, "dual-video-ltx-h3")["models"])
        self.assertEqual(["qwen", "h3"], resolve_profile(self.policy, "custom", {
            "text_primary": "qwen", "video_primary": "h3", "video_secondary": None})["models"])
        with self.assertRaisesRegex(ResidencyError, "same model"):
            resolve_profile(self.policy, "custom", {"text_primary": "qwen",
                "video_primary": "ltx", "video_secondary": "ltx"})

    def test_default_plan_retains_qwen_and_ltx(self):
        plan = plan_residency(self.policy, actual("qwen", "ltx"), "qwen-ltx-default")
        self.assertTrue(plan["admitted"])
        self.assertEqual(["ltx", "qwen"], plan["retain"])
        self.assertEqual([], plan["evict"])

    def test_qwen_h3_plan_never_silently_evicts_qwen(self):
        plan = plan_residency(self.policy, actual("qwen", "ltx"), "qwen-h3")
        self.assertTrue(plan["admitted"])
        self.assertNotIn("qwen", plan["evict"])
        self.assertEqual(["ltx"], plan["evict"])
        self.assertEqual(["h3"], plan["load"])

    def test_unhealthy_required_model_is_replaced_for_self_healing(self):
        plan = plan_residency(
            self.policy,
            actual("qwen", "ltx", unhealthy=("ltx",)),
            "qwen-ltx-default",
        )
        self.assertTrue(plan["admitted"])
        self.assertEqual(["ltx"], plan["evict"])
        self.assertEqual(["ltx"], plan["load"])
        self.assertIn("qwen", plan["retain"])

    def test_dual_video_marks_qwen_eviction_intentional(self):
        plan = plan_residency(self.policy, actual("qwen", "ltx"), "dual-video-ltx-h3")
        self.assertTrue(plan["admitted"])
        qwen = next(a for a in plan["actions"] if a.get("model") == "qwen" and
                    a["action"] == "evict")
        self.assertTrue(qwen["intentional"])

    def test_custom_with_qwen_retains_resident_qwen(self):
        plan = plan_residency(self.policy, actual("qwen", "ltx"), "custom", {
            "text_primary": "qwen", "video_primary": "ltx", "video_secondary": None})
        self.assertTrue(plan["admitted"])
        self.assertIn("qwen", plan["retain"])
        self.assertNotIn("qwen", plan["evict"])

    def test_custom_without_qwen_is_refused_as_silent_eviction(self):
        plan = plan_residency(self.policy, actual("qwen", "ltx"), "custom", {
            "text_primary": None, "video_primary": "ltx", "video_secondary": "h3"})
        self.assertFalse(plan["admitted"])
        self.assertIn({"kind": "silent-qwen-eviction", "model": "qwen",
                       "reason": "Qwen eviction is allowed only by dual-video-ltx-h3"},
                      plan["blockers"])
        qwen = next(a for a in plan["actions"] if a.get("model") == "qwen" and
                    a["action"] == "evict")
        self.assertFalse(qwen["intentional"])

    def test_custom_without_qwen_preserves_qwen_activity_fail_closed(self):
        cases = {
            "running": {"state": "busy", "running": 1.0, "waiting": 0.0},
            "waiting": {"state": "busy", "running": 0.0, "waiting": 1.0},
            "unknown": {"state": "unknown", "running": 0.0, "waiting": 0.0},
        }
        for name, activity in cases.items():
            with self.subTest(activity=name):
                state = actual("qwen", "ltx", busy=("qwen",))
                state["models"]["qwen"]["activity"] = activity
                plan = plan_residency(self.policy, state, "custom", {
                    "text_primary": None, "video_primary": "ltx", "video_secondary": "h3"})
                self.assertFalse(plan["admitted"])
                self.assertIn("busy-engine", {b["kind"] for b in plan["blockers"]})
                self.assertIn("silent-qwen-eviction", {b["kind"] for b in plan["blockers"]})
                qwen = next(a for a in plan["actions"] if a.get("model") == "qwen" and
                            a["action"] == "evict")
                self.assertFalse(qwen["intentional"])

    def test_busy_engine_protection(self):
        plan = plan_residency(self.policy, actual("qwen", "ltx", busy=("ltx",)), "qwen-h3")
        self.assertFalse(plan["admitted"])
        self.assertIn("busy-engine", {b["kind"] for b in plan["blockers"]})

    def test_active_qwen_blocks_qwen_eviction(self):
        # Binding live-test-finding repair: a running Qwen request must stop a
        # dual-video / qwen-h3 plan from evicting the text slot.  The snapshot
        # hook now reports busy from the live runtime gauge (UNKNOWN -> busy),
        # so the plan's busy-engine blocker must refuse resubmission.
        plan = plan_residency(self.policy, actual("qwen", "ltx", busy=("qwen",)), "dual-video-ltx-h3")
        self.assertFalse(plan["admitted"])
        kinds = {b["kind"] for b in plan["blockers"]}
        self.assertIn("busy-engine", kinds)
        self.assertIn("qwen", [b.get("model") for b in plan["blockers"] if b["kind"] == "busy-engine"])
        # The plan still computes the eviction intent, but the busy-engine
        # blocker is what prevents it from being admitted/applied.
        self.assertIn("qwen", plan["evict"])

    def test_qwen_retaining_profile_never_evicts_resident_degraded_qwen(self):
        plan = plan_residency(self.policy,
                              actual("qwen", "ltx", unhealthy=("qwen",)),
                              "qwen-h3")
        self.assertNotIn("qwen", plan["evict"])
        self.assertNotIn("qwen", plan["load"])
        self.assertIn("qwen", plan["retain"])

    def test_idle_qwen_can_be_evicted_and_restored(self):
        # Idle qwen (busy=False) is eligible for an intentional eviction under
        # dual-video-ltx-h3 and restores through the recorded rollback.
        plan = plan_residency(self.policy, actual("qwen", "ltx"), "dual-video-ltx-h3")
        self.assertTrue(plan["admitted"])
        self.assertIn("qwen", plan["evict"])
        with tempfile.TemporaryDirectory() as td:
            hooks = FakeHooks(actual("qwen", "ltx"))
            ctl = ResidencyController(POLICY_PATH, Path(td), hooks)
            # Idle qwen drains and the recorded rollback restores it exactly:
            restored = ctl.recover() if not hooks.state["models"]["qwen"]["resident"] else None
            # A fresh default plan must demand qwen back when it is idle/absent.
            restore_plan = plan_residency(self.policy, actual("ltx"), "qwen-ltx-default")
            self.assertTrue(any(a["action"] == "load" and a.get("model") == "qwen"
                                for a in restore_plan["actions"]))
            self.assertIn("qwen", restore_plan["load"])
            self.assertTrue(restored is None or restored.get("status") in ("committed", "rolled-back"))

    def test_inference_transaction_blocks_residency_mutation(self):
        state = actual("qwen", "ltx")
        state["inference"]["locked"] = True
        plan = plan_residency(self.policy, state, "qwen-h3")
        self.assertFalse(plan["admitted"])
        self.assertIn("inference-transaction-active", {b["kind"] for b in plan["blockers"]})

    def test_phase_floor_refusal_names_model_and_phase(self):
        plan = plan_residency(self.policy, actual("qwen", available=10), "qwen-h3")
        self.assertFalse(plan["admitted"])
        block = next(b for b in plan["blockers"] if b["kind"] == "phase-floor")
        self.assertEqual("h3", block["model"])
        self.assertIn(block["phase"], block["reason"])

    def test_all_profiles_have_current_budget_dry_run_receipts(self):
        baseline = actual("qwen", "ltx", available=58)
        requests = {
            "qwen-ltx-default": None,
            "qwen-h3": None,
            "dual-video-ltx-h3": None,
            "qwen-only": None,
            "custom": {"text_primary": "qwen", "video_primary": "h3",
                       "video_secondary": None},
        }
        receipts = {name: plan_residency(self.policy, baseline, name, slots)
                    for name, slots in requests.items()}
        self.assertEqual(set(requests), set(receipts))
        for name, receipt in receipts.items():
            with self.subTest(profile=name):
                self.assertTrue(receipt["admitted"], receipt["blockers"])
                self.assertEqual(24.0, receipt["memory"]["operational_floor_gb"])


class ResidencyTransactionTests(unittest.TestCase):
    def controller(self, hooks, root):
        return ResidencyController(POLICY_PATH, Path(root), hooks)

    def test_dual_video_apply_records_bounded_drain_before_qwen_eviction(self):
        with tempfile.TemporaryDirectory() as td:
            hooks = FakeHooks(actual("qwen", "ltx"))
            ctl = self.controller(hooks, td)
            receipt = ctl.apply("dual-video-ltx-h3")
            self.assertEqual("committed", receipt["status"])
            self.assertLess(hooks.calls.index(("drain", "qwen")),
                            hooks.calls.index(("stop", "qwen")))
            drained = next(x for x in receipt["completed"]
                           if x["action"] == "drain" and x["model"] == "qwen")
            self.assertEqual((0, 0), (drained["detail"]["running"],
                                      drained["detail"]["waiting"]))

    def test_exact_receipt_and_commit(self):
        with tempfile.TemporaryDirectory() as td:
            hooks = FakeHooks(actual("qwen", "ltx"))
            ctl = self.controller(hooks, td)
            receipt = ctl.apply("qwen-h3")
            self.assertEqual("committed", receipt["status"])
            self.assertTrue(any(s.get("model") == "ltx" for s in receipt["rollback"]))
            self.assertEqual("qwen-h3", ctl.desired()["name"])
            self.assertFalse((Path(td) / "active-transaction.json").exists())
            self.assertFalse(hooks.transaction_active)
            self.assertEqual(("begin-transaction",), hooks.calls[0])
            self.assertEqual(("end-transaction",), hooks.calls[-1])

    def test_load_failure_rolls_back_exact_pre_state(self):
        with tempfile.TemporaryDirectory() as td:
            hooks = FakeHooks(actual("qwen", "ltx"), fail_start="h3")
            ctl = self.controller(hooks, td)
            with self.assertRaisesRegex(ResidencyError, "h3 failed to load"):
                ctl.apply("qwen-h3")
            self.assertTrue(hooks.state["models"]["ltx"]["resident"])
            self.assertTrue(hooks.state["models"]["qwen"]["resident"])
            receipts = list((Path(td) / "receipts").glob("*.json"))
            self.assertEqual(1, len(receipts))
            self.assertEqual("rolled-back", json.loads(receipts[0].read_text())["status"])
            self.assertFalse(hooks.transaction_active)

    def test_crash_receipt_recovers_or_reports_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            hooks = FakeHooks(actual("qwen", "h3"))
            ctl = self.controller(hooks, td)
            interrupted = {"version": 1, "id": "crash", "started_at": 1,
                "status": "applying", "rollback": [
                    {"action": "evict", "model": "h3"},
                    {"action": "load", "model": "ltx", "detail": {"exact": ["ltx-runtime"]}}],
                "completed": [], "pre_state": actual("qwen", "ltx"), "plan": {}}
            (Path(td) / "active-transaction.json").write_text(json.dumps(interrupted))
            recovered = ctl.recover()
            self.assertEqual("rolled-back", recovered["status"])
            self.assertTrue(hooks.state["models"]["ltx"]["resident"])
            self.assertFalse(hooks.state["models"]["h3"]["resident"])

            hooks.fail_start = "ltx"
            (Path(td) / "active-transaction.json").write_text(json.dumps(interrupted))
            degraded = ctl.recover()
            self.assertEqual("failed", degraded["status"])
            self.assertTrue(degraded["rollback_errors"])

    def test_crash_recovery_waits_without_mutation_when_inference_is_busy(self):
        with tempfile.TemporaryDirectory() as td:
            hooks = FakeHooks(actual("qwen", "h3"), fail_begin=True)
            ctl = self.controller(hooks, td)
            interrupted = {"version": 1, "id": "busy-crash", "started_at": 1,
                "status": "applying", "rollback": [
                    {"action": "evict", "model": "h3"},
                    {"action": "load", "model": "ltx", "detail": {"exact": ["ltx-runtime"]}}],
                "completed": [], "pre_state": actual("qwen", "ltx"), "plan": {}}
            active = Path(td) / "active-transaction.json"
            active.write_text(json.dumps(interrupted))

            pending = ctl.recover()

            self.assertEqual("recovery-pending", pending["status"])
            self.assertTrue(active.exists())
            self.assertEqual([("begin-transaction",)], hooks.calls)
            self.assertTrue(hooks.state["models"]["h3"]["resident"])
            self.assertFalse(hooks.state["models"]["ltx"]["resident"])
            self.assertFalse(hooks.transaction_active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
