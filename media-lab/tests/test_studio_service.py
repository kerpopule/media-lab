"""systemd --user service installer for the independent host."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_lab_core import studio_service as service
from media_lab_core.studio_cli import parser


class FakeRun:
    def __init__(self, fail=None):
        self.calls = []; self.fail = fail or set()
    def __call__(self, command, capture_output=True, text=True):
        self.calls.append(command)
        key = ' '.join(command[2:4]) if command[0] == 'systemctl' else command[0]
        if command[0] == 'loginctl':
            return SimpleNamespace(returncode=0, stdout='Linger=yes\n', stderr='')
        if command[0] == 'journalctl':
            return SimpleNamespace(returncode=0, stdout='ModuleNotFoundError: fastapi\n', stderr='')
        if command[0] not in ('systemctl',):  # interpreter preflight
            return SimpleNamespace(returncode=1 if 'preflight' in self.fail else 0, stdout='', stderr='')
        if key in self.fail:
            return SimpleNamespace(returncode=1, stdout='', stderr='boom')
        return SimpleNamespace(returncode=0, stdout='active\n' if 'is-active' in command else 'enabled\n', stderr='')


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path)); monkeypatch.setenv('USER', 'tester')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(service.platform, 'system', lambda: 'Linux')
    return tmp_path


def fetch_ok(url, timeout=2):
    return io.BytesIO(json.dumps({'vibexStudio': {'version': 1, 'modelSetup': True}}).encode())


def test_unit_renders_exact_command_and_refuses_bad_paths(home):
    text = service.render_unit(root='/srv/vibex/host', python='/srv/venv/bin/python',
                               serve_args=['--port', '7864', '--origin', 'https://a b'])
    assert text.startswith(service.MARK)
    assert "ExecStart=/srv/venv/bin/python -m media_lab_core.studio_cli serve /srv/vibex/host --port 7864 --origin 'https://a b'" in text
    assert 'NoNewPrivileges=true' in text and 'WantedBy=default.target' in text
    with pytest.raises(ValueError, match='absolute'):
        service.render_unit(root='host', python='/p', serve_args=[])
    with pytest.raises(ValueError, match='Invalid serve argument'):
        service.render_unit(root='/h', python='/p', serve_args=['a\nb'])
    with pytest.raises(ValueError, match='service name'):
        service.unit_path('bad name')
    # The host must control its own model runtimes on stop; a group kill would fail in-flight owned jobs.
    assert 'KillMode=mixed' in text and 'KillSignal=SIGTERM' in text and 'TimeoutStopSec=90' in text

def test_install_writes_managed_unit_enables_and_waits_for_manifest(home):
    run = FakeRun()
    result = service.install('main', root='/srv/host', python='/srv/venv/bin/python', serve_args=['--port', '7864'],
                             port=7864, run=run, fetch=fetch_ok)
    unit = home/'.config/systemd/user/vibex-studio-main.service'
    assert unit.is_file() and unit.stat().st_mode & 0o777 == 0o600
    assert result['active'] and result['lingering'] and result['manifest']['modelSetup'] is True and result['next'] == []
    assert [c[2:] for c in run.calls if c[0] == 'systemctl'] == [['daemon-reload'], ['enable', unit.name], ['restart', unit.name]]
    assert service.status('main', run=run) == {'unit': str(unit), 'installed': True, 'managed': True, 'active': 'active', 'enabled': 'enabled'}


def test_install_refuses_foreign_unit_and_reports_systemctl_failures(home):
    unit = home/'.config/systemd/user/vibex-studio-main.service'
    unit.parent.mkdir(parents=True); unit.write_text('[Unit]\nDescription=someone else\n')
    with pytest.raises(ValueError, match='not written by studio service'):
        service.install('main', root='/srv/host', python='/p', serve_args=[], port=1, run=FakeRun(), fetch=fetch_ok)
    assert unit.read_text().startswith('[Unit]')
    with pytest.raises(ValueError, match='not touching'):
        service.uninstall('main', run=FakeRun())
    unit.unlink()
    with pytest.raises(ValueError, match='enable'):
        service.install('main', root='/srv/host', python='/p', serve_args=[], port=1, run=FakeRun(fail={'enable vibex-studio-main.service'}), fetch=fetch_ok)


def test_install_timeout_explains_and_uninstall_retains_data(home, tmp_path):
    clock = iter([0, 0.1, 100, 200, 300])
    def fetch_fail(url, timeout=2):
        raise OSError('refused')
    run = FakeRun()
    with pytest.raises(ValueError, match='did not answer.*ModuleNotFoundError'):
        service.install('main', root='/srv/host', python='/p', serve_args=[], port=1, run=run, wait_seconds=1,
                        clock=lambda: next(clock), fetch=fetch_fail)
    assert ['disable', '--now', 'vibex-studio-main.service'] in [c[2:] for c in run.calls if c[0] == 'systemctl']
    with pytest.raises(ValueError, match='cannot import the server dependencies'):
        service.install('main', root='/srv/host', python='/p', serve_args=[], port=1, run=FakeRun(fail={'preflight'}), fetch=fetch_ok)
    data = tmp_path/'host'; data.mkdir(); (data/'library.json').write_text('[]')
    run = FakeRun()
    result = service.uninstall('main', run=run)
    assert result['removed'] and not (home/'.config/systemd/user/vibex-studio-main.service').exists()
    assert (data/'library.json').read_text() == '[]'
    assert ['disable', '--now', 'vibex-studio-main.service'] in [c[2:] for c in run.calls]
    assert service.status('main', run=run) == {'unit': str(home/'.config/systemd/user/vibex-studio-main.service'), 'installed': False}


def test_non_linux_is_refused_plainly(home, monkeypatch):
    monkeypatch.setattr(service.platform, 'system', lambda: 'Darwin')
    with pytest.raises(ValueError, match='Linux systemd'):
        service.status('main')


def test_cli_maps_serve_flags_exactly():
    args = parser().parse_args(['service', 'install', 'main', '--root', '/srv/host', '--port', '62934', '--bind', '127.0.0.1',
                                '--origin', 'tauri://localhost', '--model-setup', '--setup-uv', '/u/uv', '--speech-config', '/c/s.json'])
    assert service.serve_args_from(args) == ['--bind', '127.0.0.1', '--port', '62934', '--setup-uv', '/u/uv', '--speech-config', '/c/s.json', '--origin', 'tauri://localhost', '--model-setup']
