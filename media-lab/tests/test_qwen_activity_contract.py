"""Focused fixtures for the runtime-neutral Qwen activity probe.

Covers the four defect classes the pre-repair helper got wrong:
  * Prometheus TEXT from /metrics (not JSON) must parse — running AND waiting.
  * SGLang equivalents must be recognized.
  * Unknown / unreachable RESIDENT must fail closed to busy (block eviction).
  * A truthful idle from a reachable runtime is the ONLY idle.
  * The :8003 shim is deduplicated so one authoritative backend is sampled.

This module imports only qwen_activity (pure; no FastAPI / process side
effects), never app.py, which starts the studio worker threads at import.
"""

import unittest
from unittest import mock

from qwen_activity import parse_activity_gauges, probe_text_activity

VLLM_BUSY = (
    '# HELP vllm:num_requests_running Number of running requests\n'
    '# TYPE vllm:num_requests_running gauge\n'
    'vllm:num_requests_running{engine="0",model_name="dsv4"} 1.0\n'
    '# HELP vllm:num_requests_waiting Number of waiting requests\n'
    '# TYPE vllm:num_requests_waiting gauge\n'
    'vllm:num_requests_waiting{engine="0",model_name="dsv4"} 0.0\n'
)

VLLM_WAITING = (
    'vllm:num_requests_running{engine="0",model_name="dsv4"} 0.0\n'
    'vllm:num_requests_waiting{engine="0",model_name="dsv4"} 2.0\n'
)

VLLM_IDLE = (
    'vllm:num_requests_running{engine="0",model_name="dsv4"} 0.0\n'
    'vllm:num_requests_waiting{engine="0",model_name="dsv4"} 0.0\n'
)

SGLANG_SYNONYMS = (
    'sglang:num_running_reqs{model_name="srv"} 1.0\n'
    'sglang:num_queue_reqs 0.0\n'
)

LLAMACPP_BUSY = (
    'llamacpp:requests_processing 1\n'
    'llamacpp:requests_deferred 0\n'
)

LLAMACPP_IDLE = (
    'llamacpp:requests_processing 0\n'
    'llamacpp:requests_deferred 0\n'
)

# A body that answered but exposes no recognized running/waiting gauge.
NO_GAUGE = (
    '# HELP vllm:uptime_seconds Uptime\n'
    '# TYPE vllm:uptime_seconds gauge\n'
    'vllm:uptime_seconds 12345.0\n'
)


def _prometheus_gauge_text(endpoints, raw_by_port):
    """Return a fake prometheus_gauge_text bound to raw bodies per port."""
    bodies = {f"http://127.0.0.1:{port}/metrics": body
              for port, body in raw_by_port.items()}

    def fake(url, timeout=3):
        if url not in bodies:
            raise OSError(f"unreachable {url}")
        body = bodies[url]
        if isinstance(body, Exception):
            raise body
        return body

    return fake


class ParseActivityGaugesTest(unittest.TestCase):
    def test_parses_prometheus_text_running_and_waiting(self):
        running, waiting, saw = parse_activity_gauges(VLLM_BUSY)
        self.assertTrue(saw)
        self.assertEqual(running, 1.0)
        self.assertEqual(waiting, 0.0)

    def test_counts_waiting_requests(self):
        running, waiting, saw = parse_activity_gauges(VLLM_WAITING)
        self.assertTrue(saw)
        self.assertEqual(running, 0.0)
        self.assertEqual(waiting, 2.0)

    def test_idle_is_a_truthful_zero(self):
        running, waiting, saw = parse_activity_gauges(VLLM_IDLE)
        self.assertTrue(saw)
        self.assertEqual((running, waiting), (0.0, 0.0))

    def test_recognizes_sglang_equivalents(self):
        running, waiting, saw = parse_activity_gauges(SGLANG_SYNONYMS)
        self.assertTrue(saw)
        self.assertEqual(running, 1.0)
        self.assertEqual(waiting, 0.0)

    def test_recognizes_llamacpp_processing_and_deferred(self):
        running, waiting, saw = parse_activity_gauges(LLAMACPP_BUSY)
        self.assertTrue(saw)
        self.assertEqual((running, waiting), (1.0, 0.0))

        running, waiting, saw = parse_activity_gauges(LLAMACPP_IDLE)
        self.assertTrue(saw)
        self.assertEqual((running, waiting), (0.0, 0.0))

    def test_empty_and_comment_only_bodies_are_unknown(self):
        running, waiting, saw = parse_activity_gauges("")
        self.assertFalse(saw)
        running, waiting, saw = parse_activity_gauges("# HELP nothing\n# TYPE nothing gauge\n")
        self.assertFalse(saw)

    def test_no_recognized_gauge_means_unknown(self):
        running, waiting, saw = parse_activity_gauges(NO_GAUGE)
        self.assertFalse(saw)
        self.assertEqual((running, waiting), (0.0, 0.0))


