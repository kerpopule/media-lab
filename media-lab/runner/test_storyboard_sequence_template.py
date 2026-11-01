import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StoryboardSequenceTemplateContract(unittest.TestCase):
    def test_live_registry_entry_and_owned_preview_exist(self):
        app = (ROOT / "app.py").read_text()
        self.assertIn('"h3-storyboard-sequential-beats"', app)
        self.assertIn('"Field-tested H3 workflows"', app)
        self.assertIn("not as a static image", app)
        preview = ROOT / "static/templates/h3-storyboard-sequence.gif"
        self.assertTrue(preview.is_file())
        self.assertGreater(preview.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
