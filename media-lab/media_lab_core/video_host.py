"""Opt-in Wan2.2 TI2V-5B video host for the independent server (GPU, canonical lease, warm renderer).

An operator config names the pinned runtime interpreter (hashed), the pinned Wan2.2 source checkout (its commit is
re-read from .git), the checkpoint directory plus its per-file manifest, the shared inference lock and the exact
revision. Every weight file is re-verified by size and SHA-256 before the engine is advertised.
"""
from functools import partial
import json
from pathlib import Path
import subprocess
import threading

from . import video_jobs
from .background_lifecycle import lifecycle_slot
from .cpu_worker import WorkerBusy
from .music_host import _small_regular, sha256_file, DEFAULT_IDLE
from .video_worker import make_resident, run_video_job

KEYS = {'version', 'runtime', 'runtime_sha256', 'source', 'source_commit', 'checkpoints', 'weights_manifest', 'inference_lock', 'revision'}
OPTIONAL = {'resident_idle_seconds', 'memory_gib'}  # memory_gib: owned-process budget incl. unified GPU memory (default 48)
MAX_FRAMES = 121
DEFAULT_MEMORY_GIB = 48  # 20fo tracer peaked at 44.9 GB on the GB10's unified memory


def read_config(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Use an absolute video configuration path.')
    data = json.loads(_small_regular(path, 8192))
    if type(data) is not dict or not KEYS <= set(data) <= KEYS | OPTIONAL or data['version'] != 1:
        raise ValueError('Invalid video configuration fields.')
    idle = data.get('resident_idle_seconds', DEFAULT_IDLE)
    if type(idle) is not int or not 0 <= idle <= 86400:
        raise ValueError('resident_idle_seconds must be a whole number of seconds up to one day.')
    memory = data.get('memory_gib', DEFAULT_MEMORY_GIB)
    if type(memory) is not int or not 16 <= memory <= 1024:
        raise ValueError('memory_gib must be a whole number of GiB between 16 and 1024.')
    for key in ('runtime', 'source', 'checkpoints', 'weights_manifest', 'inference_lock'):
        if type(data[key]) is not str or not Path(data[key]).is_absolute():
            raise ValueError('Video configuration paths must be absolute strings.')
    if (type(data['revision']) is not str or not 1 <= len(data['revision']) <= 128
            or type(data['runtime_sha256']) is not str or len(data['runtime_sha256']) != 64
            or type(data['source_commit']) is not str or len(data['source_commit']) != 40):
        raise ValueError('Video configuration needs an exact revision, runtime hash and source commit.')
    return data


def verify_pack(config):
    import os
    runtime = Path(config['runtime']); resolved = runtime.resolve()
    if not resolved.is_file() or resolved.is_symlink() or not os.access(resolved, os.X_OK):
        raise ValueError('The video runtime interpreter is missing.')
    if sha256_file(resolved) != config['runtime_sha256']:
        raise ValueError('The video runtime interpreter changed. Requalify it.')
    source = Path(config['source'])
    if not (source / 'wan' / '__init__.py').is_file():
        raise ValueError('The Wan2.2 source checkout is missing.')
    head = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=30)
    if head.returncode != 0 or head.stdout.strip() != config['source_commit']:
        raise ValueError('The Wan2.2 source checkout is not at its pinned commit.')
    manifest = json.loads(_small_regular(config['weights_manifest'], 65536))
    rows = manifest.get('files') if isinstance(manifest, dict) else None
    if not rows:
        raise ValueError('The weights manifest lists no files.')
    root = Path(config['checkpoints'])
    for row in rows:
        path = root / row['path']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes'] or sha256_file(path) != row['sha256']:
            raise ValueError('A pinned video weight file does not match its manifest: ' + row['path'])
    return len(rows)


class VideoHost:
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
        self.thread = threading.Thread(target=self._run, name='studio-video-gpu', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with lifecycle_slot(self.root / 'video'):
                config = read_config(self.config_path)
                self.verified_files = verify_pack(config)
                self.config = config
                idle = config.get('resident_idle_seconds', DEFAULT_IDLE)
                self.resident = make_resident(runtime=Path(config['runtime']), source=Path(config['source']),
                                              checkpoints=Path(config['checkpoints']), revision=config['revision'],
                                              home=self.root / 'video' / 'resident', idle_seconds=max(idle, 1),
                                              memory_bytes=config.get('memory_gib', DEFAULT_MEMORY_GIB) * 1024**3,
                                              shutting_down=self.stop_requested.is_set)
                execute = partial(run_video_job, resident=self.resident, inference_lock=Path(config['inference_lock']),
                                  revision=config['revision'])
                self.ready = True
                while not self.stop_requested.is_set():
                    try:
                        video_jobs.run_next(self.get_store(), root=self.root, revision=config['revision'], execute=execute)
                    except WorkerBusy:
                        pass
                    if idle == 0 and self.resident.alive():
                        self.resident.terminate()  # per-job process requested: drop the model right after each job
                    else:
                        self.resident.reap_idle()
                    self.stop_requested.wait(1)
        except Exception:
            self.error = 'The video host needs a verified configuration or repair.'
        finally:
            self.ready = False
            if self.resident is not None:
                self.resident.terminate()

    def engines(self):
        if not self.ready or not self.thread or not self.thread.is_alive() or self.stop_requested.is_set():
            return []
        return [{'id': video_jobs.ENGINE, 'revision': self.config['revision'], 'operation': 'text-to-video',
                 'device': 'gpu', 'maxFrames': MAX_FRAMES, 'fps': 24, 'sizes': ['704*1280', '1280*704'], 'experimental': True,
                 'warm': bool(self.resident is not None and self.resident.alive())}]

    def admit(self, payload):
        from fastapi import HTTPException
        if not self.engines():
            raise HTTPException(503, self.error or 'No independently qualified video engine is connected yet.')
        try:
            video_jobs.validate_payload(payload, self.config['revision'])
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
