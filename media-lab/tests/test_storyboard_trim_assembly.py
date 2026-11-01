import unittest
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import app


class StoryboardTrimAssemblyTests(unittest.TestCase):
    def test_assembly_commit_is_fail_closed_on_storyboard_readback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            board_file = root / "storyboards.json"
            final = root / "candidate.mp4"
            final.write_bytes(b"exact-private-candidate")
            board = {"id": "board-one", "title": "One", "beats": []}
            boards = [board]
            board_file.write_text(json.dumps(boards))
            job = {"id": "queue-assembly-1", "status": "running", "stage": "encoding"}
            with patch.object(app, "BOARDS_FILE", board_file):
                self.assertTrue(app._commit_storyboard_assembly(
                    board, boards, job, final, "/media/candidate.mp4"))
            persisted = json.loads(board_file.read_text())[0]
            digest = hashlib.sha256(final.read_bytes()).hexdigest()
            self.assertEqual(persisted["final_sha256"], digest)
            self.assertEqual(persisted["last_assembly_job_id"], job["id"])
            self.assertTrue(persisted["candidate_not_final_until_owner_approves"])
            self.assertFalse(persisted["publication_authorized"])
            self.assertEqual(job["status"], "done")
            self.assertTrue(job["storyboard_registered"])

    def test_full_clip_defaults_to_media_duration(self):
        with patch.object(app, "media_duration", return_value=5.166667):
            self.assertEqual(app._beat_trim_window({}, object()), (0.0, 5.166667, 5.166667))

    def test_explicit_semantic_trim_window(self):
        beat = {"trim_in_seconds": 0.1, "trim_out_seconds": 3.18}
        with patch.object(app, "media_duration", return_value=5.166667):
            self.assertEqual(app._beat_trim_window(beat, object()), (0.1, 3.18, 3.08))

    def test_invalid_trim_windows_fail_closed(self):
        bad = [
            {"trim_in_seconds": -0.1, "trim_out_seconds": 2.0},
            {"trim_in_seconds": 2.0, "trim_out_seconds": 2.0},
            {"trim_in_seconds": 2.5, "trim_out_seconds": 1.0},
            {"trim_in_seconds": 0.0, "trim_out_seconds": 6.0},
        ]
        with patch.object(app, "media_duration", return_value=5.166667):
            for beat in bad:
                with self.subTest(beat=beat), self.assertRaises(ValueError):
                    app._beat_trim_window(beat, object())

    def test_assembly_source_is_separate_from_preview_clip(self):
        beat = {"clip_url": "/media/trimmed.mp4", "assembly_source_url": "/media/original.mp4"}
        self.assertEqual(app._beat_assembly_url(beat), "/media/original.mp4")
        self.assertEqual(app._beat_assembly_url({"clip_url": "/media/clip.mp4"}), "/media/clip.mp4")

    def test_real_audio_video_assembly_obeys_trim_windows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); media = root / "media"; jobs = root / "jobs"
            media.mkdir(); jobs.mkdir()
            for index, color in enumerate(("red", "blue"), 1):
                subprocess.run([
                    "ffmpeg", "-nostdin", "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=24:d=1.2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.2",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(media / f"source{index}.mp4")], check=True)
            board_file = root / "storyboards.json"
            board_file.write_text(json.dumps([{
                "id": "trim-test", "title": "Trim test", "orientation": "landscape",
                "beats": [
                    {"clip_url": "/media/preview1.mp4", "assembly_source_url": "/media/source1.mp4", "trim_in_seconds": .2, "trim_out_seconds": .7},
                    {"clip_url": "/media/preview2.mp4", "assembly_source_url": "/media/source2.mp4", "trim_in_seconds": .1, "trim_out_seconds": .6},
                ]}]))
            job = {"id": "assemble-test", "request": {"board_id": "trim-test"}, "status": "running", "stage": ""}
            with patch.object(app, "MEDIA", media), patch.object(app, "JOBS_DIR", jobs), \
                 patch.object(app, "BOARDS_FILE", board_file), patch.object(app, "gallery_add"):
                app.run_assemble(job)
            self.assertEqual(job["status"], "done", job)
            output = media / "board_trim-test.mp4"
            duration = float(subprocess.check_output([
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(output)], text=True).strip())
            self.assertAlmostEqual(duration, 1.0, delta=.08)

    def test_external_assembly_import_runs_as_queue_job_and_binds_exact_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); media = root / "media"; imported = root / "inbox" / "imported"
            media.mkdir(); imported.mkdir(parents=True)
            source = imported / "queued-candidate.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=0.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            board_file = root / "storyboards.json"
            board_file.write_text(json.dumps([{"id": "external-board", "title": "External", "beats": []}]))
            job = {"id": "assembly-import-1", "kind": "assembly_import", "status": "running",
                   "stage": "running", "request": {"path": str(source), "board_id": "external-board",
                                                      "sha256": digest, "title": "External"}}
            with patch.object(app, "ROOT", root), patch.object(app, "MEDIA", media), \
                 patch.object(app, "BOARDS_FILE", board_file), patch.object(app, "gallery_add"):
                app.run_assembly_import(job)
            persisted = json.loads(board_file.read_text())[0]
            self.assertEqual(job["status"], "done", job)
            self.assertEqual(job["kind"], "assembly_import")
            self.assertTrue(job["storyboard_registered"])
            self.assertEqual(job["sha256"], digest)
            self.assertEqual(persisted["final_sha256"], digest)
            self.assertEqual(persisted["last_assembly_job_id"], job["id"])
            self.assertEqual(hashlib.sha256((media / Path(job["url"]).name).read_bytes()).hexdigest(), digest)

    def test_editor_exposes_trim_controls(self):
        html = (Path(app.__file__).parent / "static/index.html").read_text()
        for marker in ("be_trim_in", "be_trim_out", "be_trim_clear", "trim_in_seconds", "trim_out_seconds"):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
