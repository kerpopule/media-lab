"""Bounded candidate 3D executor. Host qualification/registration remain separate."""
import contextlib
import hashlib
import os
from pathlib import Path
import re
import tempfile

from .cpu_worker import cpu_slot, run_owned_process, WorkerStopped
from .triposr_result import verify_triposr_result
from .triposr_compatibility import expected_runtime_receipt


def run_triposr_job(*, job_id, data, root, runtime, package, cancelled=lambda: False,
                    memory_bytes=12 * 1024**3, timeout=600, lock_fd=None, runtime_profile='original'):
    expected_runtime_receipt(profile=runtime_profile)  # Refuse unknown profiles before writes or execution.
    if not isinstance(job_id, str) or not re.fullmatch('[a-f0-9]{32}', job_id):
        raise ValueError('A server-issued job identity is required.')
    if not isinstance(data, bytes) or not 0 < len(data) <= 20 * 1024**2:
        raise ValueError('Choose a cutout no larger than 20 MiB.')
    digest = hashlib.sha256(data).hexdigest()
    root = Path(root).resolve()
    runtime, package = Path(runtime).absolute(), Path(package).absolute()
    destination = root / job_id
    with (cpu_slot(root) if lock_fd is None else contextlib.nullcontext(lock_fd)) as fd:
        if cancelled():
            raise WorkerStopped('The 3D job was cancelled.')
        if destination.exists() or destination.is_symlink():
            receipt, blob = verify_triposr_result(destination, digest, runtime_profile=runtime_profile)
            return {'path': f'{job_id}/output.glb', 'bytes': len(blob),
                    'sha256': receipt['output_sha256'], 'recovered': True}
        with tempfile.TemporaryDirectory(prefix=f'.{job_id}-', dir=root) as temporary:
            stage = Path(temporary)
            (stage / 'input.png').write_bytes(data)
            accepted = stage / 'accepted'
            env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
            run_owned_process([str(runtime), '-m', 'media_lab_core.triposr_cpu',
                               '--runtime-profile', runtime_profile, '--package', str(package), '--input', str(stage / 'input.png'),
                               '--output', str(accepted)], cwd=stage, lock_fd=fd,
                              memory_bytes=memory_bytes, timeout=timeout,
                              cancelled=cancelled, env=env, job_label='3D reconstruction')
            receipt, blob = verify_triposr_result(accepted, digest, runtime_profile=runtime_profile)
            if cancelled():
                raise WorkerStopped('The 3D job was cancelled.')
            # All publishers retain the same canonical CPU lock. Never overwrite
            # an existing result; recovery above verifies it before reuse.
            if destination.exists() or destination.is_symlink():
                raise ValueError('The 3D result destination already exists.')
            os.rename(accepted, destination)
        return {'path': f'{job_id}/output.glb', 'bytes': len(blob),
                'sha256': receipt['output_sha256'], 'recovered': False}
