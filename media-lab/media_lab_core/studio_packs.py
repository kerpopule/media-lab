"""Administrator enable/disable lifecycle for the operator-configured packs.

Speech and 3D packs are configured by absolute config files placed by the
operator (their runtimes are local builds, not downloadable wheels yet). What an
administrator can do from the setup page is turn a configured pack on or off;
the choice is persisted beside the host data and restored on start after the
host re-verifies the pack. Nothing here downloads, installs or removes files.
"""
import json
from pathlib import Path
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .background_install import write_receipt

PACKS = ('speech', 'model3d', 'music', 'video', 'image')
FILE = 'studio-packs.json'


def read_preferences(root):
    """Saved on/off choices; a missing or malformed file means every configured pack is on."""
    path = Path(root)/FILE
    result = {pack: True for pack in PACKS}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return result
        value = json.loads(path.read_text())
        if value.get('version') != 1:
            return result
        for pack in PACKS:
            if isinstance(value.get(pack), dict) and value[pack].get('enabled') is False:
                result[pack] = False
    except (OSError, ValueError, AttributeError):
        pass
    return result


def write_preferences(root, preferences):
    write_receipt(Path(root)/FILE, {'version': 1, **{pack: {'enabled': bool(preferences.get(pack, True))} for pack in PACKS}})


class Packs:
    """Owns the on/off state of the speech and 3D hosts for one server process."""

    def __init__(self, root, hosts):
        self.root = Path(root)
        self.hosts = {pack: hosts[pack] for pack in PACKS}
        self.lock = threading.Lock()
        self.thread = None
        self.operation = None
        self.error = None

    def status(self):
        preferences = read_preferences(self.root)
        running = bool(self.thread and self.thread.is_alive())
        rows = {}
        for pack, host in self.hosts.items():
            rows[pack] = {'configured': host.config_path is not None,
                          'enabled': preferences[pack],
                          'active': bool(host.thread and host.thread.is_alive()),
                          'ready': bool(host.engines()),
                          'error': host.error,
                          'engine': (host.engines() or [{}])[0].get('id')}
        return {'version': 1, 'packs': rows, 'operation': self.operation if running else None,
                'runningHere': running, 'error': self.error,
                'note': 'Speech and 3D packs are configured by files the operator places on the server; this page only turns a configured pack on or off.'}

    def activation(self, pack, enabled):
        if pack not in PACKS:
            raise ValueError('Unknown pack.')
        host = self.hosts[pack]
        if host.config_path is None:
            raise ValueError('This pack is not configured on the server. Place its configuration file and restart with the matching serve flag.')
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('Another pack change is still finishing.')
            preferences = read_preferences(self.root)
            preferences[pack] = enabled
            write_preferences(self.root, preferences)  # Persist before acting; a restart honours the choice.
            self.error = None
            self.operation = ('enable ' if enabled else 'disable ') + pack

            def run():
                try:
                    if enabled:
                        host.start()
                    else:
                        host.stop()  # Finishes healthy owned work; never cancels it.
                except Exception:
                    self.error = 'The pack could not change state. Check its configuration and active work.'
            self.thread = threading.Thread(target=run, name='studio-pack-' + pack, daemon=True)
            self.thread.start()
        return {'accepted': True, 'pack': pack, 'enabled': enabled}


class Activation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    pack: str = Field(pattern=r'^(speech|model3d|music|video|image)$')
    enabled: bool


def router(packs: Packs, authorized):
    api = APIRouter()

    def require(request):
        if not authorized(request):
            raise HTTPException(403, 'Sign in with the server admin code to manage packs.')

    @api.get('/api/setup/packs')
    def status(request: Request):
        require(request)
        return packs.status()

    @api.post('/api/setup/packs/activation', status_code=202)
    def activation(body: Activation, request: Request):
        require(request)
        try:
            return packs.activation(body.pack, body.enabled)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    return api
