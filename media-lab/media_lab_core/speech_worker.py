"""Bounded development speech executor with verified atomic result publication."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from .cpu_worker import cpu_slot, run_owned_process, WorkerStopped
from .speech_request import decode_request
from .speech_artifact import inspect_wav, MAX_BYTES


def verify_result(directory, digest, revision):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Invalid speech result directory.')
    receipt = directory/'receipt.json'
    audio = directory/'output.wav'
    for path, limit in ((receipt, 16384), (audio, MAX_BYTES)):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError('Invalid speech result file.')
    with receipt.open('rb') as stream:
        info = json.loads(stream.read(16385))
    with audio.open('rb') as stream:
        metadata = inspect_wav(stream.read(MAX_BYTES + 1))
    if info != {'version':1, 'revision':revision, 'inputSha256':digest, 'audio':metadata}:
        raise ValueError('Speech receipt does not match the request and decoded output.')
    return metadata


def run_speech_job(*, job_id, data, root, runtime, models, watermark, voice, revision,
                   cancelled=lambda: False, lock_fd=None, memory_bytes=16*1024**3, timeout=180):
    decode_request(data)
    if not re.fullmatch(r'[a-f0-9]{32}', job_id or '') or not revision or len(revision) > 128:
        raise ValueError('Invalid speech job identity.')
    paths = [Path(p) for p in (root, runtime, models, watermark, voice)]
    if not all(path.is_absolute() for path in paths):
        raise ValueError('Speech worker paths must be absolute.')
    root, runtime, models, watermark, voice = paths
    digest = hashlib.sha256(data).hexdigest()
    destination = root/job_id
    with (cpu_slot(root) if lock_fd is None else contextlib.nullcontext(lock_fd)) as fd:
        if cancelled():
            raise WorkerStopped('Speech generation was cancelled.')
        recovered = destination.exists() or destination.is_symlink()
        if recovered:
            metadata = verify_result(destination, digest, revision)
        else:
            with tempfile.TemporaryDirectory(prefix=f'.{job_id}-', dir=root) as temporary:
                stage = Path(temporary); accepted = stage/'accepted'; accepted.mkdir()
                (stage/'input.json').write_bytes(data)
                env = {key:os.environ[key] for key in ('PATH','LANG','LC_ALL') if key in os.environ}
                env.update(HOME=str(stage), PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                           HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
                run_owned_process([str(runtime), '-m', 'media_lab_core.speech_render',
                    '--input',str(stage/'input.json'),'--output',str(accepted),
                    '--models',str(models),'--watermark',str(watermark),'--voice',str(voice),
                    '--revision',revision], cwd=stage, lock_fd=fd, memory_bytes=memory_bytes,
                    timeout=timeout, cancelled=cancelled, env=env, job_label='speech')
                metadata = verify_result(accepted, digest, revision)
                if cancelled():
                    raise WorkerStopped('Speech generation was cancelled.')
                os.rename(accepted,destination)
        if cancelled():
            raise WorkerStopped('Speech generation was cancelled.')
        return {**metadata,'path':f'{job_id}/output.wav','recovered':recovered}
