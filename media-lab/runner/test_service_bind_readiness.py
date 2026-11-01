#!/usr/bin/env python3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MediaLabBindReadinessContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "systemd/media-lab-simple.service.d/10-tailscale-bind-readiness.conf"
        cls.text = cls.path.read_text()

    def test_dropin_waits_for_exact_canonical_tailscale_address(self):
        self.assertIn("ExecStartPre=", self.text)
        self.assertIn("tailscale0", self.text)
        # The address is per-host: it comes from config/local.env, never a literal.
        self.assertIn("EnvironmentFile=-%h/media-lab-simple/config/local.env", self.text)
        self.assertIn("MEDIA_LAB_TAILNET_HOST", self.text)
        self.assertIn("/32", self.text)
        self.assertIn("until ", self.text)

    def test_dropin_never_times_out_while_tailnet_recovers(self):
        self.assertIn("TimeoutStartSec=0", self.text)

    def test_wait_happens_before_app_import_and_worker_start(self):
        # This must stay a systemd ExecStartPre rather than an app.py startup
        # hook: importing app.py mutates restart recovery state and starts the
        # worker, which is exactly what the readiness gate must prevent.
        self.assertNotIn("uvicorn", self.text)
        self.assertNotIn("app.py", self.text.split("[Service]", 1)[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
