from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import hashlib
import pytest

from media_lab_core import studio_jobs as api, studio_library
from media_lab_core.job_store import JobStore
from .test_cut_api import media_app

OWNER = 'a' * 32
OTHER = 'b' * 32
NOW = 1788554000
BODY = {'requestId': 'request-retry-0001', 'engineId': 'test-image', 'revision': 'test-revision',
        'kind': 'image', 'prompt': 'A badge', 'settings': {'seed': 3}}


def test_cancellation_is_owned_idempotent_and_terminal_safe(tmp_path):
    store = JobStore(tmp_path/'jobs.sqlite')
    app = FastAPI()
    app.include_router(api.router(lambda: store, lambda value: {'one': OWNER, 'two': OTHER}.get(value),
                                  lambda: [], lambda payload: None))
    client = TestClient(app)
    headers = {'Authorization': 'Bearer one'}
    jid = client.post('/api/studio/jobs', json=BODY, headers=headers).json()['id']
    route = f'/api/studio/jobs/{jid}/cancel'
    assert api.is_jobs_path(route)
    assert client.post(route).status_code == 401
    assert client.post(route, headers={'Authorization': 'Bearer two'}).status_code == 404
    assert store.get(jid)['status'] == 'queued'
    assert client.post(route, headers=headers).json()['status'] == 'cancelled'
    assert client.post(route, headers=headers).json()['status'] == 'cancelled'
    next_id = client.post('/api/studio/jobs', json={**BODY, 'requestId': 'request-running-0002'}, headers=headers).json()['id']
    store.claim_next('worker')
    assert client.post(f'/api/studio/jobs/{next_id}/cancel', headers=headers).json()['status'] == 'cancel_requested'
    store.transition(next_id, 'worker', 'cancelled')
    assert client.post(f'/api/studio/jobs/{next_id}/cancel', headers=headers).json()['status'] == 'cancelled'


def test_render_identity_is_scoped_revocable_and_stable_on_renewal():
    token = api.ticket('secret', 'user', 'CODE', OWNER, NOW)
    codes = lambda role: 'CODE'
    assert api.identity(token, 'secret', codes, NOW) == OWNER
    renewed = api.ticket('secret', 'user', 'CODE', OWNER, NOW + 100)
    assert token != renewed and api.identity(renewed, 'secret', codes, NOW + 100) == OWNER
    assert api.identity(token, 'secret', codes, NOW + api.TOKEN_AGE + 1) is None
    assert api.identity(token, 'secret', codes, NOW - 301) is None
    assert api.identity(token, 'rotated', codes, NOW) is None
    assert api.identity(token, 'secret', lambda role: 'NEW', NOW) is None
    assert api.identity(token.replace(OWNER, OTHER), 'secret', codes, NOW) is None
    assert api.identity(token.replace('.user.', '.admin.'), 'secret', codes, NOW) is None
    assert not studio_library.valid_ticket(token, 'secret', codes, NOW)
    assert api.identity(studio_library.ticket('secret', 'user', 'CODE', NOW), 'secret', codes, NOW) is None


def test_exact_admission_retry_recovery_and_owner_isolation(tmp_path):
    store = JobStore(tmp_path / 'jobs.sqlite')
    seen = []
    online = True
    def admit(payload):
        if not online:
            raise HTTPException(503, 'Engine offline')
        if (payload['engineId'], payload['revision']) != ('test-image', 'test-revision'):
            raise HTTPException(409, 'Exact engine unavailable')
        seen.append(payload.copy())
    app = FastAPI()
    app.include_router(api.router(lambda: store, lambda value: {'one': OWNER, 'two': OTHER}.get(value), lambda: [], admit))
    client = TestClient(app)
    headers = {'Authorization': 'Bearer one'}
    assert client.post('/api/studio/jobs', json=BODY).status_code == 401
    first = client.post('/api/studio/jobs', json=BODY, headers=headers)
    assert first.status_code == 200
    jid = first.json()['id']
    assert seen == [{k: v for k, v in BODY.items() if k != 'requestId'}]
    online = False
    assert client.post('/api/studio/jobs', json=BODY, headers=headers).json()['id'] == jid
    assert len(seen) == 1
    assert client.post('/api/studio/jobs', json={**BODY, 'prompt': 'Changed'}, headers=headers).status_code == 409
    assert client.post('/api/studio/jobs', json={**BODY, 'requestId': 'new-request-00002'}, headers=headers).status_code == 503
    assert client.get('/api/studio/jobs/' + jid, headers={'Authorization': 'Bearer two'}).status_code == 404
    assert client.get('/api/studio/jobs/' + jid, cookies={'mlab_access': 'anything'}).status_code == 401
    job = store.claim_next('test-worker')
    store.transition(job['id'], 'test-worker', 'failed', error='private diagnostic /secret/path')
    response = client.get('/api/studio/jobs/' + jid, headers=headers)
    assert response.json()['status'] == 'failed'
    assert 'private' not in response.text and 'payload' not in response.json()
    assert store.claim_next('test-worker') is None


