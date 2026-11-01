import json
from pathlib import Path

from fastapi.testclient import TestClient

from media_lab_core import installer
from .test_cut_api import media_app  # isolated HOME and disabled workers


def test_public_video_recipes_cannot_launch_maestro(tmp_path, monkeypatch):
    catalog = json.loads((Path(__file__).parents[1] / 'config/engine-installs.json').read_text())
    def unexpected(*args, **kwargs):
        raise AssertionError('blocked setup must not launch a worker or write install state')
    monkeypatch.setattr(installer, '_update_state', unexpected)
    for name in ('ltx', 'h3'):
        spec = catalog[name]
        assert spec['blocked'] and not spec['default']
        assert spec['steps'] == []
        # The low-level entry point still refuses if a caller drops manual mode.
        assert not installer.start_install(name, {**spec, 'requires_manual': False}, tmp_path, tmp_path)


def test_blocked_setup_cannot_be_overridden_by_running_legacy_engine(media_app, monkeypatch):
    monkeypatch.setattr(media_app, '_setup_engine_health', lambda *args: True)
    monkeypatch.setattr(media_app, '_artifact_present', lambda *args: True)
    monkeypatch.setattr(media_app.engine_installer, 'engine_install_state', lambda *args: {'state': 'ready'})
    monkeypatch.setattr(media_app, '_gpu_present', lambda: True)
    status = media_app._setup_status()
    for name in ('ltx', 'h3'):
        entry = status['engines'][name]
        assert entry['state'] == 'blocked'
        assert entry['blocked'] and not entry['default']
        assert 'independent' in entry['detail'].lower()


def test_install_refuses_whole_batch_before_terms_or_engine_actions(media_app, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('rejected batch must have no install or terms side effects')
    monkeypatch.setattr(media_app.engine_installer, 'start_install', unexpected)
    monkeypatch.setattr(media_app, '_record_acceptance', unexpected)
    client = TestClient(media_app.app, base_url='http://127.0.0.1')
    assert client.post('/api/gate', json={'code': media_app.ADMIN_CODE}).status_code == 200
    response = client.post('/api/setup/install', json={'engines': ['image', 'h3'], 'accept_terms': {'h3': True}})
    assert response.status_code == 409
    assert response.json()['refused'][0]['engine'] == 'h3'
