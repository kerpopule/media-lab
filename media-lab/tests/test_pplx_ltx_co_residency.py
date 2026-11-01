"""Static contract for the promoted PPLX-priority LTX co-residency profile."""
import json
import pytest
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PplxLtxCoResidencyContractTests(unittest.TestCase):
    @pytest.mark.spark  # needs the Spark's private productions/ or image-svc/ tree
    def test_idle_supervisor_does_not_resurrect_sam3(self):
        supervisor = (ROOT / "runner/service_supervisor.py").read_text()
        units = supervisor.split("UNITS = [", 1)[1].split("]", 1)[0]
        self.assertNotIn('("media-lab-segment.service"', units)

        image_service = (ROOT / "image-svc/image_service.py").read_text()
        self.assertIn('SEG_UNIT   = "media-lab-segment.service"', image_service)
        self.assertIn('["systemctl", "--user", "start", SEG_UNIT]', image_service)
        self.assertIn('["systemctl", "--user", "stop", SEG_UNIT]', image_service)

    def test_pplx_default_reserves_ltx_decode_floor(self):
        starter = (ROOT / "runner/start_pplx27.sh").read_text()
        policy = (ROOT / "config/model-residency-policy.json").read_text()

        self.assertIn("PPLX_GPU_UTIL:-0.28", starter)
        self.assertIn("PPLX_MAX_LEN:-65536", starter)
        self.assertIn("--max-num-seqs 2", starter)
        self.assertIn(
            "--served-model-name pplx-computer-qwen-3-8-27b-dflash2-20260824 media-lab-text",
            starter,
        )
        self.assertNotIn(" qwen3.8-27b-q4km", starter)
        self.assertNotIn(" qwen38-27b", starter)
        self.assertIn('"default_profile": "qwen-ltx-default"', policy)
        self.assertIn('"decode": 46', policy)
        # Sol-H3-Spark: the text model is remote (1 GB placeholder). The
        # measured 115 GiB decode envelope plus the approved 2 GiB operating
        # floor makes the exact cold-admission boundary 117 GiB.
        self.assertIn('"operational_floor_gb": 2', policy)
        live_policy = json.loads(policy)
        self.assertEqual(
            117,
            live_policy["models"]["h3"]["phases_gb"]["decode"]
            + live_policy["operational_floor_gb"],
        )
        self.assertIn('"display_name": "Sol-H3-Spark"', policy)

    def test_ltx_keeps_promoted_maestro_profile_and_pplx_is_not_evicted(self):
        engine = (ROOT / "runner/engine_server.py").read_text()
        app = (ROOT / "app.py").read_text()
        policy = (ROOT / "config/model-residency-policy.json").read_text()

        self.assertIn("pipe, profile_no=4, compile=False", engine)
        self.assertIn(
            'target = "qwen-h3" if name == "h3" else "qwen-ltx-default"', app
        )
        self.assertIn("retain-qwen", policy)

    def test_duplicate_portable_engine_guard_is_exact_and_fail_closed(self):
        guard = (ROOT / "runner/pplx_shared_runtime_guard.sh").read_text()
        self.assertIn("com.perplexity.computer.local-engine=vllm-docker", guard)
        self.assertIn('docker stop --time 10 "$id"', guard)
        self.assertNotIn("docker kill", guard)
        self.assertNotIn("pkill", guard)
        self.assertNotIn("killall", guard)
        self.assertNotIn("docker stop $(", guard)

        unit = (ROOT / "config/pplx-shared-runtime-guard.service").read_text()
        self.assertIn("pplx_shared_runtime_guard.sh", unit)
        self.assertIn("Restart=always", unit)

    def test_text_guard_enforces_pplx_and_stands_down_flash(self):
        guard = (ROOT / "runner/text_runtime_guard.sh").read_text()
        self.assertIn("mode=pplx", guard)
        self.assertIn("name=qwen38-vllm", guard)
        self.assertIn("docker stop --time 30 qwen38-flash-next", guard)
        self.assertNotIn('mode=flash', guard)

    def test_protected_pplx_has_exactly_one_general_companion_slot(self):
        policy = json.loads((ROOT / "config/companion-residency-policy.json").read_text())
        self.assertTrue(policy["protected_primary"]["always_resident"])
        self.assertFalse(policy["protected_primary"]["evictable_by_media"])
        self.assertEqual("ltx", policy["companion_slot"]["default"])
        self.assertEqual("exactly_one_when_idle_or_rendering",
                         policy["companion_slot"]["cardinality"])
        self.assertEqual({"ltx", "h3", "qwen-image", "flux-kontext",
                          "music3", "yue2", "voicebox-tts"},
                         set(policy["companion_slot"]["members"]))
