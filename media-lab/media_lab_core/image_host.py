"""Opt-in Qwen-Image-2.1 image host for the independent server (GPU, canonical lease, warm renderer).

An operator config names the pinned runtime interpreter (hashed), the checkpoint directory plus its per-file
manifest, the shared inference lock and the exact revision. Every weight file is re-verified before the engine
is advertised. No download, install or model substitution happens here.
"""
from functools import partial
import json
from pathlib import Path
import threading

from . import engine_licences, image_jobs
from .background_lifecycle import lifecycle_slot
from .cpu_worker import WorkerBusy
from .music_host import _small_regular, sha256_file, DEFAULT_IDLE
from .image_worker import make_resident, run_image_job

KEYS = {'version', 'runtime', 'runtime_sha256', 'checkpoints', 'weights_manifest', 'inference_lock', 'revision'}
OPTIONAL = {'resident_idle_seconds', 'memory_gib'}
# Measured, not declared: the clean 2026-09-21 per-phase run on the media Spark (no other GPU process,
# checkpoint page cache dropped) took system memory from 117.1 GiB down to a 69.06 GiB floor while
# loading and rendering this pack -- 48.0 GiB consumed -- with the device allocation flat at
# 38.4 GiB. 48 GiB is therefore the cold-load admission floor the measurement supports, and the
# capacity planner refuses co-residency while the active-phase upper bound plus the protected
# primary exceeds the allocatable pool.
DEFAULT_MEMORY_GIB = 48


def read_config(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Use an absolute image configuration path.')
    data = json.loads(_small_regular(path, 8192))
    if type(data) is not dict or not KEYS <= set(data) <= KEYS | OPTIONAL or data['version'] != 1:
        raise ValueError('Invalid image configuration fields.')
    idle = data.get('resident_idle_seconds', DEFAULT_IDLE)
    if type(idle) is not int or not 0 <= idle <= 86400:
        raise ValueError('resident_idle_seconds must be a whole number of seconds up to one day.')
    memory = data.get('memory_gib', DEFAULT_MEMORY_GIB)
    if type(memory) is not int or not 16 <= memory <= 1024:
        raise ValueError('memory_gib must be a whole number of GiB between 16 and 1024.')
    for key in ('runtime', 'checkpoints', 'weights_manifest', 'inference_lock'):
        if type(data[key]) is not str or not Path(data[key]).is_absolute():
            raise ValueError('Image configuration paths must be absolute strings.')
    if (type(data['revision']) is not str or not 1 <= len(data['revision']) <= 128
            or type(data['runtime_sha256']) is not str or len(data['runtime_sha256']) != 64):
        raise ValueError('Image configuration needs an exact revision and runtime hash.')
    return data


def verify_pack(config):
    import os
    runtime = Path(config['runtime']); resolved = runtime.resolve()
    if not resolved.is_file() or resolved.is_symlink() or not os.access(resolved, os.X_OK):
        raise ValueError('The image runtime interpreter is missing.')
    if sha256_file(resolved) != config['runtime_sha256']:
        raise ValueError('The image runtime interpreter changed. Requalify it.')
    manifest = json.loads(_small_regular(config['weights_manifest'], 65536))
    rows = manifest.get('files') if isinstance(manifest, dict) else None
    if not rows:
        raise ValueError('The weights manifest lists no files.')
    root = Path(config['checkpoints'])
    for row in rows:
        path = root / row['path']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes'] or sha256_file(path) != row['sha256']:
            raise ValueError('A pinned image weight file does not match its manifest: ' + row['path'])
    return len(rows)


class ImageHost:
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
        if not engine_licences.enabled('qwen-image-21'):
            # Research-licence weights: a configured pack still stays off until
            # the host opts in (MEDIA_LAB_PERSONAL_ENGINES in config/local.env).
            self.ready = False
            self.error = engine_licences.refusal('qwen-image-21')
            return
        self.stop_requested.clear(); self.ready = False; self.error = None
        self.thread = threading.Thread(target=self._run, name='studio-image-gpu', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with lifecycle_slot(self.root / 'image'):
                config = read_config(self.config_path)
                self.verified_files = verify_pack(config)
                self.config = config
                idle = config.get('resident_idle_seconds', DEFAULT_IDLE)
                self.resident = make_resident(runtime=Path(config['runtime']), checkpoints=Path(config['checkpoints']),
                                              revision=config['revision'], home=self.root / 'image' / 'resident', idle_seconds=max(idle, 1),
                                              memory_bytes=config.get('memory_gib', DEFAULT_MEMORY_GIB) * 1024**3,
                                              shutting_down=self.stop_requested.is_set)
                execute = partial(run_image_job, resident=self.resident, inference_lock=Path(config['inference_lock']),
                                  revision=config['revision'], store=self.get_store)
                self.ready = True
                while not self.stop_requested.is_set():
                    try:
                        image_jobs.run_next(self.get_store(), root=self.root, revision=config['revision'], execute=execute)
                    except WorkerBusy:
                        pass
                    if idle == 0 and self.resident.alive():
                        self.resident.terminate()
                    else:
                        self.resident.reap_idle()
                    self.stop_requested.wait(1)
        except Exception:
            self.error = 'The image host needs a verified configuration or repair.'
        finally:
            self.ready = False
            if self.resident is not None:
                self.resident.terminate()

    def engines(self):
        if not self.ready or not self.thread or not self.thread.is_alive() or self.stop_requested.is_set():
            return []
        return [{'id': image_jobs.ENGINE, 'revision': self.config['revision'], 'operation': image_jobs.TEXT,
                 'operations': [image_jobs.TEXT, image_jobs.EDIT],
                 'device': 'gpu', 'sizes': ['1024*1024', '1280*768', '768*1280'], 'experimental': True,
                 'warm': bool(self.resident is not None and self.resident.alive())}]

    def admit(self, payload):
        from fastapi import HTTPException
        if not self.engines():
            raise HTTPException(503, self.error or 'No independently qualified image engine is connected yet.')
        try:
            image_jobs.validate_payload(payload, self.config['revision'])
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
