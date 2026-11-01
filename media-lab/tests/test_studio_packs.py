"""Administrator on/off lifecycle for operator-configured speech and 3D packs."""
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from media_lab_core import studio_packs as packs
from media_lab_core import studio_admin as admin
from media_lab_core import speech_host
from media_lab_core.studio_cli import initialize, parser, application
from .test_speech_host import write_pack

HEADER = {'X-Setup-Request': '1'}


def fake_host(configured=True):
    h = SimpleNamespace(config_path='/cfg' if configured else None, thread=None, error=None, _engines=[])
    h.engines = lambda: h._engines
    def start():
        h.thread = SimpleNamespace(is_alive=lambda: True); h._engines = [{'id': 'x'}]
    def stop():
        h.thread = None; h._engines = []
    h.start, h.stop = start, stop
    return h


def test_preferences_default_on_and_ignore_bad_files(tmp_path):
    assert packs.read_preferences(tmp_path) == {'speech': True, 'model3d': True, 'music': True, 'video': True, 'image': True}
    (tmp_path/packs.FILE).write_text('[]')
    assert packs.read_preferences(tmp_path) == {'speech': True, 'model3d': True, 'music': True, 'video': True, 'image': True}
    packs.write_preferences(tmp_path, {'speech': False})
    assert packs.read_preferences(tmp_path) == {'speech': False, 'model3d': True, 'music': True, 'video': True, 'image': True}
    assert json.loads((tmp_path/packs.FILE).read_text()) == {'version': 1, 'speech': {'enabled': False}, 'model3d': {'enabled': True}, 'music': {'enabled': True}, 'video': {'enabled': True}, 'image': {'enabled': True}}


def test_activation_persists_then_starts_or_stops_and_refuses_unconfigured(tmp_path):
    speech, model3d = fake_host(), fake_host(configured=False)
    control = packs.Packs(tmp_path, {'speech': speech, 'model3d': model3d, 'music': fake_host(configured=False), 'video': fake_host(configured=False), 'image': fake_host(configured=False)})
    assert control.status()['packs']['model3d'] == {'configured': False, 'enabled': True, 'active': False, 'ready': False, 'error': None, 'engine': None}
    with pytest.raises(ValueError, match='not configured'):
        control.activation('model3d', True)
    with pytest.raises(ValueError, match='Unknown'):
        control.activation('sprites', True)
    assert control.activation('speech', True)['accepted']; control.thread.join(2)
    assert control.status()['packs']['speech']['ready'] is True and control.status()['packs']['speech']['engine'] == 'x'
    assert control.activation('speech', False)['accepted']; control.thread.join(2)
    assert packs.read_preferences(tmp_path)['speech'] is False and speech.thread is None
    assert control.status()['packs']['speech']['enabled'] is False and control.status()['runningHere'] is False


def test_server_routes_require_admin_and_restore_saved_choice(tmp_path, monkeypatch):
    path, config = write_pack(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, 'ITERATIONS', 1000)
    root = tmp_path/'host'; initialize(root); code = admin.enroll(root)
    entered = threading.Event()
    monkeypatch.setattr(speech_host.speech_jobs, 'run_next', lambda *a, **k: entered.set())
    args = ['serve', str(root), '--model-setup', '--speech-config', str(path)]
    with TestClient(application(parser().parse_args(args))) as client:
        assert entered.wait(3)
        assert client.get('/api/setup/packs').status_code == 403
        assert client.post('/api/setup/session', json={'code': code}, headers=HEADER).status_code == 200
        status = client.get('/api/setup/packs').json()
        assert status['packs']['speech']['ready'] is True and status['packs']['model3d']['configured'] is False
        assert client.post('/api/setup/packs/activation', json={'pack': 'speech', 'enabled': False}).status_code == 403
        assert client.post('/api/setup/packs/activation', json={'pack': 'nope', 'enabled': False}, headers=HEADER).status_code == 422
        assert client.post('/api/setup/packs/activation', json={'pack': 'model3d', 'enabled': True}, headers=HEADER).status_code == 409
        assert client.post('/api/setup/packs/activation', json={'pack': 'speech', 'enabled': False}, headers=HEADER).status_code == 202
        for _ in range(50):
            if client.get('/api/setup/packs').json()['packs']['speech']['active'] is False: break
            threading.Event().wait(0.1)
        row = client.get('/api/setup/packs').json()['packs']['speech']; assert (row['enabled'], row['active'], row['ready']) == (False, False, False)
    assert packs.read_preferences(root)['speech'] is False
    # A restart honours the saved choice: the speech host stays off until an administrator turns it on.
    entered.clear()
    with TestClient(application(parser().parse_args(args))) as client:
        assert not entered.wait(1)
        assert client.post('/api/setup/session', json={'code': code}, headers=HEADER).status_code == 200
        row = client.get('/api/setup/packs').json()['packs']['speech']; assert (row['enabled'], row['active']) == (False, False)
        assert client.post('/api/setup/packs/activation', json={'pack': 'speech', 'enabled': True}, headers=HEADER).status_code == 202
        assert entered.wait(3)
