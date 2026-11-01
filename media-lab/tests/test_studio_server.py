import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_lab_core import studio_jobs, studio_server
from media_lab_core.job_store import JobStore

OWNER = 'a' * 32
OTHER = 'b' * 32
BODY = {'requestId': 'standalone-test-0001', 'engineId': 'birefnet-cpu',
        'revision': 'unqualified', 'kind': 'image', 'prompt': 'A cutout', 'settings': {}}


def test_no_legacy_import_or_import_time_storage(tmp_path):
    source = Path(studio_server.__file__).parents[1]
    code = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in ('app', 'runner') or name.startswith(('runner.', 'models.', 'shared.')):
        raise AssertionError('Legacy import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from pathlib import Path
from media_lab_core.studio_server import create_app
import sys
root = Path(sys.argv[1])
create_app(state_root=root/'state', artifact_root=root/'artifacts', authorize=lambda _: None)
assert not (root/'state').exists()
assert not (root/'artifacts').exists()
'''
    subprocess.run([sys.executable, '-c', code, str(tmp_path)], cwd=source, check=True)


def test_offline_restart_preserves_owned_history_and_rejects_new_work(tmp_path):
    codes = lambda role: 'TEST-CODE'
    authorize = lambda raw: studio_jobs.identity(raw, 'test-secret', codes)
    headers = {'Authorization': 'Bearer ' + studio_jobs.ticket('test-secret', 'user', codes('user'), OWNER)}
    other = {'Authorization': 'Bearer ' + studio_jobs.ticket('test-secret', 'user', codes('user'), OTHER)}
    state, artifacts = tmp_path/'state', tmp_path/'artifacts'
    store = JobStore(state/'studio-jobs.sqlite')
    jid = store.enqueue_once(OWNER, BODY['requestId'], BODY['kind'],
                             {key: value for key, value in BODY.items() if key != 'requestId'})
    for _ in range(2):
        app = studio_server.create_app(state_root=state, artifact_root=artifacts, authorize=authorize)
        with TestClient(app) as client:
            assert client.get('/api/studio/jobs').status_code == 401
            assert client.get('/api/studio/engines', headers=headers).json()['engines'] == []
            assert client.get('/api/studio/jobs', headers=other).json()['jobs'] == []
            assert client.get(f'/api/studio/jobs/{jid}', headers=other).status_code == 404
            retry = client.post('/api/studio/jobs', headers=headers, json=BODY)
            assert retry.status_code == 200 and retry.json()['id'] == jid
            assert client.post('/api/studio/jobs', headers=headers,
                               json={**BODY, 'requestId': 'standalone-test-0002'}).status_code == 503
            assert client.post('/api/maestro', headers=headers, json={}).status_code == 404
            assert client.post('/api/access', json={}).status_code == 404
        assert store.get(jid)['status'] == 'queued'
    assert not artifacts.exists()


def test_host_lifecycle_is_paired_even_when_serving_raises(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(studio_server.BackgroundHost, 'start', lambda self: events.append('start'))
    monkeypatch.setattr(studio_server.BackgroundHost, 'stop', lambda self: events.append('stop'))
    app = studio_server.create_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                                  authorize=lambda _: None)
    with pytest.raises(RuntimeError, match='client failure'):
        with TestClient(app):
            raise RuntimeError('client failure')
    assert events == ['start', 'stop']


def test_explicit_paths_and_verifier_required(tmp_path):
    with pytest.raises(ValueError, match='absolute'):
        studio_server.create_app(state_root=Path('relative'), artifact_root=tmp_path, authorize=lambda _: None)
    with pytest.raises(TypeError, match='verifier'):
        studio_server.create_app(state_root=tmp_path, artifact_root=tmp_path, authorize=None)


def test_library_browse_snapshot_and_restart_without_legacy(tmp_path):
    import hashlib
    from PIL import Image
    media = tmp_path/'media'
    media.mkdir()
    source = media/'badge.png'
    Image.new('RGBA', (4, 3), 'red').save(source)
    original = source.read_bytes()
    rows = [{'id': 'badge', 'url': '/media/badge.png', 'status': 'completed'},
            {'id': 'escape', 'url': '/media/../../outside.png'},
            {'id': 'pending', 'url': '/media/badge.png', 'status': 'running'}]
    library = studio_server.Library(media, lambda: rows, lambda token: token == 'library')
    state = tmp_path/'state'
    args = dict(state_root=state, artifact_root=tmp_path/'artifacts',
                authorize=lambda token: OWNER if token == 'device' else None, library=library)
    headers = {'Authorization': 'Bearer device', 'X-Library-Authorization': 'Bearer library'}
    with TestClient(studio_server.create_app(**args)) as client:
        url = '/api/studio/library'
        assert client.get(url, headers={'Authorization': 'Bearer device'}).status_code == 401
        assets = client.get(url, headers={'Authorization': 'Bearer library'}).json()['assets']
        assert [asset['id'] for asset in assets] == ['badge']
        assert client.get(url+'/badge/content', headers={'Authorization': 'Bearer library'}).content == original
        route = '/api/studio/inputs/library'
        for denied in ({}, {'Authorization': 'Bearer device'}, {'X-Library-Authorization': 'Bearer library'}):
            assert client.post(route, json={'assetId': 'badge'}, headers=denied).status_code == 401
        response = client.post(route, json={'assetId': 'badge'}, headers=headers)
        assert response.status_code == 200
        accepted = response.json()
        assert accepted['sha256'] == hashlib.sha256(original).hexdigest()
        assert (accepted['width'], accepted['height']) == (4, 3)
        assert client.post(route, json={'assetId': 'escape'}, headers=headers).status_code == 404
    source.unlink()
    with TestClient(studio_server.create_app(**args)) as client:
        assert client.post(route, json={'assetId': 'badge'}, headers=headers).status_code == 404
        store = JobStore(state/'studio-jobs.sqlite')
        assert store.input_metadata(OWNER, accepted['id'])['sha256'] == accepted['sha256']
        assert store.input_metadata(OTHER, accepted['id']) is None
        jid = store.enqueue_once(OWNER, 'library-snapshot-0001', 'image', {})
        assert store.read_job_input(jid, accepted['id']) == original
