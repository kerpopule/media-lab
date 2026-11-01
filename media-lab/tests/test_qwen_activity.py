import pytest

from qwen_activity import parse_activity_gauges, probe_text_activity

IDLE = 'vllm:num_requests_running{engine="0",model_name="chosen model"} 0\nvllm:num_requests_waiting{engine="0"} 0\n'


def test_complete_scheduler_and_label_dimensions():
    assert parse_activity_gauges(IDLE) == (0, 0, True)
    assert parse_activity_gauges(IDLE + 'vllm:num_requests_running{engine="1"} 2 1234\n') == (2, 0, True)
    assert parse_activity_gauges('llamacpp:requests_processing 0\nllamacpp:requests_deferred 3\n') == (0, 3, True)


@pytest.mark.parametrize('body', ['', 'vllm:num_requests_running 0', 'vllm:num_requests_waiting 0', 'unrelated_metric{label="requests_running"} 0'])
def test_missing_gauge_family_is_unknown(body):
    assert parse_activity_gauges(body)[2] is False


@pytest.mark.parametrize('value', ['NaN', '+Inf', '-Inf', '-1', '0garbage', '1e999', '', '0 extra', '0 123 trailing'])
def test_malformed_recognized_sample_cannot_hide_behind_valid_zeroes(value):
    assert parse_activity_gauges(IDLE + f'vllm:num_requests_running {value}\n')[2] is False


def test_malformed_labels_are_unknown():
    assert parse_activity_gauges(IDLE + 'vllm:num_requests_running{engine="broken} 0\n')[2] is False


def test_probe_never_reports_incomplete_scheduler_idle(monkeypatch):
    monkeypatch.setattr('qwen_activity.prometheus_gauge_text', lambda *args, **kwargs: 'vllm:num_requests_running 0')
    assert probe_text_activity()[0] == 'unknown'
    monkeypatch.setattr('qwen_activity.prometheus_gauge_text', lambda *args, **kwargs: IDLE)
    assert probe_text_activity()[0] == 'idle'
    monkeypatch.setattr('qwen_activity.prometheus_gauge_text', lambda *args, **kwargs: IDLE + 'vllm:num_requests_waiting 1')
    assert probe_text_activity()[0] == 'busy'