@pytest.mark.parametrize('override', [
    {'engineId': '../arbitrary'}, {'revision': ''}, {'prompt': '   '},
    {'settings': {'text': 'x' * 33000}}, {'owner': OTHER}, {'kind': 'shell'},
])
def test_invalid_generation_never_reaches_admission(tmp_path, override):
    def unexpected(payload):
        raise AssertionError('invalid requests must not reach an engine')
    store = JobStore(tmp_path / 'jobs.sqlite')
    app = FastAPI()
    app.include_router(api.router(lambda: store, lambda value: OWNER, lambda: [], unexpected))
    response = TestClient(app).post('/api/studio/jobs', json={**BODY, **override}, headers={'Authorization': 'Bearer test'})
    assert response.status_code in (413, 422)
    assert store.claim_next('worker') is None


def test_real_gate_scopes_cors_and_unqualified_refusal(media_app, monkeypatch):
    client = TestClient(media_app.app, base_url='https://studio-test.invalid')
    # Trusted-host/cookie authentication cannot substitute for a render ticket.
    paired = client.post('/api/gate', json={'code': media_app.ACCESS_CODE, 'studio_library': True})
    library = paired.json()['token']
    assert client.get('/api/studio/engines', headers={'Authorization': f'Bearer {library}'}).status_code == 401
    paired = client.post('/api/gate', json={'code': media_app.ACCESS_CODE, 'studio_library': True,
                                          'studio_render': True, 'studio_device': OWNER})
    data = paired.json()
    assert data['scope'] == 'library:read' and data['renderScope'] == 'jobs:own'
    assert media_app.SESSION_COOKIE not in paired.cookies
    headers = {'Authorization': 'Bearer ' + data['renderToken']}
    assert client.get('/api/studio/engines', headers=headers).json() == {'version': 1, 'engines': []}
    assert client.get('/api/studio/library', headers=headers).status_code == 401
    assert client.get('/api/queue', headers=headers).status_code == 401
    assert client.post('/api/generate', headers=headers, json={'prompt': 'badge'}).status_code == 401
    response = client.post('/api/studio/jobs', headers=headers, json=BODY)
    assert response.status_code == 503
    assert media_app._studio_job_store().claim_next('worker') is None
    preflight = client.options('/api/studio/jobs', headers={'Origin': 'https://app-test.invalid',
                                                         'Access-Control-Request-Method': 'POST'})
    assert preflight.status_code == 204
    assert preflight.headers['Access-Control-Allow-Headers'] == 'Authorization, Content-Type'
    assert {value.strip() for value in preflight.headers['Access-Control-Expose-Headers'].split(',')} == {'X-Content-SHA256', 'X-Studio-Portable'}
    assert 'Access-Control-Allow-Credentials' not in preflight.headers
    cancel_url = '/api/studio/jobs/' + 'f'*32 + '/cancel'
    cancel_preflight = client.options(cancel_url, headers={'Origin': 'https://app-test.invalid',
                                                         'Access-Control-Request-Method': 'POST'})
    assert cancel_preflight.status_code == 204
    assert 'Authorization' in cancel_preflight.headers['Access-Control-Allow-Headers']
    assert client.post(cancel_url, headers=headers).status_code == 404
    assert client.post(cancel_url).status_code == 401
    assert response.headers['Cache-Control'] == 'private, no-store'
    renewed = client.post('/api/gate', json={'code': media_app.ACCESS_CODE, 'studio_render': True,
                                           'studio_device': OWNER}).json()
    assert renewed['scope'] == 'jobs:own'
    assert client.post('/api/gate', json={'code': media_app.ACCESS_CODE, 'studio_render': True}).status_code == 422


