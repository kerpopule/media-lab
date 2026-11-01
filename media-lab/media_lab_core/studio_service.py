"""Install the independent host as a systemd --user service (Linux only).

Renders one unit that runs ``studio_cli serve`` with exactly the arguments the
operator passed, marks it as managed, refuses to overwrite a unit it did not
write, enables it, waits for the manifest, and reports lingering so the studio
survives logout. Uninstall stops and removes the unit and never touches data.
macOS/Windows service installation is not qualified and is refused plainly.
"""
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import time
import urllib.request

MARK = '# managed by vibex studio service; edit with `studio service`, not by hand'


def unit_path(name):
    if not name or len(name) > 48 or not all(c.isalnum() or c in '-_' for c in name):
        raise ValueError('Choose a service name of letters, digits, - or _ (max 48).')
    return Path.home()/'.config/systemd/user'/f'vibex-studio-{name}.service'


def render_unit(*, root, python, serve_args, description='VibeX Studio independent host',
                memory_max='24G', tasks_max=512, stop_timeout=90):
    root = Path(root)
    python = Path(python)
    if not root.is_absolute() or not python.is_absolute():
        raise ValueError('Service paths must be absolute.')
    for arg in serve_args:
        if not isinstance(arg, str) or '\n' in arg or '\0' in arg:
            raise ValueError('Invalid serve argument.')
    command = [str(python), '-m', 'media_lab_core.studio_cli', 'serve', str(root), *serve_args]
    source = Path(__file__).resolve().parents[1]
    return (f'{MARK}\n[Unit]\nDescription={description}\nAfter=network-online.target\n\n[Service]\n'
            f'WorkingDirectory={root}\nEnvironment=PYTHONPATH={source}\n'
            f'ExecStart={" ".join(shlex.quote(part) for part in command)}\n'
            f'Restart=on-failure\nRestartSec=3\nUMask=0077\nNoNewPrivileges=true\n'
            # Signal the host process only. A whole-group kill races model runtimes: they can die before the
            # host records that it is stopping, and in-flight owned work would be failed instead of requeued.
            f'KillMode=mixed\nKillSignal=SIGTERM\nTimeoutStopSec={stop_timeout}\n'
            f'MemoryMax={memory_max}\nTasksMax={tasks_max}\n\n[Install]\nWantedBy=default.target\n')


def _systemctl(run, *args):
    result = run(['systemctl', '--user', *args], capture_output=True, text=True)
    return result


def _require_linux():
    if platform.system() != 'Linux':
        raise ValueError('Service installation is qualified for Linux systemd --user only. '
                         'On macOS keep `studio serve` running in a terminal or a launchd agent you manage yourself.')


def preflight_interpreter(python, run=subprocess.run):
    """The unit must use the interpreter that has the server dependencies (a venv path, not its resolved base)."""
    result = run([str(python), '-c', 'import fastapi, uvicorn, psutil, PIL'], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f'{python} cannot import the server dependencies (fastapi, uvicorn, psutil, Pillow). '
                         'Pass --python with the virtual environment interpreter that has them installed.')


