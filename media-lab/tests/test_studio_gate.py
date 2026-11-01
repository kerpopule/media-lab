from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from media_lab_core import studio_gate, studio_jobs
from media_lab_core.studio_server import create_paired_app

CREDS = studio_gate.Credentials('a'*64, 'b'*64)
OWNER = 'c'*32
BODY = {'code': CREDS.code, 'studio_library': True, 'studio_render': True, 'studio_device': OWNER}


def test_exported_pages_revalidate_after_web_update(tmp_path):
    web = tmp_path/'web'
    web.mkdir()
    for name in ('index.html', 'onboarding.html'):
        (web/name).write_text('<html>old build</html>')
    (web/'entry-hash.js').write_text('/* immutable filename */')
    with TestClient(create_paired_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                    media_root=tmp_path/'media', load_rows=lambda: [], credentials=CREDS,
                    web_root=web)) as client:
        for route in ('/', '/index.html', '/onboarding', '/onboarding.html'):
            before = client.get(route)
            assert before.status_code == 200
            assert before.headers['cache-control'] == 'no-cache'
            unchanged = client.get(route, headers={'If-None-Match': before.headers['etag']})
            assert unchanged.status_code == 304
            assert unchanged.headers['cache-control'] == 'no-cache'
        old = client.get('/onboarding')
        (web/'onboarding.html').write_text('<html>new build with changed bundle</html>')
        updated = client.get('/onboarding', headers={'If-None-Match': old.headers['etag']})
        assert updated.status_code == 200
        assert 'new build' in updated.text
        assert client.get('/api/missing').status_code == 404
        assert client.get('/entry-hash.js').status_code == 200


def test_client_contract_tokens_routes_and_revocation(tmp_path):
    with TestClient(create_paired_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                    media_root=tmp_path/'media', load_rows=lambda: [], credentials=CREDS,
                    allowed_origins=('https://studio.example',))) as client:
        assert client.get('/manifest.json').json()['vibexStudio'] == {'version': 1, 'legacyQueue': False, 'webInterface': False, 'integratedStudio': False, 'editingDrafts': True, 'editingPreview': True, 'editingExport': True, 'editingAddSources': True, 'libraryCollections': False, 'editingLibrarySave': False, 'modelSetup': False, 'speech': False, 'model3d': False, 'music': False, 'video': False, 'image': False}
        paired = client.post('/api/gate', json=BODY)
        assert paired.status_code == 200
        assert paired.headers['cache-control'] == 'no-store'
        assert 'set-cookie' not in paired.headers
        result = paired.json()
        assert (result['scope'], result['renderScope']) == ('library:read', 'jobs:own')
        assert CREDS.authorize(result['renderToken']) == OWNER
        assert CREDS.authorize_library(result['token'])
        assert not CREDS.authorize(result['token'])
        assert not CREDS.authorize_library(result['renderToken'])
        assert client.get('/api/studio/library', headers={'Authorization': 'Bearer '+result['token']}).status_code == 200
        assert client.get('/api/studio/jobs', headers={'Authorization': 'Bearer '+result['renderToken']}).status_code == 200
        renewed = client.post('/api/gate', json={**BODY, 'code': CREDS.code.upper()}).json()
        assert CREDS.authorize(renewed['renderToken']) == OWNER
        rotated = studio_gate.Credentials(CREDS.secret, 'd'*64)
        assert not rotated.authorize(result['renderToken'])
        assert not rotated.authorize_library(result['token'])
        assert not CREDS.authorize(studio_jobs.ticket(CREDS.secret, 'admin', '', OWNER))
        for origin, expected in [('https://studio.example', 'https://studio.example'), ('https://other.example', None)]:
            response = client.options('/api/gate', headers={'Origin': origin,
                'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'})
            assert response.headers.get('access-control-allow-origin') == expected


def test_validation_and_bounded_admission():
    now = [0.0]
    app = FastAPI()
    app.include_router(studio_gate.router(CREDS, library_available=True, clock=lambda: now[0]))
    with TestClient(app) as client:
        for body in ({'code': CREDS.code}, {**BODY, 'studio_device': None}, {**BODY, 'unexpected': True}):
            assert client.post('/api/gate', json=body).status_code == 422
        for _ in range(120):
            assert client.post('/api/gate', json={**BODY, 'code': 'wrong'}).status_code == 403
        response = client.post('/api/gate', json=BODY)
        assert response.status_code == 429 and response.headers['retry-after'] == '60'
        now[0] = 60
        assert client.post('/api/gate', json=BODY).status_code == 200


