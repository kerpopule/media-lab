"""Opt-in experimental English speech host for the independent server.

Serves the reviewed Chatterbox CPU pack only from an explicit, absolute,
operator-owned configuration file. Every artifact is re-verified by exact size
and SHA-256 at host start, the runtime interpreter is pinned by hash, and the
engine is advertised only while the worker thread owns the artifact lifecycle
slot. No download, install, voice cloning or device selection happens here.
"""
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import stat
import threading

from . import speech_jobs
from .background_lifecycle import lifecycle_slot
from .chatterbox_cpu import FILES, verified_bytes
from .cpu_worker import WorkerBusy
from .perth_cpu import MODEL_BYTES, MODEL_SHA256
from .speech_request import VOICE
from .speech_worker import run_speech_job

VOICE_BYTES = 105316
VOICE_SHA256 = '709e5a7fa80e010a011c8244f553853aed7a49c106fff54008fbd89a0f5a6148'
KEYS = {'version', 'runtime', 'runtime_sha256', 'models', 'watermark', 'voice', 'revision'}


def _private_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Speech configuration must be a small regular file.')
        return stream.read(limit + 1)


def read_config(path):
    """Load and validate the operator's speech configuration without loading models."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Use an absolute speech configuration path.')
    data = json.loads(_private_regular(path, 8192))
    if type(data) is not dict or set(data) != KEYS or data['version'] != 1:
        raise ValueError('Invalid speech configuration fields.')
    for key in ('runtime', 'models', 'watermark', 'voice'):
        value = data[key]
        if type(value) is not str or not Path(value).is_absolute():
            raise ValueError('Speech configuration paths must be absolute strings.')
    if (type(data['revision']) is not str or not 1 <= len(data['revision']) <= 128
            or type(data['runtime_sha256']) is not str or len(data['runtime_sha256']) != 64):
        raise ValueError('Speech configuration needs an exact revision and runtime hash.')
    return data


def sha256_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_pack(config):
    """Re-check every pinned artifact byte-for-byte; raises on any drift."""
    # A virtual environment's bin/python is normally a symlink to the base
    # interpreter; keep the venv path for execution but pin the real binary.
    runtime = Path(config['runtime'])
    resolved = runtime.resolve()
    if not resolved.is_file() or resolved.is_symlink() or not os.access(resolved, os.X_OK):
        raise ValueError('The speech runtime interpreter is missing.')
    if sha256_file(resolved) != config['runtime_sha256']:
        raise ValueError('The speech runtime interpreter changed. Requalify it.')
    for name in FILES:
        verified_bytes(config['models'], name)
    for key, size, digest in (('watermark', MODEL_BYTES, MODEL_SHA256), ('voice', VOICE_BYTES, VOICE_SHA256)):
        path = Path(config[key])
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size or sha256_file(path) != digest:
            raise ValueError('A pinned speech artifact does not match its reviewed hash.')
    return True


class SpeechHost:
    """Owned-queue speech worker; advertises only while verified and running."""

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
        self.thread = threading.Thread(target=self._run, name='studio-speech-cpu', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            # Separate lifecycle slot beside the image host: same artifact root,
            # different lock file, so both packs never share one worker lease.
            with lifecycle_slot(self.root / 'speech'):
                config = read_config(self.config_path)
                verify_pack(config)
                self.config = config
                execute = partial(run_speech_job, runtime=Path(config['runtime']), models=Path(config['models']),
                                  watermark=Path(config['watermark']), voice=Path(config['voice']),
                                  revision=config['revision'])
                self.ready = True
                while not self.stop_requested.is_set():
                    try:
                        speech_jobs.run_next(self.get_store(), root=self.root, revision=config['revision'], execute=execute)
                    except WorkerBusy:
                        pass
                    self.stop_requested.wait(1)
        except Exception:
            self.error = 'The speech host needs a verified configuration or repair.'
        finally:
            self.ready = False

    def engines(self):
        if not self.ready or not self.thread or not self.thread.is_alive() or self.stop_requested.is_set():
            return []
        return [{'id': speech_jobs.ENGINE, 'revision': self.config['revision'], 'operation': 'speak',
                 'voice': VOICE, 'language': 'en', 'experimental': True}]

    def admit(self, payload):
        from fastapi import HTTPException
        if not self.engines():
            raise HTTPException(503, self.error or 'No independently qualified speech engine is connected yet.')
        try:
            speech_jobs.validate_payload(payload, self.config['revision'])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    def stop(self):
        self.stop_requested.set()
        if self.thread:
            self.thread.join(timeout=630)
