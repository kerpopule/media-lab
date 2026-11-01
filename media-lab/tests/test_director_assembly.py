"""The app side of director school: the H3 schema is never wrapped twice, the
storyboard assembler is the director-grade stitcher with a critic, and the
board carries the grammar the director wrote."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


class H3SchemaTests(unittest.TestCase):
    def test_a_composed_schema_passes_through_once(self):
        composed = ("integrated_multimodal_description: [Shot 1] Medium close-up. (S1) says <d>[English] Hi.</d>"
                    "\n\noverall_soundscape: room tone\n\nnon_diegetic_music: N/A")
        out = app.h3_prompt(composed)
        self.assertEqual(out, composed)
        with_frame = app.h3_prompt(composed, start_image=True)
        self.assertTrue(with_frame.startswith("For the target video"))
        self.assertEqual(with_frame.count("integrated_multimodal_description:"), 1)
        self.assertEqual(app.h3_prompt(with_frame, start_image=True).count("For the target video"), 1)

    def test_plain_text_is_still_wrapped(self):
        out = app.h3_prompt('A man says "hello" in a diner.')
        self.assertTrue(out.startswith("integrated_multimodal_description: [Shot 1] "))
        self.assertIn("(S1) says <d>[English] hello</d>", out)


class BoardGrammarTests(unittest.TestCase):
    def test_compose_beat_prompt_leads_with_size_and_keeps_sides_and_wardrobe(self):
        board = {"bible": {"style": "35mm film", "world": "a laundromat", "camera": "35mm lens",
                           "palette": "teal and orange", "time_of_day": "2 a.m.",
                           "characters": [{"name": "Maya", "look": "woman, curly hair",
                                           "wardrobe": "teal scrubs"}]},
                 "cast": []}
        beat = {"video_prompt": "Maya folds a towel.", "characters": ["Maya"], "shot_size": "MCU",
                "screen_side": {"Maya": "left"}}
        text = app.compose_beat_prompt(board, beat, chars=[])
        self.assertTrue(text.startswith("Medium close-up"))
        for must in ("teal scrubs", "Maya on the left of the frame", "Nobody looks into the camera",
                     "2 a.m.", "teal and orange"):
            self.assertIn(must, text)

    def test_clean_bible_keeps_the_new_constants(self):
        bible = app.clean_bible({"style": "s", "palette": "p", "time_of_day": "t",
                                 "characters": [{"name": "A", "look": "l", "wardrobe": "w"}]})
        self.assertEqual((bible["palette"], bible["time_of_day"]), ("p", "t"))
        self.assertEqual(bible["characters"][0]["wardrobe"], "w")


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class DirectorAssemblyTests(unittest.TestCase):
    def _clip(self, path, seconds, freq, flash=0.0):
        inputs = ["-f", "lavfi", "-i", f"testsrc2=s=336x192:r=24:d={seconds - flash}"]
        chain = "[0:v]null[v]"
        if flash:
            inputs += ["-f", "lavfi", "-i", f"color=c=red:s=336x192:r=24:d={flash}"]
            chain = "[1:v][0:v]concat=n=2:v=1:a=0[v]"
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}"]
        audio = len([x for x in inputs if x == "-i"]) - 1
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", chain,
                        "-map", "[v]", "-map", f"{audio}:a", "-t", str(seconds), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)

    def test_assemble_uses_the_director_stitcher_and_records_the_critic(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); media = root / "media"; jobs = root / "jobs"
            media.mkdir(); jobs.mkdir()
            self._clip(media / "one.mp4", 3.0, 330, flash=0.5)
            self._clip(media / "two.mp4", 3.0, 550)
            board_file = root / "storyboards.json"
            board_file.write_text(json.dumps([{
                "id": "dir-test", "title": "Director test", "orientation": "landscape",
                "bible": {"style": "35mm", "characters": []},
                "beats": [
                    {"clip_url": "/media/one.mp4", "scene": "A", "shot_size": "WS"},
                    {"clip_url": "/media/two.mp4", "scene": "A", "shot_size": "MCU", "transition": "cut_on_action"},
                ]}]))
            job = {"id": "assemble-dir", "request": {"board_id": "dir-test"}, "status": "running", "stage": ""}
            with patch.object(app, "MEDIA", media), patch.object(app, "JOBS_DIR", jobs), \
                 patch.object(app, "BOARDS_FILE", board_file), patch.object(app, "gallery_add"), \
                 patch.dict("os.environ", {"MEDIA_LAB_CRITIC_VISION": "off"}):
                app.run_assemble(job)
            self.assertEqual(job["status"], "done", job)
            persisted = json.loads(board_file.read_text())[0]
            assembly = persisted["assembly"]
            self.assertEqual(assembly["engine"], "director-v1")
            self.assertEqual(assembly["quality"], "high")
            self.assertTrue(any("unrequested cut" in why for t in assembly["trims"] for why in t["why"]))
            self.assertIn("measured checks only", assembly["critic"])
            receipt = json.loads((jobs / "assemble-dir" / "assembly-receipt.json").read_text())
            self.assertEqual(receipt["probe"]["width"], 336)          # all takes share a canvas: kept
            self.assertEqual(receipt["plan"]["shots"][1]["transition_in"]["kind"], "cut_on_action")
            self.assertTrue((jobs / "assemble-dir" / "critic.json").is_file())
            self.assertIn("critic", job)

    def test_legacy_engine_is_still_available(self):
        with patch.object(app, "ASSEMBLY_ENGINE", "legacy"), \
             patch.object(app, "_run_assemble_legacy", return_value=None) as legacy, \
             patch.object(app, "_run_assemble_director") as director, \
             tempfile.TemporaryDirectory() as td:
            root = Path(td); media = root / "media"; media.mkdir()
            self._clip(media / "one.mp4", 1.5, 330)
            board_file = root / "storyboards.json"
            board_file.write_text(json.dumps([{"id": "b", "beats": [{"clip_url": "/media/one.mp4"}]}]))
            with patch.object(app, "MEDIA", media), patch.object(app, "BOARDS_FILE", board_file):
                app.run_assemble({"id": "x", "request": {"board_id": "b"}, "status": "running"})
            legacy.assert_called_once()
            director.assert_not_called()


if __name__ == "__main__":
    unittest.main()
