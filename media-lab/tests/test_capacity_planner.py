"""Capacity planner + budget tests — acceptance proofs 2, 3, 4.

Proof 2: seeded manifests produce deterministic capacity plans for
          Qwen+LTX (admitted), Qwen+H3 (admitted), dual-video with Qwen drained
          (deterministic refused on GB10: ltx+h3 warm 70 GiB > 69.7 pool), and
          full-memory LLM exclusive (deterministic refused: active 70 > pool).
Proof 3: capacity bar reconciles exactly to the planner's bytes and exposes
          phase/range uncertainty.
Proof 4: invalid combinations fail with exact phase/floor explanation.
"""
from pathlib import Path
import unittest

from media_lab_core.capacity_planner import Planner

ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "config" / "capacity-budget.json"
MANIFESTS = ROOT / "config" / "model-manifests.json"
POLICY = ROOT / "config" / "capacity-policy.json"


def make_planner():
    return Planner(BUDGET, MANIFESTS, POLICY)


class DeterministicPlans(unittest.TestCase):
    def test_qwen_ltx_admitted(self):
        plan = make_planner().plan({"language": "qwen", "video": "ltx"})
        self.assertTrue(plan["admitted"], plan["blockers"])
        self.assertEqual(["ltx", "qwen"], plan["resident"])
        self.assertEqual([], plan["blockers"])
        self.assertAlmostEqual(69.7, plan["memory"]["pool_gb"], places=1)

    def test_qwen_h3_admitted(self):
        plan = make_planner().plan({"language": "qwen", "video": "h3"})
        self.assertTrue(plan["admitted"], plan["blockers"])
        self.assertEqual(["h3", "qwen"], plan["resident"])

    def test_dual_video_qwen_drained_deterministic(self):
        # ltx+h3 warm: 35 + 35 = 70 > 69.7 allocatable pool => deterministic,
        # explainable refusal on GB10. Qwen is drained (absent from selection).
        plan = make_planner().plan({"video": "ltx", "video_secondary": "h3"})
        self.assertFalse(plan["admitted"])
        kinds = [b["kind"] for b in plan["blockers"]]
        self.assertIn("warm-idle-overflow", kinds)
        warm = next(b for b in plan["blockers"] if b["kind"] == "warm-idle-overflow")
        self.assertIn("70.0 GiB warm", warm["reason"])
        self.assertIn("69.7", warm["reason"])
        self.assertNotIn("qwen", plan["resident"])

    def test_full_memory_llm_exclusive_deterministic(self):
        plan = make_planner().plan({"language": "deepseek-full"})
        # deepseek active 70 GiB > 69.7 pool => deterministic refusal on this
        # Spark; matches the studio rule that full-memory LLMs are unavailable if
        # measured floors fail.
        self.assertFalse(plan["admitted"])
        kinds = [b["kind"] for b in plan["blockers"]]
        self.assertIn("active-phase-floor", kinds)
        self.assertEqual([], plan["resident"])

    def test_plan_is_deterministic_across_runs(self):
        p = make_planner()
        a = p.plan({"language": "qwen", "video": "ltx"})
        b = p.plan({"language": "qwen", "video": "ltx"})
        self.assertEqual(a, b)
        self.assertEqual(make_planner().plan({"language": "qwen", "video": "h3"}),
                         make_planner().plan({"language": "qwen", "video": "h3"}))


class PhaseFloorRefusals(unittest.TestCase):
    def test_unknown_slot_fails(self):
        with self.assertRaises(Exception):
            make_planner().plan({"bogus": "qwen"})

    def test_unknown_model_fails(self):
        with self.assertRaises(Exception):
            make_planner().plan({"language": "not-a-model"})

    def test_model_not_allowed_in_slot_explains(self):
        plan = make_planner().plan({"video": "qwen-image-21"})  # image model, wrong slot
        self.assertFalse(plan["admitted"])
        kinds = [b["kind"] for b in plan["blockers"]]
        self.assertIn("model-not-allowed-in-slot", kinds)


class CapacityBar(unittest.TestCase):
    def test_bar_reconciles_exactly(self):
        bar = make_planner().capacity_bar({"language": "qwen", "video": "ltx"})
        self.assertTrue(bar["admitted"])
        # physical total from budget, never nominal 128
        self.assertAlmostEqual(121.7, bar["physical_total_gb"], places=1)
        self.assertAlmostEqual(69.7, bar["allocatable_pool_gb"], places=1)
        committed = sum(s["committed_warm_hi_gb"] for s in bar["segments"])
        self.assertAlmostEqual(committed, bar["committed_warm_hi_gb"], places=2)
        self.assertAlmostEqual(bar["grey_headroom_gb"],
                               round(bar["allocatable_pool_gb"] - bar["committed_warm_hi_gb"], 2),
                               places=2)
        self.assertGreaterEqual(bar["grey_headroom_gb"], 0)
        # segments sorted deterministically by id
        self.assertEqual([s["id"] for s in bar["segments"]], ["ltx", "qwen"])
        # every segment exposes the measured phase range bands
        qwen = next(s for s in bar["segments"] if s["id"] == "qwen")
        self.assertIn("phase_ranges_gb", qwen)
        for ph in ("cold_load", "warm_idle", "active_inference",
                   "decode_vae_mux", "kv_growth", "swap_overlap"):
            self.assertIn(ph, qwen["phase_ranges_gb"])
        self.assertLessEqual(qwen["phase_ranges_gb"]["warm_idle"]["lo"],
                             qwen["phase_ranges_gb"]["warm_idle"]["hi"])

    def test_grey_is_uncommitted_not_optimistic(self):
        bar = make_planner().capacity_bar({"language": "qwen", "video": "h3"})
        # committed = qwen warm hi 28 + h3 warm hi 35 = 63; grey = 69.7-63 = 6.7
        self.assertAlmostEqual(63.0, bar["committed_warm_hi_gb"], places=1)
        self.assertAlmostEqual(6.7, bar["grey_headroom_gb"], places=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)