def test_no_library_and_no_unsafe_configuration(tmp_path):
    app = FastAPI()
    app.include_router(studio_gate.router(CREDS, library_available=False))
    with TestClient(app) as client:
        assert client.post('/api/gate', json=BODY).status_code == 409
        response = client.post('/api/gate', json={**BODY, 'studio_library': False})
        assert response.status_code == 200 and response.json()['scope'] == 'jobs:own'
    assert CREDS.code not in repr(CREDS) and CREDS.secret not in repr(CREDS)
    with pytest.raises(ValueError):
        studio_gate.Credentials('a'*64, 'short')
    with pytest.raises(ValueError):
        studio_gate.Credentials('a'*64, 'A'*64)
    with pytest.raises(ValueError):
        create_paired_app(state_root=tmp_path, artifact_root=tmp_path, credentials=CREDS,
                          media_root=tmp_path, load_rows=lambda: [], allowed_origins=('*',))


def test_browser_can_read_verified_portable_asset_headers(tmp_path):
    from media_lab_core.studio_gate import Credentials
    from .test_glb_contract import glb, fixture
    data = glb(fixture())
    media = tmp_path/'media'
    media.mkdir()
    (media/'model.glb').write_bytes(data)
    app = create_paired_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
        media_root=media, load_rows=lambda: [{'id':'model','url':'/media/model.glb','status':'done'}],
        credentials=Credentials('a'*64, 'b'*64), allowed_origins=('https://studio.example',))
    client = TestClient(app)
    paired = client.post('/api/gate', json={'code':'b'*64,'studio_library':True}).json()
    headers = {'Authorization':'Bearer '+paired['token'], 'Origin':'https://studio.example'}
    response = client.get('/api/studio/library/model/content?portable=1', headers=headers)
    assert response.status_code == 200 and response.content == data
    assert response.headers['X-Studio-Portable'] == 'glb-v1'
    exposed = {name.strip().lower() for name in response.headers['Access-Control-Expose-Headers'].split(',')}
    assert {'x-content-sha256','x-studio-portable'} <= exposed
    assert response.headers['Access-Control-Allow-Origin'] == 'https://studio.example'
    assert 'Access-Control-Allow-Credentials' not in response.headers
    denied = client.get('/api/studio/library/model/content?portable=1',
        headers={**headers,'Origin':'https://other.example'})
    assert 'Access-Control-Allow-Origin' not in denied.headers


def test_windows_desktop_origin_is_allowed_alongside_the_mac_linux_one(tmp_path):
    """Tauri's packaged webview has a different origin per platform: tauri://localhost on
    macOS/Linux, http://tauri.localhost on Windows. A host configured with either must accept
    both, or the desktop app is refused by CORS before any request leaves the machine and can
    only report a generic failure (2026-09-09: Windows could not pair against such a host)."""
    for configured in ('tauri://localhost', 'http://tauri.localhost'):
        with TestClient(create_paired_app(state_root=tmp_path/('state-'+configured[:6]),
                        artifact_root=tmp_path/'artifacts', media_root=tmp_path/'media',
                        load_rows=lambda: [], credentials=CREDS,
                        allowed_origins=(configured,))) as client:
            for origin in ('tauri://localhost', 'http://tauri.localhost'):
                headers={'Origin':origin,'Access-Control-Request-Method':'POST',
                         'Access-Control-Request-Headers':'content-type'}
                preflight=client.options('/api/gate',headers=headers)
                assert preflight.status_code==200, (configured, origin)
                assert preflight.headers['access-control-allow-origin']==origin
            # Widening to the platform twin must not widen anything else.
            for origin in ('null','tauri://evil','https://tauri.localhost','http://tauri.localhost:1'):
                blocked=client.options('/api/gate',headers={'Origin':origin,
                        'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'})
                assert blocked.status_code==400, (configured, origin)


