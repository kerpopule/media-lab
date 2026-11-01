"""Probe tests — acceptance proof 1 reuse.

Probe stays read-only and side-effect-free: given a parsed meminfo mapping it
returns the exact total/available GB and confirms headless vs a live display
service.  These are the facts the capacity bar's top segment consumes.
"""
import unittest

from media_lab_core.probe import probe_headless_from_mapping, parse_meminfo

MEMINFO = """MemTotal:       127600748 kB
MemFree:         38650616 kB
MemAvailable:    63569960 kB
"""


class ParseMeminfo(unittest.TestCase):
    def test_precise_gb(self):
        facts = parse_meminfo(MEMINFO)
        self.assertAlmostEqual(facts["MemTotal"], 121.7, places=1)
        self.assertAlmostEqual(facts["MemFree"], 36.9, places=1)
        self.assertAlmostEqual(facts["MemAvailable"], 60.6, places=1)


class ProbeHeadless(unittest.TestCase):
    def test_headless_with_no_display_service(self):
        r = probe_headless_from_mapping(parse_meminfo(MEMINFO), services=[])
        self.assertTrue(r.headless)
        self.assertEqual(r.display_service, "none")
        self.assertEqual(r.source, "mapping")

    def test_display_service_present_marks_not_headless(self):
        r = probe_headless_from_mapping(parse_meminfo(MEMINFO), services=["Xorg"])
        self.assertFalse(r.headless)
        self.assertIn("Xorg", r.display_service)


if __name__ == "__main__":
    unittest.main(verbosity=2)