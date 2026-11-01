import hashlib
import pytest
from media_lab_core import chatterbox_cpu


def test_exact_bytes_and_same_size_corruption(tmp_path, monkeypatch):
    payload = b'reviewed fixture'
    monkeypatch.setattr(chatterbox_cpu, 'FILES', {'fixture': (len(payload), hashlib.sha256(payload).hexdigest())})
    path = tmp_path / 'fixture'; path.write_bytes(payload)
    assert chatterbox_cpu.verified_bytes(tmp_path, 'fixture') == payload
    path.write_bytes(b'x' * len(payload))
    with pytest.raises(ValueError, match='hash'):
        chatterbox_cpu.verified_bytes(tmp_path, 'fixture')


def test_size_and_relative_directory_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(chatterbox_cpu, 'FILES', {'fixture': (12, '0'*64)})
    (tmp_path/'fixture').write_bytes(b'short')
    with pytest.raises(ValueError, match='size'):
        chatterbox_cpu.verified_bytes(tmp_path, 'fixture')
    with pytest.raises(ValueError, match='absolute'):
        chatterbox_cpu.verified_bytes('relative', 'fixture')


def test_explicit_default_voice_restores_nullable_fields(monkeypatch):
    import sys, types
    from unittest.mock import Mock
    values = {key: object() for key in ('t3.speaker_emb','t3.cond_prompt_speech_tokens','t3.emotion_adv',
        'gen.prompt_token','gen.prompt_token_len','gen.prompt_feat','gen.embedding')}
    verify = Mock(return_value=b'fixture')
    monkeypatch.setattr(chatterbox_cpu, '_verified_file', verify)
    cond = Mock(return_value='text-conditions')
    combined = Mock(return_value='voice-conditions')
    for name, attrs in {
        'safetensors.torch': {'load': lambda payload: values},
        'chatterbox.tts': {'Conditionals': combined},
        'chatterbox.models.t3.modules.cond_enc': {'T3Cond': cond},
    }.items():
        module=types.ModuleType(name);module.__dict__.update(attrs);monkeypatch.setitem(sys.modules,name,module)
    model=types.SimpleNamespace(conds=None)
    chatterbox_cpu.use_upstream_default_voice(model,'/reviewed/preset.safetensors')
    assert model.conds=='voice-conditions'
    assert cond.call_args.kwargs['clap_emb'] is None
    assert cond.call_args.kwargs['cond_prompt_speech_emb'] is None
    assert combined.call_args.args[1]['prompt_feat_len'] is None
    verify.assert_called_once_with('/reviewed/preset.safetensors',105316,
        '709e5a7fa80e010a011c8244f553853aed7a49c106fff54008fbd89a0f5a6148')
