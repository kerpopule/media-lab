"""Bounded POSIX CPU worker; never owns or interrupts the GPU queue."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import psutil
from PIL import Image, ImageOps

from .birefnet_cpu import MANIFEST


class WorkerBusy(RuntimeError):
    pass


class WorkerStopped(RuntimeError):
    pass


@contextlib.contextmanager
def cpu_slot(root: Path):
    if os.name != 'posix':
        raise RuntimeError('This CPU worker host is not yet qualified on Windows.')
    import fcntl
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / '.background-cpu.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkerBusy('Another background-removal job is still running.') from None
        yield fd
    finally:
        # Do not issue LOCK_UN: an orphaned child inherits this same open file
        # description and must retain the lock until its actual process exits.
        os.close(fd)


def run_owned_process(command: list[str], *, cwd: Path, lock_fd: int, memory_bytes: int,
                      timeout: float, cancelled=lambda: False, env=None, job_label='background-removal') -> dict:
    if memory_bytes <= 0 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Positive memory and time budgets are required.')
    if psutil.virtual_memory().available < memory_bytes:
        raise WorkerBusy('There is not enough available memory for this model. Try again later or use another host.')
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, shell=False, pass_fds=(lock_fd,),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    owner = psutil.Process(process.pid)
    tracked = {owner.pid: owner}
    tail = bytearray()

    os.set_blocking(process.stdout.fileno(), False)

    def drain():
        # Limit each pass so a noisy child cannot starve budget checks.
        for _ in range(32):
            try:
                chunk = os.read(process.stdout.fileno(), 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            tail.extend(chunk)
            del tail[:-65536]
    peak = 0
    reason = None
    try:
        while process.poll() is None:
            drain()
            if cancelled():
                reason = f'The {job_label} job was cancelled.'
                break
            if time.monotonic() - started > timeout:
                reason = f'The {job_label} job exceeded its time budget.'
                break
            try:
                for child in owner.children(recursive=True):
                    tracked[child.pid] = child
                rss = 0
                for member in tracked.values():
                    try:
                        rss += member.memory_info().rss if member.is_running() else 0
                    except psutil.NoSuchProcess:
                        pass
                peak = max(peak, rss)
                if rss > memory_bytes:
                    reason = f'The {job_label} job exceeded its memory budget.'
                    break
            except psutil.NoSuchProcess:
                pass
            time.sleep(0.1)
    finally:
        # Only processes descended from our exact child are ever stopped.
        # psutil's Process handles guard against PID reuse for terminate/kill.
        alive = []
        for member in reversed(list(tracked.values())):
            try:
                if member.is_running() and member.status() != psutil.STATUS_ZOMBIE:
                    member.terminate()
                    alive.append(member)
            except psutil.NoSuchProcess:
                pass
        _, remaining = psutil.wait_procs(alive, timeout=5)
        for member in remaining:
            try:
                member.kill()
            except psutil.NoSuchProcess:
                pass
        process.wait()
        drain()
        process.stdout.close()
    if reason:
        raise WorkerStopped(reason)
    if process.returncode:
        raise WorkerStopped('The isolated model worker failed before producing a verified result.')
    return {'seconds': time.monotonic() - started, 'peak_rss_bytes': peak,
            'log_tail': tail.decode('utf-8', errors='replace')}


def verify_result(directory: Path, input_hash: str, size: tuple[int, int]) -> dict:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('The worker result directory is invalid.')
    manifest = json.loads(MANIFEST.read_text())
    receipt_path = directory / 'receipt.json'
    if receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.stat().st_size > 16384:
        raise ValueError('The worker receipt is invalid.')
    receipt = json.loads(receipt_path.read_text())
    if not isinstance(receipt, dict):
        raise ValueError('The worker receipt is invalid.')
    output = directory / 'output.png'
    if output.is_symlink() or not output.is_file() or output.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('The worker output is not an accepted PNG file.')
    data = output.read_bytes()
    if (receipt.get('engine'), receipt.get('revision'), receipt.get('device'), receipt.get('dtype')) != (
            'birefnet-cpu', manifest['revision'], 'cpu', 'float32'):
        raise ValueError('The worker result does not match the requested model.')
    if receipt.get('input_sha256') != input_hash or receipt.get('output_sha256') != hashlib.sha256(data).hexdigest():
        raise ValueError('The worker result failed input/output integrity checks.')
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'PNG' or image.mode != 'RGBA' or image.size != size:
            raise ValueError('The worker output has incorrect dimensions or pixel format.')
        image.load()  # independent decode before publication
    return receipt


def run_background_job(*, job_id: str, data: bytes, root: Path, runtime: Path,
                       package: Path, cache: Path, cancelled=lambda: False,
                       memory_bytes=12 * 1024**3, timeout=600, lock_fd=None) -> dict:
    """Run one job; a queue controller may supply its already-held cpu_slot fd.

    The controller must retain that slot across claim, execution and terminal
    state publication. This internal parameter is never supplied by API input.
    """
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('A server-issued job identity is required.')
    if not data or len(data) > 20 * 1024 * 1024:
        raise ValueError('Choose an image no larger than 20 MiB.')
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in ('PNG', 'JPEG', 'WEBP') or getattr(image, 'n_frames', 1) != 1 or image.width * image.height > 16_000_000:
            raise ValueError('Choose a single supported image of at most 16 megapixels.')
        size = ImageOps.exif_transpose(image).size
    digest = hashlib.sha256(data).hexdigest()
    root = Path(root).resolve()
    runtime, package, cache = (Path(value).absolute() for value in (runtime, package, cache))
    destination = root / job_id
    with (cpu_slot(root) if lock_fd is None else contextlib.nullcontext(lock_fd)) as fd:
        if cancelled():
            raise WorkerStopped('The background-removal job was cancelled.')
        if destination.exists() or destination.is_symlink():
            verify_result(destination, digest, size)
            return {'path': f'{job_id}/output.png', 'bytes': (destination/'output.png').stat().st_size,
                    'sha256': hashlib.sha256((destination/'output.png').read_bytes()).hexdigest(), 'recovered': True}
        with tempfile.TemporaryDirectory(prefix=f'.{job_id}-', dir=root) as temporary:
            stage = Path(temporary)
            (stage/'input.bin').write_bytes(data)
            accepted = stage/'accepted'
            accepted.mkdir()
            # No API credentials or arbitrary PYTHONPATH enter the model process.
            env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
            run_owned_process([str(runtime), '-m', 'media_lab_core.birefnet_cpu', '--package', str(package),
                               '--cache', str(cache), '--input', str(stage/'input.bin'), '--output', str(accepted)],
                              cwd=stage, lock_fd=fd, memory_bytes=memory_bytes, timeout=timeout,
                              cancelled=cancelled, env=env)
            verify_result(accepted, digest, size)
            if cancelled():
                raise WorkerStopped('The background-removal job was cancelled.')
            # All publishers use the held canonical lock. This directory is
            # immutable after the rename; existing results were handled above.
            os.rename(accepted, destination)
        output = destination/'output.png'
        return {'path': f'{job_id}/output.png', 'bytes': output.stat().st_size,
                'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'recovered': False}
