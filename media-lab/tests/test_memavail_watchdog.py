"""Execute watchdog with stub commands; no real /proc or systemctl."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'runner/memavail-watchdog.sh'

class WatchdogSafety(unittest.TestCase):
    def run_fixture(self, available=1024, active=True, count=2, latched=True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bins = root / 'bin'
            bins.mkdir()
            stubs = {
                'grep': '#!/bin/bash\nprintf "%s %s kB\\n" "${1#^}" "$AVAILABLE"\n',
                'systemctl': '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\nif [[ "$*" == *is-active* ]]; then exit "$INACTIVE"; fi\nexit 0\n',
                'logger': '#!/bin/bash\nexit 0\n',
            }
            for name, text in stubs.items():
                p = bins / name
                p.write_text(text)
                p.chmod(0o700)
            (root / 'flashnext-memwatch.count').write_text(str(count))
            if latched:
                (root / 'flashnext-memwatch.latch').write_text('previous trip')
            env = dict(os.environ, PATH=str(bins)+os.pathsep+os.environ['PATH'],
                       XDG_RUNTIME_DIR=tmp, LOG=str(root/'watch.log'),
                       CALLS=str(root/'calls'), FLOOR_GIB='8', CONSECUTIVE='3',
                       UNIT='media-lab-sol-h3.service', CONTAINER='none',
                       AVAILABLE=str(available), INACTIVE='0' if active else '3')
            p = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=5)
            return p, (root/'calls').read_text(), (root/'flashnext-memwatch.latch').exists()

    def test_old_latch_does_not_disable_stop_after_relaunch(self):
        p, calls, latched = self.run_fixture()
        self.assertIn('--user stop media-lab-sol-h3.service', calls, p.stderr)
        self.assertEqual(p.returncode, 2)
        self.assertTrue(latched)

    def test_healthy_restarted_unit_is_not_stopped(self):
        p, calls, latched = self.run_fixture(available=12*1048576)
        self.assertNotIn('--user stop', calls)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(latched)

    def test_inactive_unit_is_not_stopped_again(self):
        p, calls, latched = self.run_fixture(active=False)
        self.assertNotIn('--user stop', calls)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(latched)

    def test_one_low_sample_is_not_enough(self):
        p, calls, latched = self.run_fixture(count=0, latched=False)
        self.assertNotIn('--user stop', calls)
        self.assertEqual(p.returncode, 0)
        self.assertFalse(latched)

if __name__ == '__main__':
    unittest.main()
