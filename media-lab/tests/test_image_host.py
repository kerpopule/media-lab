"""Qwen-Image-2.1 image pack: request contract, PNG gate, host verification and server routing."""
import io
import json
import threading
from pathlib import Path

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from media_lab_core import image_host as host
from media_lab_core import image_jobs
from media_lab_core.image_artifact import inspect_png
from media_lab_core.image_request import decode_request
from media_lab_core.studio_cli import initialize, read_credentials, parser, application


def png(size=(1024, 1024), fmt='PNG'):
    buffer = io.BytesIO(); Image.new('RGB', size, (200, 120, 40)).save(buffer, format=fmt); return buffer.getvalue()


def write_pack(tmp_path):
    runtime = tmp_path/'python'; runtime.write_bytes(b'#!/bin/sh\nexit 0\n'); runtime.chmod(0o700)
    ck = tmp_path/'checkpoints'; ck.mkdir(); (ck/'w.safetensors').write_bytes(b'w' * 50)
    manifest = tmp_path/'weights-manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'w.safetensors', 'bytes': 50, 'sha256': host.sha256_file(ck/'w.safetensors')}]}))
    config = {'version': 1, 'runtime': str(runtime), 'runtime_sha256': host.sha256_file(runtime), 'checkpoints': str(ck),
              'weights_manifest': str(manifest), 'inference_lock': str(tmp_path/'lock'), 'revision': 'qwen-image-21-test-rev', 'resident_idle_seconds': 0}
    path = tmp_path/'image.json'; path.write_text(json.dumps(config))
    return path, config


def test_request_bounds():
    good = {'prompt': 'a cat', 'size': '1024*1024', 'steps': 9, 'seed': 7}
    assert decode_request(json.dumps(good).encode())['steps'] == 9
    for bad in ({**good, 'size': '512*512'}, {**good, 'steps': 2}, {**good, 'steps': 60}, {**good, 'seed': -1}, {**good, 'prompt': ' '},
                {k: v for k, v in good.items() if k != 'size'}):
        with pytest.raises(ValueError):
            decode_request(json.dumps(bad).encode())


def test_png_gate_accepts_supported_sizes_and_rejects_others():
    meta = inspect_png(png())
    assert (meta['width'], meta['height'], meta['mimeType']) == (1024, 1024, 'image/png')
    assert inspect_png(png((768, 1280)))['height'] == 1280
    for bad in (png((512, 512)), png(fmt='JPEG'), b'\x89PNG' + b'\x00' * 100, png()[:-200]):
        with pytest.raises(ValueError):
            inspect_png(bad)


def test_payload_validation_requires_exact_engine_and_settings():
    good = {'engineId': image_jobs.ENGINE, 'revision': 'r', 'kind': 'image', 'prompt': 'cat',
            'settings': {'operation': 'text-to-image', 'size': '1024*1024', 'steps': 9, 'seed': 1}}
    image_jobs.validate_payload(good, 'r')
    for bad in ({**good, 'revision': 'x'}, {**good, 'kind': 'video'}, {**good, 'settings': {**good['settings'], 'size': '640*640'}},
                {**good, 'settings': {**good['settings'], 'inputId': 'x'}}, {**good, 'settings': {**good['settings'], 'operation': 'compose'}}):
        with pytest.raises(ValueError):
            image_jobs.validate_payload(bad, 'r')


def test_verify_pack_checks_runtime_and_weights(tmp_path):
    path, config = write_pack(tmp_path)
    assert host.verify_pack(host.read_config(path)) == 1
    (tmp_path/'checkpoints/w.safetensors').write_bytes(b'x' * 50)
    with pytest.raises(ValueError, match='manifest'):
        host.verify_pack(config)
    with pytest.raises(ValueError):
        host.read_config(Path('relative.json'))
    path.write_text(json.dumps({**config, 'memory_gib': 4}))
    with pytest.raises(ValueError, match='memory_gib'):
        host.read_config(path)


def test_server_routes_image_jobs_and_saves_generated_images(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path)
    root = tmp_path/'host'; initialize(root)
    monkeypatch.setattr(host.image_jobs, 'run_next', lambda *a, **k: None)
    app = application(parser().parse_args(['serve', str(root), '--image-config', str(path)]))
    with TestClient(app) as client:
        assert client.get('/manifest.json').json()['vibexStudio']['image'] is True
        paired = client.post('/api/gate', json={'code': read_credentials(root).code, 'studio_render': True, 'studio_library': True, 'studio_device': 'e'*32}).json()
        render = {'Authorization': 'Bearer ' + paired['renderToken']}
        for _ in range(50):
            engines = client.get('/api/studio/engines', headers=render).json()['engines']
            if engines: break
            threading.Event().wait(0.1)
        assert [e['id'] for e in engines] == [image_jobs.ENGINE] and engines[0]['operation'] == 'text-to-image'
        good = {'requestId': 'image-request-00001', 'engineId': image_jobs.ENGINE, 'revision': 'qwen-image-21-test-rev', 'kind': 'image',
                'prompt': 'a sleeping cat', 'settings': {'operation': 'text-to-image', 'size': '1024*1024', 'steps': 9, 'seed': 7}}
        assert client.post('/api/studio/jobs', headers=render, json={**good, 'settings': {**good['settings'], 'steps': 1}}).status_code == 409
        job = client.post('/api/studio/jobs', headers=render, json=good).json()
        store = app.state.background_host.get_store()
        claimed = store.claim_cpu_job('w', image_jobs.ENGINE, kind='image'); assert claimed['id'] == job['id']
        data = png(); meta = inspect_png(data)
        (root/'artifacts'/job['id']).mkdir(parents=True); (root/'artifacts'/job['id']/'output.png').write_bytes(data)
        store.transition(job['id'], 'w', 'succeeded', result={'artifact': {**meta, 'path': job['id'] + '/output.png', 'recovered': False}})
        content = client.get(f"/api/studio/jobs/{job['id']}/content", headers=render)
        assert content.status_code == 200 and content.headers['content-type'].startswith('image/png')
        saved = client.post(f"/api/studio/jobs/{job['id']}/library", headers=render)
        assert saved.status_code == 200, saved.text
        rows = json.loads((root/'library.json').read_text())
        assert rows[-1]['url'].startswith('/media/Images/Generated/') and rows[-1]['engine'] == 'Image (Qwen-Image-2.1)' and rows[-1]['title'] == 'a sleeping cat'
