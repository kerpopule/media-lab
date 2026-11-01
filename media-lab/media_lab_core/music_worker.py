"""Bounded music executor: canonical GPU lease + owned process + verified atomic publication."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
import psutil
from .cpu_worker import cpu_slot, run_owned_process, WorkerBusy, WorkerStopped
from .music_request import decode_request
from .music_artifact import inspect_wav, MAX_BYTES


def verify_result(directory, digest, revision):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Invalid music result directory.')
    receipt = directory / 'receipt.json'; audio = directory / 'output.wav'
    for path, limit in ((receipt, 16384), (audio, MAX_BYTES)):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError('Invalid music result file.')
    with receipt.open('rb') as stream:
        info = json.loads(stream.read(16385))
    with audio.open('rb') as stream:
        metadata = inspect_wav(stream.read(MAX_BYTES + 1))
    if info != {'version': 1, 'revision': revision, 'inputSha256': digest, 'audio': metadata}:
        raise ValueError('Music receipt does not match the request and decoded output.')
    return metadata


@contextlib.contextmanager
def gpu_lease(path):
    """The canonical inference lock every GPU consumer on the host shares; never wait on it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkerBusy('The GPU is busy with another engine. The job stays queued.') from None
        yield fd
    finally:
        os.close(fd)


LOAD_RETRY_SECONDS = 60
LOAD_FAILURE_LIMIT = 3
RECLAIM_IDLE_SECONDS = 15   # another pack's warm model may be dropped for a cold start once it has sat idle this long
RESIDENTS = set()           # every live ResidentRenderer in this process, so packs can make room for each other


def reclaim_idle_residents(exclude):
    """Drop other packs' idle warm models so a cold start can fit. Only residents not rendering right now and
    idle for at least RECLAIM_IDLE_SECONDS are touched; the busy one is never interrupted. Returns how many."""
    reclaimed = 0
    for other in list(RESIDENTS):
        if other is exclude or not other.alive() or other.last_used is None:
            continue
        if time.monotonic() - other.last_used < RECLAIM_IDLE_SECONDS or not other.lock.acquire(blocking=False):
            continue
        try:
            other._terminate(); reclaimed += 1
        finally:
            other.lock.release()
    return reclaimed