def install(name, *, root, python, serve_args, port, run=subprocess.run, wait_seconds=30, clock=time.monotonic,
            fetch=urllib.request.urlopen):
    _require_linux()
    path = unit_path(name)
    preflight_interpreter(python, run)
    rendered = render_unit(root=root, python=python, serve_args=serve_args)
    if path.exists() or path.is_symlink():
        existing = path.read_text() if path.is_file() and not path.is_symlink() else ''
        if not existing.startswith(MARK):
            raise ValueError(f'{path} exists and was not written by studio service. Remove or rename it first.')
        previous = path.with_suffix('.service.before-' + time.strftime('%Y%m%dT%H%M%S'))
        previous.write_text(existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered)
    path.chmod(0o600)
    for step in (('daemon-reload',), ('enable', path.name), ('restart', path.name)):
        result = _systemctl(run, *step)
        if result.returncode != 0:
            raise ValueError(f'systemctl --user {" ".join(step)} failed: {(result.stderr or "").strip()[:300]}')
    deadline = clock() + wait_seconds
    manifest = None
    while clock() < deadline:
        try:
            with fetch(f'http://127.0.0.1:{port}/manifest.json', timeout=2) as response:
                manifest = json.load(response)
                break
        except Exception:
            time.sleep(0.5)
    if manifest is None:
        _systemctl(run, 'disable', '--now', path.name)
        log = run(['journalctl', '--user', '-u', path.name, '-n', '8', '--no-pager', '-o', 'cat'], capture_output=True, text=True)
        raise ValueError('The service did not answer on its port; it was stopped and disabled again. Last log lines: '
                         + (log.stdout or '').strip()[-600:])
    linger = run(['loginctl', 'show-user', os.environ.get('USER', ''), '-p', 'Linger'], capture_output=True, text=True)
    lingering = 'Linger=yes' in (linger.stdout or '')
    return {'unit': str(path), 'active': True, 'manifest': manifest.get('vibexStudio', {}),
            'lingering': lingering,
            'next': [] if lingering else ['Run `loginctl enable-linger` (needs an administrator once) so the studio keeps running after logout and reboot.']}


def status(name, run=subprocess.run):
    _require_linux()
    path = unit_path(name)
    if not path.exists():
        return {'unit': str(path), 'installed': False}
    managed = path.read_text().startswith(MARK)
    active = _systemctl(run, 'is-active', path.name).stdout.strip()
    enabled = _systemctl(run, 'is-enabled', path.name).stdout.strip()
    return {'unit': str(path), 'installed': True, 'managed': managed, 'active': active, 'enabled': enabled}


def uninstall(name, run=subprocess.run):
    _require_linux()
    path = unit_path(name)
    if not path.exists():
        return {'unit': str(path), 'removed': False, 'note': 'No such service.'}
    if not path.read_text().startswith(MARK):
        raise ValueError(f'{path} was not written by studio service; not touching it.')
    _systemctl(run, 'disable', '--now', path.name)
    path.unlink()
    _systemctl(run, 'daemon-reload')
    return {'unit': str(path), 'removed': True, 'note': 'Host data, credentials, Library and models were retained.'}


def add_arguments(parser):
    parser.add_argument('action', choices=['install', 'status', 'uninstall'])
    parser.add_argument('name', help='Short service name, e.g. main')
    parser.add_argument('--root', help='Independent host data directory (install only)')
    parser.add_argument('--python', default=sys.executable, help='Interpreter with the server dependencies installed')
    parser.add_argument('--port', type=int, default=7864)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--web-root')
    parser.add_argument('--origin', action='append', default=[])
    parser.add_argument('--model-setup', action='store_true')
    parser.add_argument('--setup-python')
    parser.add_argument('--setup-uv')
    parser.add_argument('--speech-config')
    parser.add_argument('--triposr-config')


def serve_args_from(args):
    result = ['--bind', args.bind, '--port', str(args.port)]
    for key in ('web_root', 'setup_python', 'setup_uv', 'speech_config', 'triposr_config'):
        value = getattr(args, key)
        if value:
            result += ['--' + key.replace('_', '-'), value]
    for origin in args.origin:
        result += ['--origin', origin]
    if args.model_setup:
        result.append('--model-setup')
    return result


def main_from(args):
    if args.action == 'install':
        if not args.root:
            raise ValueError('--root is required to install a service.')
        # Keep the interpreter path as given: resolving a venv's bin/python to the base binary loses its packages.
        return install(args.name, root=Path(args.root).expanduser().resolve(), python=Path(args.python).expanduser().absolute(),
                       serve_args=serve_args_from(args), port=args.port)
    if args.action == 'status':
        return status(args.name)
    return uninstall(args.name)
