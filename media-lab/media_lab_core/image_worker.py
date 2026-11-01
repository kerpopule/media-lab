"""Bounded image executor: canonical GPU lease + owned (warm) renderer + verified atomic publication.

An edit request stages its condition images into the private render directory under the names fixed by
``image_request``. They are read through the queue's own owner-checked input reader, so a job can only ever
stage inputs its owner was granted, and every staged file is re-hashed against the digest the payload
declared before the renderer sees it.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from .cpu_worker import cpu_slot, WorkerStopped
from .music_worker import gpu_lease, ResidentRenderer
from .image_request import (MASK_NAME, SOURCE_NAME, decode_request, is_edit, reference_names)
from .image_artifact import inspect_png, MAX_BYTES

RENDER_MODULE = 'media_lab_core.image_render'
ENV = {'THP_MEM_ALLOC_ENABLE': '1'}


def stage_names(request):
    """The exact files an edit request requires, in staging order. Counts and flags only; no caller paths."""
    if not is_edit(request):
        return []
    names = [SOURCE_NAME] + reference_names(request['references'])
    return names + ([MASK_NAME] if request['mask'] else [])


def edit_sources(store, job_id, request):
    """Read the owner-granted condition images for one edit job and re-hash them against the payload.

    The job row carries the owner, so ``read_job_input`` is itself the ownership check. A declared digest or
    a declared count that does not match what is actually staged fails closed rather than rendering.
    """
    record = store.get(job_id) or {}
    settings = ((record.get('payload') or {}).get('settings') or {})
    names = stage_names(request)
    if names and not isinstance(settings.get('sourceId'), str):
        raise ValueError('The image edit is missing its source image.')
    declared = [settings.get('sourceId')]
    declared += [entry.get('inputId') for entry in (settings.get('references') or [])]
    declared += [settings['mask'].get('inputId')] if settings.get('mask') else []
    digests = [settings.get('sourceSha256')]
    digests += [entry.get('inputSha256') for entry in (settings.get('references') or [])]
    digests += [settings['mask'].get('inputSha256')] if settings.get('mask') else []
    if len(declared) != len(names):
        raise ValueError('The image edit inputs do not match the staged request.')
    staged = {}
    for name, input_id, digest in zip(names, declared, digests):
        data = store.read_job_input(job_id, input_id)
        if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('A staged image input changed.')
        staged[name] = data
    return staged


def write_stage(directory, data, request, staged):
    directory = Path(directory)
    (directory / 'input.json').write_bytes(data)
    for name in stage_names(request):
        (directory / name).write_bytes(staged[name])


def expected_receipt(digest, revision, request, image):
    if is_edit(request):
        return {'version': 2, 'revision': revision, 'inputSha256': digest, 'operation': request['operation'],
                'references': request['references'], 'mask': request['mask'], 'transparent': request['transparent'],
                'image': image}
    return {'version': 1, 'revision': revision, 'inputSha256': digest, 'image': image}


def verify_result(directory, digest, revision, request=None):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Invalid image result directory.')
    receipt = directory / 'receipt.json'; picture = directory / 'output.png'
    for path, limit in ((receipt, 16384), (picture, MAX_BYTES)):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError('Invalid image result file.')
    with receipt.open('rb') as stream:
        info = json.loads(stream.read(16385))
    with picture.open('rb') as stream:
        metadata = inspect_png(stream.read(MAX_BYTES + 1),
                               require_alpha=bool(request and is_edit(request) and request['transparent']))
    if info != expected_receipt(digest, revision, request, metadata):
        raise ValueError('Image receipt does not match the request and decoded output.')
    return metadata


def make_resident(*, runtime, checkpoints, revision, home, idle_seconds, memory_bytes=32 * 1024**3, shutting_down=lambda: False):
    return ResidentRenderer(runtime=runtime, checkpoints=checkpoints, revision=revision, home=home, memory_bytes=memory_bytes,
                            idle_seconds=idle_seconds, module=RENDER_MODULE, env=ENV, shutting_down=shutting_down)


def run_image_job(*, job_id, data, root, resident, inference_lock, revision, store=None, cancelled=lambda: False, lock_fd=None, timeout=600):
    request = decode_request(data)
    if not re.fullmatch(r'[a-f0-9]{32}', job_id or '') or not revision or len(revision) > 128:
        raise ValueError('Invalid image job identity.')
    root, inference_lock = Path(root), Path(inference_lock)
    if not root.is_absolute() or not inference_lock.is_absolute():
        raise ValueError('Image worker paths must be absolute.')
    digest = hashlib.sha256(data).hexdigest()
    destination = root / job_id
    with (cpu_slot(root) if lock_fd is None else contextlib.nullcontext(lock_fd)):
        if cancelled():
            raise WorkerStopped('Image generation was cancelled.')
        recovered = destination.exists() or destination.is_symlink()
        if recovered:
            metadata = verify_result(destination, digest, revision, request)
        else:
            staged = edit_sources(store, job_id, request) if is_edit(request) else {}
            with gpu_lease(inference_lock), tempfile.TemporaryDirectory(prefix=f'.{job_id}-', dir=root) as temporary:
                stage = Path(temporary); accepted = stage / 'accepted'; accepted.mkdir()
                write_stage(stage, data, request, staged)
                resident.render(input_path=stage / 'input.json', output_dir=accepted, timeout=timeout, cancelled=cancelled, job_label='image')
                metadata = verify_result(accepted, digest, revision, request)
                if cancelled():
                    raise WorkerStopped('Image generation was cancelled.')
                os.rename(accepted, destination)
        if cancelled():
            raise WorkerStopped('Image generation was cancelled.')
        return {**metadata, 'path': f'{job_id}/output.png', 'recovered': recovered}
