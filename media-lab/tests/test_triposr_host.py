"""Candidate 3D host: verified configuration, advertisement and server wiring."""
import hashlib
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_lab_core import triposr_host as host
from media_lab_core.triposr_cpu import MODEL_SHA, VARIANT
from media_lab_core.studio_cli import initialize, read_credentials, parser, application


def write_pack(tmp_path, monkeypatch):
    runtime = tmp_path/'python'; runtime.write_bytes(b'#!/bin/sh\nexit 0\n'); runtime.chmod(0o700)
    package = tmp_path/'package'; package.mkdir()
    monkeypatch.setattr(host, 'verify_package', lambda root: root)
    config = {'version': 1, 'runtime': str(runtime), 'runtime_sha256': host.sha256_file(runtime),
              'package': str(package), 'profile': 'original'}
    path = tmp_path/'triposr.json'; path.write_text(json.dumps(config))
    return path, config


def test_config_requires_reviewed_profile_and_absolute_paths(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    assert host.read_config(path)['profile'] == 'original'
    for broken in ({**config, 'profile': 'auto'}, {**config, 'package': 'pkg'}, {**config, 'extra': 1}):
        path.write_text(json.dumps(broken))
        with pytest.raises(ValueError):
            host.read_config(path)


def test_verify_pack_checks_interpreter_and_package(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    assert host.verify_pack(config)
    Path(config['runtime']).write_bytes(b'#!/bin/sh\nexit 1\n')
    with pytest.raises(ValueError, match='changed'):
        host.verify_pack(config)
    Path(config['runtime']).write_bytes(b'#!/bin/sh\nexit 0\n')
    monkeypatch.setattr(host, 'verify_package', lambda root: (_ for _ in ()).throw(ValueError('Model component hash mismatch')))
    with pytest.raises(ValueError, match='hash mismatch'):
        host.verify_pack(config)


def test_host_advertises_exact_variant_only_while_verified(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    entered = threading.Event()
    monkeypatch.setattr(host.triposr_jobs, 'run_next', lambda *a, **k: entered.set())
    instance = host.TriposrHost(lambda: object(), tmp_path/'artifacts', path)
    instance.start()
    try:
        assert entered.wait(3)
        engine = instance.engines()[0]
        assert engine == {'id': 'triposr-cpu', 'revision': MODEL_SHA, 'operation': 'image-to-3d', 'variant': VARIANT, 'creativeStatus': 'draft'}
        with pytest.raises(Exception):
            instance.admit({'engineId': 'triposr-cpu', 'revision': MODEL_SHA, 'kind': 'model', 'settings': {}})
    finally:
        instance.stop()
    assert instance.engines() == []


def test_server_routes_model_jobs_and_marks_manifest(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    root = tmp_path/'host'; initialize(root)
    monkeypatch.setattr(host.triposr_jobs, 'run_next', lambda *a, **k: None)
    app = application(parser().parse_args(['serve', str(root), '--triposr-config', str(path)]))
    with TestClient(app) as client:
        assert client.get('/manifest.json').json()['vibexStudio']['model3d'] is True
        paired = client.post('/api/gate', json={'code': read_credentials(root).code, 'studio_render': True, 'studio_device': 'd'*32}).json()
        render = {'Authorization': 'Bearer ' + paired['token']}
        for _ in range(30):
            engines = client.get('/api/studio/engines', headers=render).json()['engines']
            if engines: break
            threading.Event().wait(0.1)
        assert [e['id'] for e in engines] == ['triposr-cpu']
        good = {'requestId': 'model-request-00001', 'engineId': 'triposr-cpu', 'revision': MODEL_SHA, 'kind': 'model',
                'prompt': 'Make a draft 3D asset', 'settings': {'operation': 'image-to-3d', 'variant': VARIANT, 'inputId': 'a'*32, 'inputSha256': 'b'*64}}
        assert client.post('/api/studio/jobs', headers=render, json={**good, 'settings': {**good['settings'], 'variant': 'other'}}).status_code == 409
        # A well-formed request still needs an owned input snapshot; the store refuses the unknown input.
        assert client.post('/api/studio/jobs', headers=render, json=good).status_code in (404, 409, 422)
