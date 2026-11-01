"""ACE-Step music pack: request contract, WAV gate, host verification and server routing."""
import hashlib
import io
import json
import math
import struct
import threading
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_lab_core import music_host as host
from media_lab_core import music_jobs
from media_lab_core.music_artifact import inspect_wav
from media_lab_core.music_request import decode_request
from media_lab_core.studio_cli import initialize, read_credentials, parser, application


def wav(seconds=6, rate=48000, channels=2, amp=9000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(channels); out.setsampwidth(2); out.setframerate(rate)
        frames = int(seconds * rate)
        out.writeframes(b''.join(struct.pack('<h', int(amp * math.sin(i / 17))) * channels for i in range(frames)))
    return buffer.getvalue()


def write_pack(tmp_path, monkeypatch):
    runtime = tmp_path/'python'; runtime.write_bytes(b'#!/bin/sh\nexit 0\n'); runtime.chmod(0o700)
    ck = tmp_path/'checkpoints'; (ck/'model').mkdir(parents=True)
    (ck/'model/weights.safetensors').write_bytes(b'w' * 100); (ck/'config.json').write_text('{}')
    rows = [{'path': 'model/weights.safetensors', 'bytes': 100, 'sha256': hashlib.sha256(b'w' * 100).hexdigest()},
            {'path': 'config.json', 'bytes': 2, 'sha256': hashlib.sha256(b'{}').hexdigest()}]
    manifest = tmp_path/'weights-manifest.json'; manifest.write_text(json.dumps({'files': rows}))
    lock = tmp_path/'inference.lock'
    config = {'version': 1, 'runtime': str(runtime), 'runtime_sha256': host.sha256_file(runtime), 'checkpoints': str(ck),
              'weights_manifest': str(manifest), 'inference_lock': str(lock), 'revision': 'acestep-test-rev'}
    path = tmp_path/'music.json'; path.write_text(json.dumps(config))
    return path, config


def test_request_and_gate_bounds():
    good = json.dumps({'prompt': 'calm piano', 'lyrics': '[inst]', 'seconds': 20, 'seed': 7}).encode()
    assert decode_request(good)['seconds'] == 20
    for bad in ({'prompt': '', 'lyrics': '', 'seconds': 20, 'seed': 7}, {'prompt': 'x', 'lyrics': '', 'seconds': 5, 'seed': 7},
                {'prompt': 'x', 'lyrics': '', 'seconds': 20, 'seed': -1}, {'prompt': 'x', 'lyrics': '', 'seconds': 20}):
        with pytest.raises(ValueError):
            decode_request(json.dumps(bad).encode())
    meta = inspect_wav(wav())
    assert (meta['sampleRate'], meta['channels'], round(meta['durationSeconds'])) == (48000, 2, 6)
    with pytest.raises(ValueError, match='48 kHz stereo'):
        inspect_wav(wav(channels=1))
    with pytest.raises(ValueError, match='silent'):
        inspect_wav(wav(amp=1))
    with pytest.raises(ValueError, match='duration'):
        inspect_wav(wav(seconds=2))


def test_payload_validation_requires_exact_engine_and_settings():
    ok = {'engineId': 'acestep-gpu', 'revision': 'r', 'kind': 'audio', 'prompt': 'calm piano',
          'settings': {'operation': 'compose', 'lyrics': '[inst]', 'seconds': 20, 'seed': 7}}
    music_jobs.validate_payload(ok, 'r')
    for bad in ({**ok, 'revision': 'other'}, {**ok, 'settings': {**ok['settings'], 'seconds': 500}},
                {**ok, 'settings': {**ok['settings'], 'operation': 'speak'}}, {**ok, 'prompt': ''}):
        with pytest.raises(ValueError):
            music_jobs.validate_payload(bad, 'r')


def test_verify_pack_checks_every_weight_file(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    assert host.verify_pack(config) == 2
    (Path(config['checkpoints'])/'model/weights.safetensors').write_bytes(b'W' * 100)
    with pytest.raises(ValueError, match='does not match its manifest'):
        host.verify_pack(config)


def test_host_advertises_only_when_verified(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    entered = threading.Event()
    monkeypatch.setattr(host.music_jobs, 'run_next', lambda *a, **k: entered.set())
    instance = host.MusicHost(lambda: object(), tmp_path/'artifacts', path)
    instance.start()
    try:
        assert entered.wait(3)
        assert instance.engines()[0]['id'] == 'acestep-gpu' and instance.engines()[0]['operation'] == 'compose'
        assert instance.verified_files == 2
    finally:
        instance.stop()
    assert instance.engines() == []


def test_gpu_lease_refuses_when_held(tmp_path):
    import fcntl
    from media_lab_core.music_worker import gpu_lease
    from media_lab_core.cpu_worker import WorkerBusy
    lock = tmp_path/'inference.lock'
    holder = open(lock, 'a+'); fcntl.flock(holder, fcntl.LOCK_EX)
    with pytest.raises(WorkerBusy):
        with gpu_lease(lock): pass
    fcntl.flock(holder, fcntl.LOCK_UN)
    with gpu_lease(lock) as fd:
        assert fd


def test_server_routes_music_jobs_and_saves_to_library(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    root = tmp_path/'host'; initialize(root)
    monkeypatch.setattr(host.music_jobs, 'run_next', lambda *a, **k: None)
    app = application(parser().parse_args(['serve', str(root), '--music-config', str(path)]))
    with TestClient(app) as client:
        assert client.get('/manifest.json').json()['vibexStudio']['music'] is True
        paired = client.post('/api/gate', json={'code': read_credentials(root).code, 'studio_render': True, 'studio_library': True, 'studio_device': 'e'*32}).json()
        render = {'Authorization': 'Bearer ' + paired['renderToken']}
        for _ in range(30):
            engines = client.get('/api/studio/engines', headers=render).json()['engines']
            if engines: break
            threading.Event().wait(0.1)
        assert [e['id'] for e in engines] == ['acestep-gpu']
        good = {'requestId': 'music-request-00001', 'engineId': 'acestep-gpu', 'revision': 'acestep-test-rev', 'kind': 'audio',
                'prompt': 'calm piano', 'settings': {'operation': 'compose', 'lyrics': '[inst]', 'seconds': 20, 'seed': 7}}
        assert client.post('/api/studio/jobs', headers=render, json={**good, 'settings': {**good['settings'], 'seconds': 1}}).status_code == 409
        job = client.post('/api/studio/jobs', headers=render, json=good).json()
        store = app.state.background_host.get_store()
        claimed = store.claim_cpu_job('w', 'acestep-gpu', kind='audio'); assert claimed['id'] == job['id']
        data = wav(); meta = inspect_wav(data)
        (root/'artifacts'/job['id']).mkdir(parents=True); (root/'artifacts'/job['id']/'output.wav').write_bytes(data)
        store.transition(job['id'], 'w', 'succeeded', result={'artifact': {**meta, 'path': job['id'] + '/output.wav', 'recovered': False}})
        content = client.get(f"/api/studio/jobs/{job['id']}/content", headers=render)
        assert content.status_code == 200 and content.headers['content-type'].startswith('audio/wav')
        saved = client.post(f"/api/studio/jobs/{job['id']}/library", headers=render)
        assert saved.status_code == 200, saved.text
        rows = json.loads((root/'library.json').read_text())
        assert rows[-1]['url'].startswith('/media/Audio/Music/') and rows[-1]['engine'] == 'Music (ACE-Step)' and rows[-1]['title'] == 'calm piano'