def test_result_bytes_require_owner_publication_and_integrity(tmp_path):
    store = JobStore(tmp_path / 'jobs.sqlite')
    jid = store.enqueue_once(OWNER, 'result-request-0001', 'image', {})
    root = tmp_path / 'artifacts'
    folder = root / jid
    folder.mkdir(parents=True)
    # Real image bytes, generated without a model or a production artifact.
    from PIL import Image
    output = folder / 'badge.png'
    Image.new('RGBA', (8, 8), (128, 40, 255, 128)).save(output)
    original = output.read_bytes()
    artifact = {'path': f'{jid}/badge.png', 'sha256': hashlib.sha256(original).hexdigest(), 'bytes': len(original)}
    app = FastAPI()
    app.include_router(api.router(lambda: store, lambda value: {'one': OWNER, 'two': OTHER}.get(value),
                                  lambda: [], lambda _: None, root))
    client = TestClient(app)
    url = f'/api/studio/jobs/{jid}/content'
    headers = {'Authorization': 'Bearer one'}
    assert client.get(url, headers=headers).status_code == 404
    store.claim_next('test-worker')
    store.transition(jid, 'test-worker', 'succeeded', result={'artifact': artifact})
    assert client.get(url).status_code == 401
    assert client.get(url, headers={'Authorization': 'Bearer two'}).status_code == 404
    response = client.get(url, headers=headers)
    assert response.content == original
    assert response.headers['X-Content-SHA256'] == artifact['sha256']
    preview_url = f'/api/studio/jobs/{jid}/preview'
    assert api.is_jobs_path(preview_url)
    assert client.get(preview_url).status_code == 401
    assert client.get(preview_url, headers={'Authorization': 'Bearer two'}).status_code == 404
    preview = client.get(preview_url, headers=headers)
    assert preview.status_code == 200 and preview.headers['content-type'] == 'image/png'
    import io
    assert Image.open(io.BytesIO(preview.content)).getpixel((0, 0)) == (128, 40, 255, 128)
    output.write_bytes(b'x' * len(original))
    assert client.get(preview_url, headers=headers).status_code == 409
    assert client.get(url, headers=headers).status_code == 409
    output.unlink()
    outside = tmp_path / 'private.png'
    outside.write_bytes(original)
    output.symlink_to(outside)
    assert client.get(url, headers=headers).status_code == 409


def test_adapter_cannot_silently_substitute_settings(tmp_path):
    store = JobStore(tmp_path / 'jobs.sqlite')
    def mutate(payload):
        payload['engineId'] = 'legacy-fallback'
    app = FastAPI()
    app.include_router(api.router(lambda: store, lambda _: OWNER, lambda: [], mutate))
    response = TestClient(app).post('/api/studio/jobs', json=BODY, headers={'Authorization': 'Bearer one'})
    assert response.status_code == 500
    assert store.claim_next('worker') is None


def test_owned_history_paginates_ties_and_survives_app_restart(tmp_path):
    path = tmp_path/'jobs.sqlite'
    store = JobStore(path)
    ids = [store.enqueue_once(OWNER, f'history-request-{i:04d}', 'image', {'private': 'secret/path'}) for i in range(5)]
    other = store.enqueue_once(OTHER, 'other-history-0001', 'image', {})
    legacy = store.enqueue('image', {'legacy': True}, job_id='c'*32)
    with store.connect() as db:
        db.execute('UPDATE jobs SET created_at=100')
    # Reopening the durable store requires no client-side job list.
    app = FastAPI()
    app.include_router(api.router(lambda: JobStore(path), lambda value: {'one': OWNER, 'two': OTHER}.get(value),
                                  lambda: [], lambda _: None))
    client = TestClient(app)
    headers = {'Authorization': 'Bearer one'}
    assert client.get('/api/studio/jobs').status_code == 401
    found, cursor = [], None
    while True:
        response = client.get('/api/studio/jobs', headers=headers,
                              params={'limit': 2, **({'before': cursor} if cursor else {})})
        assert response.status_code == 200
        data = response.json()
        assert 'secret' not in response.text and 'payload' not in response.text
        found.extend(job['id'] for job in data['jobs'])
        cursor = data['nextCursor']
        if cursor is None:
            break
    assert found == sorted(ids, reverse=True)
    assert other not in found and legacy not in found
    for unavailable in (other, legacy, 'f'*32):
        assert client.get('/api/studio/jobs', headers=headers, params={'before': unavailable}).status_code == 404
    assert client.get('/api/studio/jobs', headers=headers, params={'limit': 101}).status_code == 422
    assert client.get('/api/studio/jobs', headers=headers, params={'before': '../private'}).status_code == 422
    with store.connect() as db:
        db.execute('UPDATE jobs SET created_at=200 WHERE id=?', (other,))
    assert [row['id'] for row in store.list_owned(OWNER, limit=100)] == found


def test_public_job_title_is_bounded_plain_prompt_only():
    job = {'id':'a'*32,'kind':'video','status':'succeeded','created_at':1,'updated_at':2,
           'payload':{'prompt':'  '+ '🎬'*241 +'  ','settings':{'private':'secret'}},
           'result':{'artifact':{'path':'private/output.mp4'}}}
    result = api.public_job(job)
    assert result['title'] == '🎬'*240
    assert 'payload' not in result and 'result' not in result
    job['payload']['prompt'] = {'unexpected':'value'}
    assert api.public_job(job)['title'] == ''