class ProbeActivityTest(unittest.TestCase):
    @mock.patch("qwen_activity.prometheus_gauge_text")
    def test_busy_running_blocks_eviction(self, gt):
        gt.side_effect = _prometheus_gauge_text(1, {8004: VLLM_BUSY})
        state, detail = probe_text_activity()
        self.assertEqual(state, "busy")
        # Verdict must NOT be idle even though a later shim answer could idle.
        self.assertEqual(detail["source"], "8004")
        self.assertEqual(detail["running"], 1.0)

    @mock.patch("qwen_activity.prometheus_gauge_text")
    def test_waiting_requests_are_busy(self, gt):
        # Only :8004 serves; the :8003 shim is unreachable.
        gt.side_effect = _prometheus_gauge_text(1, {8004: VLLM_WAITING})
        state, detail = probe_text_activity()
        self.assertEqual(state, "busy")
        self.assertEqual(detail["waiting"], 2.0)

    @mock.patch("qwen_activity.prometheus_gauge_text")
    def test_idle_reachable_runtime_is_idle(self, gt):
        gt.side_effect = _prometheus_gauge_text(1, {8004: VLLM_IDLE})
        state, detail = probe_text_activity()
        self.assertEqual(state, "idle")

    @mock.patch("qwen_activity.prometheus_gauge_text")
    def test_sglang_busy_is_busy(self, gt):
        gt.side_effect = _prometheus_gauge_text(1, {8004: SGLANG_SYNONYMS})
        state, detail = probe_text_activity()
        self.assertEqual(state, "busy")

    @mock.patch("qwen_activity.prometheus_gauge_text")
    def test_reachable_no_gauge_is_unknown_not_idle(self, gt):
        gt.side_effect = _prometheus_gauge_text(1, {8004: NO_GAUGE})
        state, detail = probe_text_activity()
        self.assertEqual(state, "unknown")

    @mock.patch("qwen_activity.prometheus_gauge_text")
    def test_all_unreachable_is_unknown_not_idle(self, gt):
        # The old helper returned False (idle) when every probe was unreadable;
        # a resident text slot with an unreadable runtime must block eviction.
        gt.side_effect = _prometheus_gauge_text(1, {})
        state, detail = probe_text_activity()
        self.assertEqual(state, "unknown")
        self.assertEqual(detail["probes"], [])

    @mock.patch("qwen_activity.prometheus_gauge_text")
    def test_duplicate_shim_sampled_once_strictest_wins(self, gt):
        # :8004 is authoritative. The :8003 OpenAI shim is skipped rather than
        # sampled as a second copy of the same scheduler.
        gt.side_effect = _prometheus_gauge_text(
            1, {8004: VLLM_IDLE, 8003: VLLM_IDLE})
        state, detail = probe_text_activity()
        self.assertEqual(state, "idle")
        self.assertEqual(detail["source"], "8004")
        self.assertEqual(detail["skipped_shims"], {"8003": "8004"})
        self.assertEqual(gt.call_count, 1)

    @mock.patch("qwen_activity.prometheus_gauge_text")
    def test_idle_peer_never_downgrades_busy(self, gt):
        # A busy body on the shim is ignored; the canonical body is the only
        # authority for residency activity.
        gt.side_effect = _prometheus_gauge_text(
            1, {8004: VLLM_IDLE, 8003: VLLM_BUSY})
        state, detail = probe_text_activity()
        self.assertEqual(state, "idle")
        self.assertEqual(detail["source"], "8004")
        self.assertEqual(gt.call_count, 1)

    @mock.patch("qwen_activity.prometheus_gauge_text")
    def test_metric_label_text_cannot_fake_activity(self, gt):
        body = 'vllm:uptime_seconds{note="running_requests"} 123.0\n'
        gt.side_effect = _prometheus_gauge_text(1, {8004: body})
        state, detail = probe_text_activity()
        self.assertEqual(state, "unknown")
        self.assertEqual(detail["probe_status"]["8004"]["activity"], "unreadable")


if __name__ == "__main__":
    unittest.main()