"""Opt-in candidate image-to-3D host (TripoSR CPU) for the independent server.

Mirrors speech_host: an operator-owned absolute configuration names the reviewed
isolated runtime interpreter (pinned by hash), the verified package directory and
the explicit runtime profile. Every model component is re-verified before the
engine is advertised, and results are verified again before publication by the
existing worker. This is a draft-quality candidate, not an installer.
"""
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import stat
import threading

from . import triposr_jobs
from .background_lifecycle import lifecycle_slot
from .cpu_worker import WorkerBusy
from .triposr_compatibility import expected_runtime_receipt
from .triposr_cpu import MODEL_SHA, VARIANT, verify_package
from .triposr_worker import run_triposr_job

KEYS = {'version', 'runtime', 'runtime_sha256', 'package', 'profile'}


def _private_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('3D configuration must be a small regular file.')
        return stream.read(limit + 1)


def read_config(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Use an absolute 3D configuration path.')
    data = json.loads(_private_regular(path, 8192))
    if type(data) is not dict or set(data) != KEYS or data['version'] != 1:
        raise ValueError('Invalid 3D configuration fields.')
    for key in ('runtime', 'package'):
        if type(data[key]) is not str or not Path(data[key]).is_absolute():
            raise ValueError('3D configuration paths must be absolute strings.')
    if type(data['runtime_sha256']) is not str or len(data['runtime_sha256']) != 64:
        raise ValueError('3D configuration needs the exact runtime interpreter hash.')
    expected_runtime_receipt(profile=data['profile'])  # Refuses unknown profiles.
    return data


def sha256_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_pack(config):
    runtime = Path(config['runtime'])
    resolved = runtime.resolve()
    if not resolved.is_file() or resolved.is_symlink() or not os.access(resolved, os.X_OK):
        raise ValueError('The 3D runtime interpreter is missing.')
    if sha256_file(resolved) != config['runtime_sha256']:
        raise ValueError('The 3D runtime interpreter changed. Requalify it.')
    verify_package(Path(config['package']))  # model.safetensors, config, prepared source, DINO config
    return True


class TriposrHost:
    """Owned-queue 3D worker; advertises only while verified and running."""

    def __init__(self, get_store, root, config_path=None):
        self.get_store = get_store
        self.root = Path(root)
        self.config_path = Path(config_path) if config_path else None
        self.config = None
        self.thread = None
        self.stop_requested = threading.Event()
        self.ready = False
        self.error = None

    def start(self):
        if not self.config_path or (self.thread and self.thread.is_alive()):
            return
        self.stop_requested.clear()
        self.ready = False
        self.error = None
        self.thread = threading.Thread(target=self._run, name='studio-triposr-cpu', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with lifecycle_slot(self.root / 'model3d'):
                config = read_config(self.config_path)
                verify_pack(config)
                self.config = config
                execute = partial(run_triposr_job, runtime=Path(config['runtime']),
                                  package=Path(config['package']), runtime_profile=config['profile'])
                self.ready = True
                while not self.stop_requested.is_set():
                    try:
                        triposr_jobs.run_next(self.get_store(), root=self.root, execute=execute)
                    except WorkerBusy:
                        pass
                    self.stop_requested.wait(1)
        except Exception:
            self.error = 'The 3D host needs a verified configuration or repair.'
        finally:
            self.ready = False

    def engines(self):
        if not self.ready or not self.thread or not self.thread.is_alive() or self.stop_requested.is_set():
            return []
        return [{'id': triposr_jobs.ENGINE, 'revision': MODEL_SHA, 'operation': 'image-to-3d',
                 'variant': VARIANT, 'creativeStatus': 'draft'}]

    def admit(self, payload):
        from fastapi import HTTPException
        if not self.engines():
            raise HTTPException(503, self.error or 'No independently qualified 3D engine is connected yet.')
        try:
            triposr_jobs.validate_payload(payload)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    def stop(self):
        self.stop_requested.set()
        if self.thread:
            self.thread.join(timeout=630)