class ResidentRenderer:
    """Keeps one renderer process warm between jobs so the model loads once, not per job.

    The process is owned exactly like a per-job one: the same memory and time budgets are
    enforced while it renders, cancellation or any breach terminates it (dropping the
    model), and it is reaped after `idle_seconds` without work so VRAM is not held
    indefinitely. The canonical GPU lease is still taken per render by the caller; between
    renders the warm model occupies memory without the lease, which is why idle reaping
    exists and why the operator sizes `idle_seconds`."""

    def __init__(self, *, runtime, checkpoints, revision, home, memory_bytes=40 * 1024**3, idle_seconds=600,
                 module='media_lab_core.music_render', extra_args=(), env=None, working_margin=None,
                 shutting_down=lambda: False):
        self.shutting_down = shutting_down  # host stop in progress: a renderer that dies mid-job must not fail the job
        if working_margin is None:
            working_margin = min(8 * 1024**3, memory_bytes)
        if idle_seconds < 0 or memory_bytes <= 0 or not 0 < working_margin <= memory_bytes:
            raise ValueError('Positive resident budgets are required.')
        self.working_margin = working_margin  # A live renderer already holds its model; a render only needs headroom.
        self.runtime, self.checkpoints, self.revision = Path(runtime), Path(checkpoints), revision
        self.home = Path(home); self.memory_bytes = memory_bytes; self.idle_seconds = idle_seconds
        self.module, self.extra_args, self.env = module, list(extra_args), dict(env or {})
        self.process = None; self.tracked = {}; self.last_used = None; self.lock = threading.Lock()
        # Set outside the render lock so a supervisor stop never waits for an in-flight render to finish.
        self.stopping = threading.Event()
        self.loads = 0; self.renders = 0
        self.load_failures = 0; self.load_failed_at = None  # backoff after a runtime that could not load its model
        RESIDENTS.add(self)

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def _spawn(self):
        self.home.mkdir(mode=0o700, exist_ok=True)
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
        env.update(HOME=str(self.home), PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                   HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', **self.env)
        log = (self.home / 'resident.log').open('wb')  # Truncated per process; never grows past one lifetime.
        self.process = subprocess.Popen([str(self.runtime), '-m', self.module, '--serve', *self.extra_args,
                                         '--checkpoints', str(self.checkpoints), '--revision', self.revision],
                                        cwd=self.home, env=env, shell=False, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=log, close_fds=True)
        log.close()
        self.tracked = {self.process.pid: psutil.Process(self.process.pid)}
        os.set_blocking(self.process.stdout.fileno(), False)
        self.loads += 1

    def _rss(self):
        try:
            for child in self.tracked[self.process.pid].children(recursive=True):
                self.tracked[child.pid] = child
        except psutil.NoSuchProcess:
            pass
        total = 0
        for member in list(self.tracked.values()):
            try:
                total += member.memory_info().rss if member.is_running() else 0
            except psutil.NoSuchProcess:
                pass
        return total

    def terminate(self):
        """Stop the runtime now, even mid-render.

        A render holds the lock for its whole duration, so this must not wait for it: the tracked
        processes are signalled first (only our exact child and its descendants, with psutil handles
        guarding against PID reuse), which unblocks the render, and the lock is taken afterwards only
        to clear state.
        """
        self.stopping.set()
        try:
            members = list(self.tracked.values())
            for member in reversed(members):
                try:
                    if member.is_running() and member.status() != psutil.STATUS_ZOMBIE:
                        member.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(members, timeout=5)
            for member in alive:
                try:
                    member.kill()
                except psutil.NoSuchProcess:
                    pass
            with self.lock:
                self._terminate()
        finally:
            self.stopping.clear()

    def _terminate(self):
        members = list(self.tracked.values()); self.tracked = {}
        for member in reversed(members):
            try:
                if member.is_running() and member.status() != psutil.STATUS_ZOMBIE:
                    member.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(members, timeout=5)
        for member in alive:
            try:
                member.kill()
            except psutil.NoSuchProcess:
                pass
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            self.process.wait(timeout=5)
        self.process = None; self.last_used = None

    def reap_idle(self, now=None):
        """Drops the warm model after idle_seconds without a render; returns True when it did."""
        with self.lock:
            if not self.alive() or self.last_used is None:
                return False
            if (now if now is not None else time.monotonic()) - self.last_used < self.idle_seconds:
                return False
            self._terminate()
            return True

    def render(self, *, input_path, output_dir, timeout, cancelled=lambda: False, job_label='music'):
        if not math_finite(timeout) or timeout <= 0:
            raise ValueError('A positive time budget is required.')
        if self.stopping.is_set() or self.shutting_down():
            raise WorkerBusy('The renderer is shutting down. The job stays queued.')
        if self.load_failed_at is not None and time.monotonic() - self.load_failed_at < LOAD_RETRY_SECONDS:
            raise WorkerBusy('The model could not be loaded a moment ago; waiting before trying again. The job stays queued.')
        with self.lock:
            # A cold start must fit the whole model; a warm renderer already owns its memory (unified-memory
            # hosts do not report it as process RSS), so only the working margin has to be free.
            needed = self.working_margin if self.alive() else self.memory_bytes
            if psutil.virtual_memory().available < needed:
                # Before giving up, make room: another pack's idle warm model is cheaper to reload later than a
                # job that bounces between queue and worker for minutes (Spark: a 20 s song waited 4 minutes
                # behind an idle image model).
                if not self.alive() and reclaim_idle_residents(self):
                    time.sleep(1.0)
                if psutil.virtual_memory().available < needed:
                    raise WorkerBusy('There is not enough available memory for this model. Try again later or use another host.')
            if not self.alive():
                self._spawn()
            started = time.monotonic(); buffer = bytearray(); reason = None
            try:
                self.process.stdin.write((json.dumps({'input': str(input_path), 'output': str(output_dir)}) + '\n').encode())
                self.process.stdin.flush()
            except (OSError, ValueError):
                self._terminate()
                raise RuntimeError('The resident music renderer is unavailable.') from None
            while True:
                try:
                    chunk = os.read(self.process.stdout.fileno(), 4096)
                except BlockingIOError:
                    chunk = b''
                if chunk:
                    buffer.extend(chunk); del buffer[:-16384]
                    if b'\n' in buffer:
                        break
                code = self.process.poll()
                if code is not None:
                    # SIGTERM/SIGINT means the platform is stopping this service: systemd signals the whole
                    # control group, so the renderer can die before the host sets its own stop flag. Either
                    # signal is a shutdown, never a job failure. Our own budget breaches never reach here
                    # because they set a reason and terminate the process themselves.
                    if self.stopping.is_set() or self.shutting_down() or code in (-signal.SIGTERM, -signal.SIGINT):
                        reason = 'shutdown'; break
                    reason = f'The {job_label} renderer exited unexpectedly.'; break
                if cancelled():
                    reason = f'The {job_label} job was cancelled.'; break
                if time.monotonic() - started > timeout:
                    reason = f'The {job_label} job exceeded its time budget.'; break
                if self._rss() > self.memory_bytes:
                    reason = f'The {job_label} job exceeded its memory budget.'; break
                time.sleep(0.1)
            if reason is not None:
                self._terminate()
                if reason == 'shutdown':
                    # The service is stopping (systemd signals the whole group); the job goes back to the queue
                    # untouched and a later start picks it up.
                    raise WorkerBusy('The renderer is shutting down. The job stays queued.')
                raise WorkerStopped(reason) if 'cancelled' in reason else RuntimeError(reason)
            self.last_used = time.monotonic(); self.renders += 1
            try:
                reply = json.loads(bytes(buffer).split(b'\n')[0])
            except ValueError:
                self._terminate()
                raise RuntimeError('The resident music renderer replied unreadably.') from None
            if reply != {'ok': True}:
                if isinstance(reply, dict) and reply.get('stage') == 'load':
                    # The runtime never got its model up (typically memory on a shared GPU host). Drop the
                    # half-loaded process, back off, and hand the job back to the queue; give up only after
                    # repeated failures so a model that never fits does not spin the worker forever.
                    self._terminate()
                    self.load_failures += 1; self.load_failed_at = time.monotonic()
                    if self.load_failures >= LOAD_FAILURE_LIMIT:
                        self.load_failures = 0
                        raise RuntimeError(f'The {job_label} model could not be loaded after repeated attempts.')
                    raise WorkerBusy(f'The {job_label} model could not be loaded right now. The job stays queued.')
                raise RuntimeError(f'The {job_label} renderer could not finish this piece.')
            self.load_failures = 0; self.load_failed_at = None
            return {'seconds': round(time.monotonic() - started, 2), 'warm': self.renders > 1 or self.loads > 1}


def math_finite(value):
    return isinstance(value, (int, float)) and value == value and value not in (float('inf'), float('-inf'))


def run_music_job(*, job_id, data, root, runtime, checkpoints, inference_lock, revision,
                  cancelled=lambda: False, lock_fd=None, memory_bytes=40 * 1024**3, timeout=900, resident=None):
    decode_request(data)
    if not re.fullmatch(r'[a-f0-9]{32}', job_id or '') or not revision or len(revision) > 128:
        raise ValueError('Invalid music job identity.')
    paths = [Path(p) for p in (root, runtime, checkpoints, inference_lock)]
    if not all(path.is_absolute() for path in paths):
        raise ValueError('Music worker paths must be absolute.')
    root, runtime, checkpoints, inference_lock = paths
    digest = hashlib.sha256(data).hexdigest()
    destination = root / job_id
    with (cpu_slot(root) if lock_fd is None else contextlib.nullcontext(lock_fd)) as fd:
        if cancelled():
            raise WorkerStopped('Music generation was cancelled.')
        recovered = destination.exists() or destination.is_symlink()
        if recovered:
            metadata = verify_result(destination, digest, revision)
        else:
            with gpu_lease(inference_lock), tempfile.TemporaryDirectory(prefix=f'.{job_id}-', dir=root) as temporary:
                stage = Path(temporary); accepted = stage / 'accepted'; accepted.mkdir()
                (stage / 'input.json').write_bytes(data)
                env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
                env.update(HOME=str(stage), PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                           HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
                if resident is not None:
                    resident.render(input_path=stage / 'input.json', output_dir=accepted, timeout=timeout, cancelled=cancelled)
                else:
                    run_owned_process([str(runtime), '-m', 'media_lab_core.music_render', '--input', str(stage / 'input.json'),
                                       '--output', str(accepted), '--checkpoints', str(checkpoints), '--revision', revision],
                                      cwd=stage, lock_fd=fd, memory_bytes=memory_bytes, timeout=timeout, cancelled=cancelled,
                                      env=env, job_label='music')
                metadata = verify_result(accepted, digest, revision)
                if cancelled():
                    raise WorkerStopped('Music generation was cancelled.')
                os.rename(accepted, destination)
        if cancelled():
            raise WorkerStopped('Music generation was cancelled.')
        return {**metadata, 'path': f'{job_id}/output.wav', 'recovered': recovered}
