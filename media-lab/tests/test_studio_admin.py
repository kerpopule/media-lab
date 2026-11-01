"""Administrator enrollment, sessions and model-setup wiring on the independent host."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from media_lab_core import studio_admin as admin
from media_lab_core.background_setup import Setup, write_receipt
from media_lab_core.studio_cli import initialize, read_credentials, parser, application, main

HEADER = {'X-Setup-Request': '1'}


@pytest.fixture
def host(tmp_path, monkeypatch):
    # Keep enrollment fast in tests; production uses the full iteration count.
    monkeypatch.setattr(admin, 'ITERATIONS', 1000)
    root = tmp_path/'host'
    initialize(root)
    code = admin.enroll(root)
    return SimpleNamespace(root=root, code=code)


def serve_args(root, *extra):
    return parser().parse_args(['serve', str(root), '--model-setup', *extra])


def test_enrollment_is_private_hashed_and_rotatable(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, 'ITERATIONS', 1000)
    root = tmp_path/'host'
    with pytest.raises(ValueError, match='Initialize'):
        admin.enroll(root)
    initialize(root)
    code = admin.enroll(root)
    path = root/admin.ADMIN_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_text()
    assert code not in raw and len(code) == 64
    first = admin.read_admin(root)
    assert first['generation'] == 1
    with pytest.raises(FileExistsError):
        admin.enroll(root)
    rotated = admin.enroll(root, rotate=True)
    assert rotated != code and admin.read_admin(root)['generation'] == 2
    assert admin.revoke(root) == 3
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match='private'):
        admin.read_admin(root)


def test_cli_prints_code_once_and_never_the_hash(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(admin, 'ITERATIONS', 1000)
    root = tmp_path/'host'
    initialize(root)
    assert main(['admin-init', str(root)]) == 0
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    stored = json.loads((root/admin.ADMIN_FILE).read_text())
    assert len(printed) == 64 and printed != stored['code_hash'] and stored['code_hash'] not in ''.join(printed)
    assert main(['admin-init', str(root)]) == 2  # refuses to overwrite silently
    assert 'could not admin-init' in capsys.readouterr().out
    assert main(['admin-revoke', str(root)]) == 0
    assert admin.read_admin(root)['generation'] == 2


def test_serve_with_model_setup_requires_enrollment(tmp_path):
    root = tmp_path/'host'
    initialize(root)
    with pytest.raises(OSError):
        application(serve_args(root))


def test_setup_routes_refuse_every_device_token_and_require_a_session(host):
    app = application(serve_args(host.root))
    with TestClient(app) as client:
        assert client.get('/manifest.json').json()['vibexStudio']['modelSetup'] is True
        assert client.get('/setup/background').status_code == 200
        assert 'background-setup.js' in client.get('/setup/background').text
        assert client.get('/static/background-setup.js').headers['content-type'].startswith('text/javascript')
        plan = '/api/setup/background/plan'
        assert client.get(plan).status_code == 403
        assert client.get('/api/setup/session').json() == {'signedIn': False}
        pairing = read_credentials(host.root).code
        paired = client.post('/api/gate', json={'code': pairing, 'studio_library': True, 'studio_render': True,
                                                'studio_edit': True, 'studio_device': 'a'*32}).json()
        for token in (paired['token'], paired['renderToken'], paired['editToken']):
            assert client.get(plan, headers={'Authorization': 'Bearer '+token}).status_code == 403
            client.cookies.set(admin.COOKIE, token, path=admin.COOKIE_PATH)
            assert client.get(plan).status_code == 403
            client.cookies.clear()
        # The pairing code is not the administrator code.
        assert client.post('/api/setup/session', json={'code': pairing}, headers=HEADER).status_code == 403
        assert client.post('/api/setup/background/install', json={'planId': 'a'*64}, headers=HEADER).status_code == 403


def test_admin_login_issues_a_bound_cookie_and_plan_uses_host_roots(host):
    app = application(serve_args(host.root))
    with TestClient(app, base_url='https://spark.example') as client:
        # Login without the same-origin request header is refused before any check.
        assert client.post('/api/setup/session', json={'code': host.code}).status_code == 403
        assert client.post('/api/setup/session', json={'code': host.code, 'extra': 1}, headers=HEADER).status_code == 422
        response = client.post('/api/setup/session', json={'code': host.code}, headers=HEADER)
        assert response.status_code == 200 and response.json()['signedIn'] is True
        cookie = response.headers['set-cookie']
        assert admin.COOKIE in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie.lower().replace('samesite=strict', 'SameSite=strict')
        assert 'Secure' in cookie and 'Path=/api/setup' in cookie
        assert host.code not in cookie
        assert response.headers['cache-control'] == 'no-store'
        assert client.get('/api/setup/session').json()['signedIn'] is True
        plan = client.get('/api/setup/background/plan')
        assert plan.status_code == 200
        body = plan.json()
        assert body['artifactRoot'] == str(host.root.resolve()/'artifacts')
        assert body['installationRoot'].startswith(str(host.root.resolve()/'runtimes'))
        assert client.get('/api/setup/background/status').json()['recordedStatus'] == 'not-installed'
        assert client.get('/api/setup/background/status').json()['externallyControlled'] is False


def test_writes_require_same_origin_proof_and_malformed_actions_are_refused(host):
    app = application(serve_args(host.root))
    with TestClient(app) as client:
        assert client.post('/api/setup/session', json={'code': host.code}, headers=HEADER).status_code == 200
        install = '/api/setup/background/install'
        assert client.post(install, json={'planId': 'a'*64}).status_code == 403
        assert client.post(install, json={'planId': 'a'*64}, headers={**HEADER, 'Origin': 'https://evil.example'}).status_code == 403
        assert client.post(install, json={'planId': 'a'*64}, headers={**HEADER, 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
        same = {**HEADER, 'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin'}
        assert client.post(install, json={'planId': 'a'*64, 'root': '/arbitrary'}, headers=same).status_code == 422
        assert client.post(install, json={'planId': 'short'}, headers=same).status_code == 422
        assert client.post('/api/setup/background/activation', json={'planId': 'a'*64, 'enabled': 'yes'}, headers=same).status_code == 422
        # A well-formed stale plan reaches the controller and is refused there, not executed.
        assert client.post(install, json={'planId': 'a'*64}, headers=same).status_code == 409
        assert client.post('/api/setup/background/remove', json={'planId': 'a'*64}, headers=same).status_code == 409
        assert not (host.root/'runtimes').exists()


def test_attempts_are_bounded_and_sessions_expire_logout_and_revoke(host, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(admin.time, 'monotonic', lambda: now[0])
    app = application(serve_args(host.root))
    with TestClient(app) as client:
        for _ in range(admin.ATTEMPT_LIMIT):
            assert client.post('/api/setup/session', json={'code': 'wrong'}, headers=HEADER).status_code == 403
        limited = client.post('/api/setup/session', json={'code': host.code}, headers=HEADER)
        assert limited.status_code == 429 and limited.headers['retry-after']
        now[0] += admin.ATTEMPT_WINDOW
        assert client.post('/api/setup/session', json={'code': host.code}, headers=HEADER).status_code == 200
        assert client.get('/api/setup/background/plan').status_code == 200
        now[0] += admin.IDLE_AGE + 1
        assert client.get('/api/setup/background/plan').status_code == 403
        assert client.post('/api/setup/session', json={'code': host.code}, headers=HEADER).status_code == 200
        assert client.delete('/api/setup/session').status_code == 403  # needs same-origin proof
        assert client.delete('/api/setup/session', headers=HEADER).status_code == 200
        assert client.get('/api/setup/background/plan').status_code == 403
        assert client.post('/api/setup/session', json={'code': host.code}, headers=HEADER).status_code == 200
        admin.revoke(host.root)  # operator CLI revocation reaches the running process
        assert client.get('/api/setup/background/plan').status_code == 403
        assert client.post('/api/setup/session', json={'code': host.code}, headers=HEADER).status_code == 200
        now[0] += admin.MAX_AGE + 1
        assert client.get('/api/setup/session').json()['signedIn'] is False


def test_startup_restores_saved_activation_only_through_host_verification(host, monkeypatch):
    root = host.root.resolve()
    app = application(serve_args(host.root))
    assert app.state.background_host.receipt is None
    write_receipt(root/'studio-background-enabled.json', {'version': 1, 'enabled': True, 'receipt': '/elsewhere'})
    app = application(serve_args(host.root))
    assert Path(app.state.background_host.receipt) == root/'artifacts/qualification.json'
    # No qualification exists: the host must fail verification and advertise nothing.
    with TestClient(app) as client:
        app.state.background_host.thread.join(5)
        assert app.state.background_host.engines() == []
        assert client.post('/api/setup/session', json={'code': host.code}, headers=HEADER).status_code == 200
        status = client.get('/api/setup/background/status').json()
        assert status['desiredEnabled'] is True and status['hostReady'] is False and status['hostError']
    # An explicit receipt argument makes activation externally controlled.
    receipt = root/'artifacts/qualification.json'
    app = application(serve_args(host.root, '--background-receipt', str(receipt)))
    with TestClient(app) as client:
        assert client.post('/api/setup/session', json={'code': host.code}, headers=HEADER).status_code == 200
        assert client.get('/api/setup/background/status').json()['externallyControlled'] is True
        blocked = client.post('/api/setup/background/activation', json={'planId': client.get('/api/setup/background/plan').json()['planId'], 'enabled': True}, headers=HEADER)
        assert blocked.status_code == 409 and 'server configuration' in blocked.json()['detail']


def test_factory_refuses_half_wired_setup_and_mismatched_roots(tmp_path, host):
    from media_lab_core.studio_server import create_paired_app
    from media_lab_core.studio_gate import Credentials
    credentials = Credentials('a'*64, 'b'*64)
    args = dict(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts', media_root=tmp_path/'media',
                load_rows=lambda: [], credentials=credentials)
    with pytest.raises(ValueError, match='both'):
        create_paired_app(**args, setup=Setup(tmp_path, artifact_root=tmp_path/'artifacts'))
    with pytest.raises(ValueError, match='same artifact root'):
        create_paired_app(**args, admin=admin.AdminGate(host.root), setup=Setup(tmp_path, artifact_root=tmp_path/'other'))
    app = create_paired_app(**args)
    with TestClient(app) as client:
        assert client.get('/manifest.json').json()['vibexStudio']['modelSetup'] is False
        assert client.get('/setup/background').status_code == 404
        assert client.get('/api/setup/session').status_code == 404
        assert client.get('/api/setup/background/plan').status_code == 404


def test_setup_and_cli_output_never_contain_secrets(host, capsys):
    app = application(serve_args(host.root))
    with TestClient(app) as client:
        assert client.post('/api/setup/session', json={'code': host.code}, headers=HEADER).status_code == 200
        text = client.get('/api/setup/background/plan').text + client.get('/api/setup/background/status').text
        assert host.code not in text and read_credentials(host.root).code not in text
    assert main(['inspect', str(host.root)]) == 0
    assert host.code not in capsys.readouterr().out
