import hashlib
import sys
import types
from unittest.mock import Mock
import pytest
from media_lab_core import perth_cpu


def test_rejects_wrong_bytes_before_runtime(tmp_path):
    path = tmp_path / 'model.safetensors'
    path.write_bytes(b'not a model')
    with pytest.raises(ValueError, match='reviewed artifact'):
        perth_cpu.load_watermarker(path)
    with pytest.raises(ValueError, match='absolute'):
        perth_cpu.load_watermarker('model.safetensors')


def runtime(monkeypatch, tmp_path):
    payload = b'fixture model bytes'
    path = tmp_path / 'fixture.safetensors'
    path.write_bytes(payload)
    monkeypatch.setattr(perth_cpu, 'MODEL_BYTES', len(payload))
    monkeypatch.setattr(perth_cpu, 'MODEL_SHA256', hashlib.sha256(payload).hexdigest())
    model = Mock(); model.to.return_value = model
    constructor = Mock(return_value=model); decoder = Mock(return_value={'fixture': 'tensor'}); wrapper = Mock()
    for name, attrs in {
        'safetensors.torch': {'load': decoder},
        'perth.perth_net.perth_net_implicit.config': {'PerthConfig': lambda **kw: kw},
        'perth.perth_net.perth_net_implicit.model.perth_net': {'PerthNet': constructor},
        'perth.perth_net.perth_net_implicit.perth_watermarker': {'PerthImplicitWatermarker': wrapper},
    }.items():
        module = types.ModuleType(name); module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    return path, model, constructor, decoder, wrapper


def test_strict_state_cpu_and_explicit_model(monkeypatch, tmp_path):
    path, model, constructor, decoder, wrapper = runtime(monkeypatch, tmp_path)
    perth_cpu.load_watermarker(path)
    constructor.assert_called_once_with(perth_cpu.CONFIG)
    decoder.assert_called_once_with(b'fixture model bytes')
    model.load_state_dict.assert_called_once_with({'fixture': 'tensor'}, strict=True)
    model.to.assert_called_once_with('cpu'); model.eval.assert_called_once()
    wrapper.assert_called_once_with(run_name=None, perth_net=model)


def test_state_mismatch_never_creates_watermarker(monkeypatch, tmp_path):
    path, model, _, _, wrapper = runtime(monkeypatch, tmp_path)
    model.load_state_dict.side_effect = RuntimeError('state mismatch')
    with pytest.raises(RuntimeError, match='state mismatch'):
        perth_cpu.load_watermarker(path)
    wrapper.assert_not_called()


def test_same_size_corruption_is_rejected_before_decode(monkeypatch, tmp_path):
    path, _, constructor, decoder, wrapper = runtime(monkeypatch, tmp_path)
    path.write_bytes(b'x' * path.stat().st_size)
    with pytest.raises(ValueError, match='reviewed artifact'):
        perth_cpu.load_watermarker(path)
    constructor.assert_not_called(); decoder.assert_not_called(); wrapper.assert_not_called()
