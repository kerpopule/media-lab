"""Intake + update-scout adversarial tests — acceptance proof 6.

Proof 6: intake and update-scout adversarial tests reject prompt injection,
arbitrary code, mutable revisions, unsafe formats, license/auth gaps, and
unsigned/unreviewed nodes.

IntakeGate never executes any supplied file — it only inspects metadata.
UpdateScout never auto-promotes and refuses unreviewable/unsigned sources.
"""
from pathlib import Path
import tempfile
import unittest

from media_lab_core.intake import IntakeGate, UpdateScout

SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4"
OK_ARGS = dict(repo_url="https://hf.co/org/repo", revision=SHA,
               license_name="Apache-2.0", license_url="https://hf.co/license",
               files=[{"name": "model.safetensors"}, {"name": "config.json"}],
               signatures_verified=True)


class IntakeAdversarial(unittest.TestCase):
    def setUp(self):
        self.gate = IntakeGate(budget_gb=100.0)

    def test_rejects_prompt_injection_in_repo_url(self):
        # scheme not https/ssh => rejected atomically.
        v = self.gate.inspect(
            repo_url="javascript:alert(1)//example.com",
            revision=SHA, license_name="MIT", license_url="https://e/l",
            files=[{"name": "config.json"}],
        )
        self.assertFalse(v["admitted"])
        self.assertTrue(any("https or ssh" in e for e in v["errors"]))

    def test_rejects_trust_remote_code(self):
        v = self.gate.inspect(**{**OK_ARGS, "trust_remote_code": True})
        self.assertFalse(v["admitted"])
        self.assertTrue(any("trust_remote_code" in e for e in v["errors"]))

    def test_rejects_mutable_revision(self):
        v = self.gate.inspect(**{**OK_ARGS, "revision": "main"})
        self.assertFalse(v["admitted"])
        self.assertTrue(any("mutable revision" in e for e in v["errors"]))

    def test_rejects_non_sha_revision(self):
        v = self.gate.inspect(**{**OK_ARGS, "revision": "v1.2.3"})
        self.assertFalse(v["admitted"])
        self.assertTrue(any("immutable sha" in e for e in v["errors"]))

    def test_rejects_unsafe_format_without_signatures(self):
        v = self.gate.inspect(**{**OK_ARGS, "files": [{"name": "model.pkl"}],
                                 "signatures_verified": False})
        self.assertFalse(v["admitted"])
        self.assertTrue(any("signatures not verified" in e for e in v["errors"]))

    def test_rejects_license_gap(self):
        v = self.gate.inspect(**{**OK_ARGS, "license_name": "unresolved",
                                 "license_url": ""})
        self.assertFalse(v["admitted"])
        self.assertTrue(any("license" in e for e in v["errors"]))

    def test_rejects_empty_file_manifest(self):
        v = self.gate.inspect(**{**OK_ARGS, "files": []})
        self.assertFalse(v["admitted"])
        self.assertTrue(any("file manifest" in e for e in v["errors"]))

    def test_rejects_file_scheme(self):
        v = self.gate.inspect(repo_url="file:///etc/passwd", **{k: val for k, val in OK_ARGS.items() if k != "repo_url"})
        self.assertFalse(v["admitted"])
        self.assertTrue(any("scheme" in e or "https or ssh" in e for e in v["errors"]))


class IntakeStage(unittest.TestCase):
    def test_stage_hashes_immutably(self):
        gate = IntakeGate(cache_root=Path("/tmp/intake-stage-test"))
        with tempfile.TemporaryDirectory() as d:
            staged = Path(d)
            (staged / "model.safetensors").write_bytes(b"abc")
            (staged / "cfg.json").write_text("{}")
            r = gate.stage_artifacts(staged, "entry-1")
            self.assertEqual(r["entry_id"], "entry-1")
            self.assertIn("model.safetensors", r["files"])
            self.assertIn("cfg.json", r["files"])
            self.assertEqual(len(r["files"]["model.safetensors"]["sha256"]), 64)


class UpdateScoutTrustPolicy(unittest.TestCase):
    def test_rejects_unreviewed_node(self):
        scout = UpdateScout(trusted_sources={"https://hf.co"},
                            reviewed_nodes={"qwen2"})
        v = scout.check("https://hf.example/Qwen/Qwen38-27B", "evil-node")
        self.assertEqual(v["review_stage"], "pending")
        self.assertTrue(any("node not in reviewed set" in p for p in v["problems"]))
        self.assertFalse(v["promote"])

    def test_rejects_untrusted_source(self):
        scout = UpdateScout(trusted_sources={"https://hf.co"},
                            reviewed_nodes={"qwen2"})
        v = scout.check("https://evil.example/repo", "qwen2")
        self.assertEqual(v["review_stage"], "pending")
        self.assertTrue(any("source not in trusted set" in p for p in v["problems"]))
        self.assertFalse(v["promote"])
