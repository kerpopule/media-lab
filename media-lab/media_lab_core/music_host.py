"""Opt-in ACE-Step music host for the independent server (GPU, canonical lease).

An operator config names the pinned runtime interpreter (hashed), the checkpoint
directory plus its per-file manifest, the shared inference lock and the exact
revision. Every weight file is re-verified by size and SHA-256 before the engine
is advertised. No download, install or model substitution happens here.
"""
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import stat
import threading

from . import music_jobs
from .background_lifecycle import lifecycle_slot
from .cpu_worker import WorkerBusy
from .music_worker import run_music_job, ResidentRenderer

KEYS = {'version', 'runtime', 'runtime_sha256', 'checkpoints', 'weights_manifest', 'inference_lock', 'revision'}
OPTIONAL = {'resident_idle_seconds'}  # 0 = a fresh renderer per job; default keeps the model warm for 10 minutes
DEFAULT_IDLE = 600


def _small_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Music configuration must be a small regular file.')
        return stream.read(limit + 1)


def read_config(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Use an absolute music configuration path.')
    data = json.loads(_small_regular(path, 8192))
    if type(data) is not dict or not KEYS <= set(data) <= KEYS | OPTIONAL or data['version'] != 1:
        raise ValueError('Invalid music configuration fields.')
    idle = data.get('resident_idle_seconds', DEFAULT_IDLE)
    if type(idle) is not int or not 0 <= idle <= 86400:
        raise ValueError('resident_idle_seconds must be a whole number of seconds up to one day.')
    for key in ('runtime', 'checkpoints', 'weights_manifest', 'inference_lock'):
        if type(data[key]) is not str or not Path(data[key]).is_absolute():
            raise ValueError('Music configuration paths must be absolute strings.')
    if (type(data['revision']) is not str or not 1 <= len(data['revision']) <= 128
            or type(data['runtime_sha256']) is not str or len(data['runtime_sha256']) != 64):
        raise ValueError('Music configuration needs an exact revision and runtime hash.')
    return data


def sha256_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_pack(config):
    runtime = Path(config['runtime']); resolved = runtime.resolve()
    if not resolved.is_file() or resolved.is_symlink() or not os.access(resolved, os.X_OK):
        raise ValueError('The music runtime interpreter is missing.')
    if sha256_file(resolved) != config['runtime_sha256']:
        raise ValueError('The music runtime interpreter changed. Requalify it.')
    manifest = json.loads(_small_regular(config['weights_manifest'], 65536))
    rows = manifest.get('files') if isinstance(manifest, dict) else None
    if not rows:
        raise ValueError('The weights manifest lists no files.')
    root = Path(config['checkpoints'])
    for row in rows:
        path = root / row['path']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes'] or sha256_file(path) != row['sha256']:
            raise ValueError('A pinned music weight file does not match its manifest: ' + row['path'])
    return len(rows)


class MusicHost:
    def __init__(self, get_store, root, config_path=None):
        self.get_store = get_store
        self.root = Path(root)
        self.config_path = Path(config_path) if config_path else None
        self.config = None
        self.thread = None
        self.stop_requested = threading.Event()
        self.ready = False
        self.error = None
        self.verified_files = 0
        self.resident = None

    def start(self):
        if not self.config_path or (self.thread and self.thread.is_alive()):
            return
        self.stop_requested.clear(); self.ready = False; self.error = None
        self.thread = threading.Thread(target=self._run, name='studio-music-gpu', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with lifecycle_slot(self.root / 'music'):
                config = read_config(self.config_path)
                self.verified_files = verify_pack(config)
                self.config = config
                idle = config.get('resident_idle_seconds', DEFAULT_IDLE)
                self.resident = ResidentRenderer(runtime=Path(config['runtime']), checkpoints=Path(config['checkpoints']),
                                                 revision=config['revision'], home=self.root / 'music' / 'resident',
                                                 idle_seconds=idle, shutting_down=self.stop_requested.is_set) if idle > 0 else None
                execute = partial(run_music_job, runtime=Path(config['runtime']), checkpoints=Path(config['checkpoints']),
                                  inference_lock=Path(config['inference_lock']), revision=config['revision'], resident=self.resident)
                self.ready = True
                while not self.stop_requested.is_set():
                    try:
                        music_jobs.run_next(self.get_store(), root=self.root, revision=config['revision'], execute=execute)
                    except WorkerBusy:
                        pass
                    if self.resident is not None:
                        self.resident.reap_idle()
                    self.stop_requested.wait(1)
        except Exception:
            self.error = 'The music host needs a verified configuration or repair.'
        finally:
            self.ready = False
            if self.resident is not None:
                self.resident.terminate()

    def engines(self):
        if not self.ready or not self.thread or not self.thread.is_alive() or self.stop_requested.is_set():
            return []
        return [{'id': music_jobs.ENGINE, 'revision': self.config['revision'], 'operation': 'compose',
                 'device': 'gpu', 'maxSeconds': 120, 'experimental': True,
                 'warm': bool(self.resident is not None and self.resident.alive())}]

    def admit(self, payload):
        from fastapi import HTTPException
        if not self.engines():
            raise HTTPException(503, self.error or 'No independently qualified music engine is connected yet.')
        try:
            music_jobs.validate_payload(payload, self.config['revision'])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    def stop(self):
        # Stop the model runtime before waiting for the worker: an in-flight render would otherwise hold the
        # thread until it finished, and a supervisor's stop timeout would kill the process mid-job. Terminating
        # the resident first confirms the runtime is gone, then the owned job is released back to the queue.
        self.stop_requested.set()
        if self.resident is not None:
            self.resident.terminate()
        if self.thread:
            self.thread.join(timeout=60)
