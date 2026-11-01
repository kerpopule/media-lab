#!/usr/bin/env python3
import math
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from runner.audio_signal_gate import audio_signal_metrics


class AudioSignalGateTest(unittest.TestCase):
    def make_wav(self, path, values, sr=8000):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(struct.pack("<" + "h" * len(values), *values))

    def test_all_zero_audio_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "silent.wav"
            self.make_wav(p, [0] * 8000)
            m = audio_signal_metrics(p)
            self.assertFalse(m["ok"], m)
            self.assertEqual("silent-or-near-silent", m["reason"])

    def test_real_signal_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tone.wav"
            values = [int(2000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8000)]
            self.make_wav(p, values)
            m = audio_signal_metrics(p)
            self.assertTrue(m["ok"], m)
            self.assertGreater(m["rms"], 8)

    def test_missing_audio_is_rejected(self):
        m = audio_signal_metrics("/definitely/missing.wav")
        self.assertFalse(m["ok"], m)
        self.assertEqual("missing-or-empty", m["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
