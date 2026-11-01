import io
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from media_lab_core import studio_inputs, studio_jobs, background_jobs
from media_lab_core import job_store
from media_lab_core.job_store import JobStore
from .test_cut_api import media_app

OWNER = 'a'*32
OTHER = 'b'*32


def png(color='red'):
    output = io.BytesIO()
    Image.new('RGBA', (4, 3), color).save(output, format='PNG')
    return output.getvalue()


def test_dual_permission_snapshot_ownership_and_original_independence(tmp_path):
    store = JobStore(tmp_path/'jobs.sqlite')
    data = png()
    original = data
    calls = []
    def read(asset_id):
        calls.append(asset_id)
        return data
    auth = lambda token: {'one': OWNER, 'two': OTHER}.get(token)
    app = FastAPI()
    app.include_router(studio_inputs.router(lambda: store, auth, lambda token: token == 'library', read))
    app.include_router(studio_jobs.router(lambda: store, auth, lambda: [], lambda payload: None))
    client = TestClient(app)
    url = '/api/studio/inputs/library'
    headers = {'Authorization': 'Bearer one', 'X-Library-Authorization': 'Bearer library'}
    for denied in ({}, {'Authorization': 'Bearer one'}, {'X-Library-Authorization': 'Bearer library'}):
        assert client.post(url, json={'assetId': 'image-one'}, headers=denied).status_code == 401
    assert calls == []
    first = client.post(url, json={'assetId': 'image-one'}, headers=headers).json()
    retry = client.post(url, json={'assetId': 'image-one'}, headers=headers).json()
    assert first == retry
    assert first['width'] == 4 and first['height'] == 3
    assert store.input_metadata(OTHER, first['id']) is None
    body = {'requestId': 'snapshot-request-0001', 'engineId': 'test', 'revision': 'test', 'kind': 'image',
            'prompt': 'Remove background', 'settings': {'inputId': first['id'], 'inputSha256': first['sha256']}}
    assert client.post('/api/studio/jobs', json=body, headers={'Authorization': 'Bearer two'}).status_code == 404
    jid = client.post('/api/studio/jobs', json=body, headers=headers).json()['id']
    data = png('blue')
    assert store.read_job_input(jid, first['id']) == original
    other_job = store.enqueue_once(OTHER, 'other-request-00001', 'image', {})
    with pytest.raises(ValueError, match='does not own'):
        store.read_job_input(other_job, first['id'])
    changed = client.post(url, json={'assetId': 'image-one'}, headers=headers).json()
    assert changed['id'] != first['id']
    assert JobStore(store.path).read_job_input(jid, first['id']) == original


def test_snapshot_decode_refuses_invalid_input():
    with pytest.raises(ValueError):
        studio_inputs.validate_image(b'not an image')
    with pytest.raises(ValueError):
        studio_inputs.validate_image(b'x'*(20*1024**2+1))


def test_quota_refusal_is_atomic_and_duplicates_still_recover(tmp_path, monkeypatch):
    store = JobStore(tmp_path/'quota.sqlite')
    data = png()
    first = store.put_input(OWNER, data, width=4, height=3)
    monkeypatch.setattr(job_store, 'INPUT_OWNER_BYTES', len(data))
    assert store.put_input(OWNER, data, width=4, height=3) == first
    with pytest.raises(ValueError, match='storage limit'):
        store.put_input(OWNER, png('blue'), width=4, height=3)
    monkeypatch.setattr(job_store, 'INPUT_TOTAL_BYTES', len(data))
    with pytest.raises(ValueError, match='storage limit'):
        store.put_input(OTHER, data, width=4, height=3)
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM studio_inputs').fetchone()[0] == 1


def test_real_app_input_scopes_and_cors(media_app):
    client = TestClient(media_app.app, base_url='https://studio-test.invalid')
    paired = client.post('/api/gate', json={'code': media_app.ACCESS_CODE, 'studio_library': True,
                                          'studio_render': True, 'studio_device': OWNER}).json()
    headers = {'Authorization': 'Bearer '+paired['renderToken'],
               'X-Library-Authorization': 'Bearer '+paired['token']}
    url = '/api/studio/inputs/library'
    assert client.options(url, headers={'Origin': 'https://app-test.invalid',
                                       'Access-Control-Request-Method': 'POST'}).headers['Access-Control-Allow-Headers'] == \
        'Authorization, Content-Type, X-Library-Authorization'
    assert client.post(url, json={'assetId': 'missing'}, headers=headers).status_code == 404
    assert client.post(url, json={'assetId': 'missing'}).status_code == 401
    source = media_app.MEDIA/'input-snapshot-test.png'
    source.write_bytes(png())
    gallery = media_app.ROOT/'gallery.json'
    previous = gallery.read_bytes() if gallery.exists() else None
    try:
        gallery.write_text(json.dumps([{'id': 'snapshot-image', 'url': '/media/input-snapshot-test.png', 'status': 'done'}]))
        response = client.post(url, json={'assetId': 'snapshot-image'}, headers=headers)
        assert response.status_code == 200, response.text
        accepted = response.json()
        payload = {'engineId': background_jobs.ENGINE, 'revision': json.loads(background_jobs.MANIFEST.read_text())['revision'],
                   'kind': 'image', 'prompt': 'Remove background',
                   'settings': {'operation': 'remove-background', 'inputId': accepted['id'], 'inputSha256': accepted['sha256']}}
        # The production route must still refuse an unregistered model.
        assert client.post('/api/studio/jobs', headers=headers,
                           json={'requestId': 'unregistered-test-001', **payload}).status_code == 503
        store = media_app._studio_job_store()
        jid = store.enqueue_once(OWNER, 'worker-snapshot-00001', 'image', payload)
        source.write_bytes(png('blue'))
        def execute(**kwargs):
            assert kwargs['data'] == png()
            return {'path': jid+'/output.png'}
        result = background_jobs.run_next(store, root=media_app.ROOT/'test-output', runtime=source,
                                           package=source, cache=media_app.ROOT/'cache', execute=execute)
        assert result['status'] == 'succeeded'
    finally:
        source.unlink()
        if previous is None:
            gallery.unlink()
        else:
            gallery.write_bytes(previous)
