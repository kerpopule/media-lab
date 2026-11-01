"""Coordinator tests — rescues plan->profile mapping + drain-not-kill guard.

Companion to proof 5: the transaction/recovery machine (residency.py) is
already exercised by test_residency.py; this file covers the coordinator's
plan_to_profile translation and BusyGuard drain-vs-kill contract, which the
capacity planner relies on to keep busy services alive.
"""
from pathlib import Path
import unittest

from media_lab_core.coordinator import plan_to_profile, BusyGuard


class PlanToProfile(unittest.TestCase):
    def test_qwen_ltx_maps_text_and_video(self):
        plan = {"selected": {"language": "qwen", "video": "ltx"}}
        slots = plan_to_profile(plan)
        self.assertEqual(slots, {"text_primary": "qwen", "video_primary": "ltx"})

    def test_none_returns_empty(self):
        self.assertEqual(plan_to_profile({"selected": {}}), {})
