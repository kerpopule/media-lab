import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import coupled_av_trim as c


class CoupledAvTrimTests(unittest.TestCase):
    def test_visible_dialogue_command_has_one_timeline_input(self):
        cmd = c.build_command(Path("source.mp4"), Path("out.mp4"), 0.1, 1.5)
        self.assertEqual(cmd.count("-i"), 1)
        self.assertIn(["-map", "0:v:0", "-map", "0:a:0"], [cmd[i:i+4] for i in range(len(cmd)-3)])
        self.assertNotIn("-itsoffset", cmd)

    def test_real_coupled_trim_keeps_audio_and_duration(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td); src = td / "src.mp4"; out = td / "out.mp4"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x176:rate=24:duration=2", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
            receipt = c.coupled_av_trim(src, out, 0.4, 0.8, width=320, height=176)
            self.assertTrue(out.is_file())
            self.assertTrue(receipt["coupled_av_trim"])
            self.assertLess(abs(receipt["duration_seconds"] - 0.8), 0.08)
            self.assertTrue(receipt["has_audio"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
