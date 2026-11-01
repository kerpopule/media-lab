"""Experimental speech host: verified configuration, advertisement and server wiring."""
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_lab_core import speech_host as host
from media_lab_core.chatterbox_cpu import FILES
from media_lab_core.studio_cli import initialize, read_credentials, parser, application


def write_pack(tmp_path, monkeypatch):
    """Fixture artifacts with matching pinned sizes/hashes, no real model."""
    runtime = tmp_path/'python'
    runtime.write_bytes(b'#!/bin/sh\nexit 0\n')
    runtime.chmod(0o700)
    models = tmp_path/'models'; models.mkdir()
    fake = {}
    for name in FILES:
        data = b'x' * 64
        (models/name).write_bytes(data)
        fake[name] = (64, hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(host, 'FILES', fake)
    monkeypatch.setattr('media_lab_core.chatterbox_cpu.FILES', fake)
    watermark = tmp_path/'perth.safetensors'; watermark.write_bytes(b'w' * 32)
    voice = tmp_path/'voice.safetensors'; voice.write_bytes(b'v' * 16)
    monkeypatch.setattr(host, 'MODEL_BYTES', 32)
    monkeypatch.setattr(host, 'MODEL_SHA256', hashlib.sha256(b'w' * 32).hexdigest())
    monkeypatch.setattr(host, 'VOICE_BYTES', 16)
    monkeypatch.setattr(host, 'VOICE_SHA256', hashlib.sha256(b'v' * 16).hexdigest())
    config = {'version': 1, 'runtime': str(runtime), 'runtime_sha256': host.sha256_file(runtime),
              'models': str(models), 'watermark': str(watermark), 'voice': str(voice), 'revision': 'speech-test-rev'}
    path = tmp_path/'speech.json'
    path.write_text(json.dumps(config))
    return path, config


def test_config_requires_exact_fields_and_absolute_paths(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    assert host.read_config(path)['revision'] == 'speech-test-rev'
    with pytest.raises(ValueError, match='absolute'):
        host.read_config('relative.json')
    for broken in ({**config, 'extra': 1}, {**config, 'models': 'models'}, {**config, 'revision': ''}, {**config, 'runtime_sha256': 'short'}):
        path.write_text(json.dumps(broken))
        with pytest.raises(ValueError):
            host.read_config(path)
    link = tmp_path/'link.json'; link.symlink_to(path)
    with pytest.raises(OSError):
        host.read_config(link)


def test_verify_pack_detects_any_artifact_drift(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    assert host.verify_pack(config)
    Path(config['voice']).write_bytes(b'V' * 16)
    with pytest.raises(ValueError, match='reviewed hash'):
        host.verify_pack(config)
    Path(config['voice']).write_bytes(b'v' * 16)
    Path(config['runtime']).write_bytes(b'#!/bin/sh\nexit 1\n')
    with pytest.raises(ValueError, match='changed'):
        host.verify_pack(config)


def test_host_advertises_only_while_verified_and_running(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    entered = threading.Event()
    monkeypatch.setattr(host.speech_jobs, 'run_next', lambda *a, **k: entered.set())
    instance = host.SpeechHost(lambda: object(), tmp_path/'artifacts', path)
    assert instance.engines() == []
    instance.start()
    try:
        assert entered.wait(3)
        engine = instance.engines()[0]
        assert engine['id'] == 'chatterbox-english-cpu' and engine['operation'] == 'speak' and engine['revision'] == 'speech-test-rev'
        with pytest.raises(Exception):
            instance.admit({'engineId': 'chatterbox-english-cpu', 'revision': 'other', 'kind': 'audio'})
    finally:
        instance.stop()
    assert instance.engines() == []
    # Drift after a restart is refused before any advertisement.
    Path(config['watermark']).write_bytes(b'W' * 32)
    instance.start(); instance.thread.join(3)
    assert instance.engines() == [] and instance.error


def test_unconfigured_host_is_inert(tmp_path):
    instance = host.SpeechHost(lambda: object(), tmp_path, None)
    instance.start()
    assert instance.thread is None and instance.engines() == []
    with pytest.raises(Exception):
        instance.admit({})


def test_server_routes_speech_jobs_and_saves_wav_to_library(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    root = tmp_path/'host'; initialize(root)
    monkeypatch.setattr(host.speech_jobs, 'run_next', lambda *a, **k: None)
    args = parser().parse_args(['serve', str(root), '--speech-config', str(path)])
    app = application(args)
    with TestClient(app) as client:
        assert client.get('/manifest.json').json()['vibexStudio']['speech'] is True
        code = read_credentials(root).code
        paired = client.post('/api/gate', json={'code': code, 'studio_render': True, 'studio_library': True, 'studio_device': 'c'*32}).json()
        render = {'Authorization': 'Bearer ' + paired['renderToken']}
        app.state.speech_host.thread.join(3) if not app.state.speech_host.ready else None
        for _ in range(30):
            engines = client.get('/api/studio/engines', headers=render).json()['engines']
            if engines: break
            threading.Event().wait(0.1)
        assert [e['id'] for e in engines] == ['chatterbox-english-cpu']
        good = {'requestId': 'speech-request-0001', 'engineId': 'chatterbox-english-cpu', 'revision': 'speech-test-rev',
                'kind': 'audio', 'prompt': 'Welcome to your creative studio.',
                'settings': {'operation': 'speak', 'voice': 'upstream-default-english', 'seed': 7}}
        assert client.post('/api/studio/jobs', headers=render, json={**good, 'engineId': 'birefnet-cpu'}).status_code == 503
        assert client.post('/api/studio/jobs', headers=render, json={**good, 'settings': {**good['settings'], 'seed': -1}}).status_code == 409
        submitted = client.post('/api/studio/jobs', headers=render, json=good)
        assert submitted.status_code == 200, submitted.text
        job = submitted.json(); assert job['kind'] == 'audio' and job['status'] == 'queued'
        # Publish a synthetic accepted result directly, then save it to Library.
        import math, struct, wave, io
        frames = 24000
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
            out.writeframes(b''.join(struct.pack('<h', int(12000*math.sin(i/20))) for i in range(frames)))
        data = buffer.getvalue()
        from media_lab_core.speech_artifact import inspect_wav
        meta = inspect_wav(data)
        store = app.state.background_host.get_store()
        jid = job['id']
        claimed = store.claim_cpu_job('w-test', 'chatterbox-english-cpu', kind='audio')
        assert claimed['id'] == jid
        (root/'artifacts'/jid).mkdir(parents=True)
        (root/'artifacts'/jid/'output.wav').write_bytes(data)
        store.transition(jid, 'w-test', 'succeeded', result={'artifact': {**meta, 'path': jid + '/output.wav', 'recovered': False}})
        content = client.get(f'/api/studio/jobs/{jid}/content', headers=render)
        assert content.status_code == 200 and content.headers['content-type'].startswith('audio/wav')
        saved = client.post(f'/api/studio/jobs/{jid}/library', headers=render)
        assert saved.status_code == 200, saved.text
        assert saved.json()['kind'] == 'audio'
        rows = json.loads((root/'library.json').read_text())
        assert rows[-1]['url'].startswith('/media/Audio/Speech/') and rows[-1]['title'] == 'Welcome to your creative studio.'
        library = {'Authorization': 'Bearer ' + paired['token']}
        assert any(a['id'] == saved.json()['id'] for a in client.get('/api/studio/library', headers=library).json()['assets'])


def test_speech_config_must_be_absolute_and_bundle_includes_speech_modules(tmp_path):
    from media_lab_core.studio_server import create_paired_app
    from media_lab_core.studio_gate import Credentials
    from media_lab_core.studio_source_bundle import MODULES
    with pytest.raises(ValueError, match='absolute'):
        create_paired_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts', media_root=tmp_path/'media',
                          load_rows=lambda: [], credentials=Credentials('a'*64, 'b'*64), speech_config=Path('speech.json'))
    assert {'speech_host', 'speech_jobs', 'speech_worker', 'speech_render', 'speech_request', 'chatterbox_cpu', 'perth_cpu'} <= set(MODULES)