def test_explicit_packaged_desktop_origin_preserves_pairing_auth(tmp_path):
    with TestClient(create_paired_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                    media_root=tmp_path/'media', load_rows=lambda: [], credentials=CREDS,
                    allowed_origins=('tauri://localhost',))) as client:
        headers={'Origin':'tauri://localhost','Access-Control-Request-Method':'POST',
                 'Access-Control-Request-Headers':'content-type'}
        preflight=client.options('/api/gate',headers=headers)
        assert preflight.status_code==200
        assert preflight.headers['access-control-allow-origin']=='tauri://localhost'
        denied=client.post('/api/gate',json={**BODY,'code':'wrong'},headers={'Origin':'tauri://localhost'})
        assert denied.status_code==403
        paired=client.post('/api/gate',json=BODY,headers={'Origin':'tauri://localhost'})
        assert paired.status_code==200
        assert paired.headers['access-control-allow-origin']=='tauri://localhost'
        for origin in ('null','tauri://evil','tauri://localhost:1','https://unrelated.example'):
            blocked=client.options('/api/gate',headers={**headers,'Origin':origin})
            assert blocked.status_code==400
            assert 'access-control-allow-origin' not in blocked.headers


@pytest.mark.parametrize('origin',['tauri://evil','tauri://localhost/','tauri://localhost:1','null'])
def test_custom_origin_variants_refused(tmp_path,origin):
    with pytest.raises(ValueError):
        create_paired_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',
                         media_root=tmp_path/'media',load_rows=lambda:[],credentials=CREDS,
                         allowed_origins=(origin,))


def test_desktop_origin_not_enabled_by_default(tmp_path):
    with TestClient(create_paired_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',
                    media_root=tmp_path/'media',load_rows=lambda:[],credentials=CREDS)) as client:
        response=client.options('/api/gate',headers={'Origin':'tauri://localhost','Access-Control-Request-Method':'POST'})
        assert response.status_code==400
        assert 'access-control-allow-origin' not in response.headers


@pytest.mark.parametrize('enabled', [False, True])
def test_library_save_capability_matches_handler(tmp_path, enabled):
    with TestClient(create_paired_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                    media_root=tmp_path/'media', load_rows=lambda: [], credentials=CREDS,
                    save_export=(lambda path, title: {}) if enabled else None)) as client:
        assert client.get('/manifest.json').json()['vibexStudio']['editingLibrarySave'] is enabled


def test_direct_project_urls_use_the_exported_template_without_masking_missing_routes(tmp_path):
    web=tmp_path/'web'
    (web/'project').mkdir(parents=True)
    (web/'index.html').write_text('<html>home</html>')
    (web/'project/[id].html').write_text('<html>project route</html>')
    with TestClient(create_paired_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                    media_root=tmp_path/'media', load_rows=lambda: [], credentials=CREDS,
                    web_root=web)) as client:
        for route in ('/project/game-123', '/project/game_123/', '/project/game-123?review=1'):
            response=client.get(route)
            assert response.status_code==200 and response.text=='<html>project route</html>'
            assert response.headers['cache-control']=='no-cache'
            cached=client.get(route,headers={'If-None-Match':response.headers['etag']})
            assert cached.status_code==304 and cached.headers['cache-control']=='no-cache'
        for route in ('/project/game/missing','/project/game.js','/project/'+('a'*129),'/api/project/game','/unknown/game'):
            assert client.get(route).status_code==404


def test_unknown_pages_get_the_branded_not_found_screen_with_a_404(tmp_path):
    web=tmp_path/'web'
    (web/'_expo').mkdir(parents=True)
    (web/'index.html').write_text('<html>home</html>')
    (web/'settings.html').write_text('<html>settings</html>')
    (web/'+not-found.html').write_text('<html>branded not found</html>')
    with TestClient(create_paired_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                    media_root=tmp_path/'media', load_rows=lambda: [], credentials=CREDS,
                    web_root=web)) as client:
        for route in ('/no-such-page', '/deep/unknown/route/', '/project/game/missing'):
            response=client.get(route)
            assert response.status_code==404
            assert response.text=='<html>branded not found</html>'
            assert response.headers['cache-control']=='no-cache'
        assert client.head('/no-such-page').status_code==404
        # Real pages, APIs and missing assets keep their behaviour.
        assert client.get('/settings').text=='<html>settings</html>'
        assert client.get('/').text=='<html>home</html>'
        for route in ('/api/missing', '/api', '/_expo/missing.js', '/missing.png'):
            response=client.get(route)
            assert response.status_code==404 and 'branded' not in response.text
        assert client.get('/manifest.json').status_code==200
