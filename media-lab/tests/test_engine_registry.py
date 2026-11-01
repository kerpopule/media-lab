import tempfile
import unittest
from pathlib import Path

from media_lab_core.engine_registry import load_engine_registry
from media_lab_core.preflight import inspect_registry

ROOT = Path(__file__).resolve().parents[1]


class EngineRegistryTest(unittest.TestCase):
    def test_public_example_fails_closed(self):
        result = inspect_registry(ROOT / "config" / "engines.example.toml")
        self.assertFalse(result["ok"])
        self.assertEqual(0, result["configured_engines"])
        self.assertIn("disabled", result["engines"]["h3"]["refusal_reason"])

    def test_qualified_engine_requires_immutable_revision(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "engines.toml"
            path.write_text(
                "[engines.fake]\n"
                "adapter='fake'\nstatus='qualified'\nenabled=true\n"
                "distribution='bundled'\nlicense_class='MIT'\n"
                "modes=['text-to-video']\nrequired_kernels=[]\n"
            )
            engine = load_engine_registry(path)["fake"]
            self.assertFalse(engine.available)
            self.assertIn("immutable revision missing", engine.refusal_reason())


if __name__ == "__main__":
    unittest.main()
