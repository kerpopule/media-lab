"""Bounded video executor: canonical GPU lease + owned (warm) renderer + verified atomic publication."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from .cpu_worker import cpu_slot, WorkerStopped
from .music_worker import gpu_lease, ResidentRenderer
from .video_request import decode_request
from .video_artifact import inspect_mp4, MAX_BYTES

RENDER_MODULE = 'media_lab_core.video_render'
ENV = {'THP_MEM_ALLOC_ENABLE': '1'}


def verify_result(directory, digest, revision):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Invalid video result directory.')
    receipt = directory / 'receipt.json'; clip = directory / 'output.mp4'
    for path, limit in ((receipt, 16384), (clip, MAX_BYTES)):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError('Invalid video result file.')
    with receipt.open('rb') as stream:
        info = json.loads(stream.read(16385))
    with clip.open('rb') as stream:
        metadata = inspect_mp4(stream.read(MAX_BYTES + 1))
    if info != {'version': 1, 'revision': revision, 'inputSha256': digest, 'video': metadata}:
        raise ValueError('Video receipt does not match the request and decoded output.')
    return metadata


def make_resident(*, runtime, source, checkpoints, revision, home, idle_seconds, memory_bytes=48 * 1024**3, shutting_down=lambda: False):
    return ResidentRenderer(runtime=runtime, checkpoints=checkpoints, revision=revision, home=home, memory_bytes=memory_bytes,
                            idle_seconds=idle_seconds, module=RENDER_MODULE, extra_args=['--source', str(source)], env=ENV,
                            shutting_down=shutting_down)


def run_video_job(*, job_id, data, root, resident, inference_lock, revision, cancelled=lambda: False, lock_fd=None, timeout=1500):
    decode_request(data)
    if not re.fullmatch(r'[a-f0-9]{32}', job_id or '') or not revision or len(revision) > 128:
        raise ValueError('Invalid video job identity.')
    root, inference_lock = Path(root), Path(inference_lock)
    if not root.is_absolute() or not inference_lock.is_absolute():
        raise ValueError('Video worker paths must be absolute.')
    digest = hashlib.sha256(data).hexdigest()
    destination = root / job_id
    with (cpu_slot(root) if lock_fd is None else contextlib.nullcontext(lock_fd)):
        if cancelled():
            raise WorkerStopped('Video generation was cancelled.')
        recovered = destination.exists() or destination.is_symlink()
        if recovered:
            metadata = verify_result(destination, digest, revision)
        else:
            with gpu_lease(inference_lock), tempfile.TemporaryDirectory(prefix=f'.{job_id}-', dir=root) as temporary:
                stage = Path(temporary); accepted = stage / 'accepted'; accepted.mkdir()
                (stage / 'input.json').write_bytes(data)
                resident.render(input_path=stage / 'input.json', output_dir=accepted, timeout=timeout, cancelled=cancelled, job_label='video')
                metadata = verify_result(accepted, digest, revision)
                if cancelled():
                    raise WorkerStopped('Video generation was cancelled.')
                os.rename(accepted, destination)
        if cancelled():
            raise WorkerStopped('Video generation was cancelled.')
        return {**metadata, 'path': f'{job_id}/output.mp4', 'recovered': recovered}