def test_cancel_unknown_request_blocks_late_submit_after_restart(tmp_path):
    path = tmp_path / 'jobs.sqlite'
    store = JobStore(path)
    def offline(payload):
        raise AssertionError('Cancellation or an accepted retry must not admit work')
    app = FastAPI()
    app.include_router(api.router(lambda: store, lambda value: {'one': OWNER, 'two': OTHER}.get(value),
                                  lambda: [], offline))
    client = TestClient(app)
    route = '/api/studio/requests/cancel'
    headers = {'Authorization': 'Bearer one'}
    assert api.is_jobs_path(route)
    assert client.post(route, json=BODY).status_code == 401
    cancelled = client.post(route, json=BODY, headers=headers).json()
    assert cancelled['status'] == 'cancelled'
    store = JobStore(path)
    assert client.post(route, json=BODY, headers=headers).json() == cancelled
    assert client.post('/api/studio/jobs', json=BODY, headers=headers).json() == cancelled
    assert store.claim_next('worker') is None
    assert client.post(route, json={**BODY, 'prompt': 'Changed'}, headers=headers).status_code == 409
    other = client.post(route, json=BODY, headers={'Authorization': 'Bearer two'}).json()
    assert other['id'] != cancelled['id']
    assert store.get_owned(OWNER, other['id']) is None
    assert client.post(route, json={**BODY, 'settings': {'large': 'x'*33000}}, headers=headers).status_code == 413


@pytest.mark.parametrize('accepted_status', ['queued', 'running', 'succeeded'])
def test_request_cancellation_preserves_terminal_work(tmp_path, accepted_status):
    store = JobStore(tmp_path/'jobs.sqlite')
    payload = {key: value for key, value in BODY.items() if key != 'requestId'}
    jid = store.enqueue_once(OWNER, BODY['requestId'], 'image', payload)
    if accepted_status != 'queued':
        store.claim_next('worker')
    if accepted_status == 'succeeded':
        store.transition(jid, 'worker', 'succeeded', result={'kept': True})
    assert store.enqueue_once(OWNER, BODY['requestId'], 'image', payload, cancel=True) == jid
    expected = {'queued': 'cancelled', 'running': 'cancel_requested', 'succeeded': 'succeeded'}[accepted_status]
    assert store.get(jid)['status'] == expected
    if accepted_status == 'succeeded':
        assert store.get(jid)['result'] == {'kept': True}


def test_submit_cancel_race_never_leaves_queued_work(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store = JobStore(tmp_path/'jobs.sqlite')
    payload = {key: value for key, value in BODY.items() if key != 'requestId'}
    for index in range(12):
        barrier = Barrier(2)
        request_id = f'concurrent-request-{index:04d}'
        def operation(cancel):
            barrier.wait()
            return store.enqueue_once(OWNER, request_id, 'image', payload, cancel=cancel)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(operation, False)
            b = pool.submit(operation, True)
            assert a.result() == b.result()
            assert store.get(a.result())['status'] == 'cancelled'
    assert store.claim_next('worker') is None


def test_save_cutout_to_library_preserves_bytes_and_retries(tmp_path):
    from PIL import Image
    from media_lab_core.studio_cli import initialize, import_media, read_library_catalog
    store = JobStore(tmp_path / 'jobs.sqlite')
    jid = store.enqueue_once(OWNER, 'cutout-save-request', 'image', {})
    artifacts = tmp_path / 'artifacts'
    output = artifacts / jid / 'output.png'
    output.parent.mkdir(parents=True)
    Image.new('RGBA', (16, 16), (0, 80, 150, 128)).save(output)
    original = output.read_bytes()
    host = tmp_path / 'host'
    initialize(host)
    app = FastAPI()
    app.include_router(api.router(lambda: store, lambda value: {'one': OWNER, 'two': OTHER}.get(value),
        lambda: [], lambda _: None, artifacts,
        save_image=lambda path,title: import_media(host,path,title,cutout=True)))
    client = TestClient(app)
    url = f'/api/studio/jobs/{jid}/library'
    assert api.is_jobs_path(url)
    headers = {'Authorization':'Bearer one'}
    assert client.post(url,headers=headers).status_code == 404
    store.claim_next('worker')
    store.transition(jid,'worker','succeeded',result={'artifact':{'path':f'{jid}/output.png','bytes':len(original),'sha256':hashlib.sha256(original).hexdigest()}})
    assert client.post(url).status_code == 401
    assert client.post(url,headers={'Authorization':'Bearer two'}).status_code == 404
    first = client.post(url,headers=headers)
    assert first.status_code == 200, first.text
    again = client.post(url,headers=headers)
    assert again.status_code == 200
    rows = read_library_catalog(host)
    assert len(rows) == 1
    paths = list((host/'media/Images/Cutouts').glob('*.png'))
    assert len(paths) == 1 and paths[0].read_bytes() == original
    output.write_bytes(b'x'*len(original))
    assert client.post(url,headers=headers).status_code == 409
    assert paths[0].read_bytes() == original
