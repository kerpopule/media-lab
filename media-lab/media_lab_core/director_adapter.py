"""POSIX director callback with canonical flock ownership and explicit recovery.

The host supplies its existing canonical lock and a bounded probe that verifies
idle activity on the same exact runtime/model. No guessed lock paths, discovery,
service control or alternate providers. Hosts must poll recovery and finish it
before graceful shutdown; process death cannot preserve an OS flock.
"""
import os
from pathlib import Path
import stat
import threading
import time
from typing import Callable

from .director_transport import LocalDirectorTransport


class LocalDirectorAdapter:
    def __init__(self, *, transport: LocalDirectorTransport, lease_path: Path,
                 runtime_idle: Callable[[], bool]):
        if os.name != 'posix':
            raise RuntimeError('The local director lease is not qualified on this platform.')
        path = Path(lease_path)
        if not path.is_absolute() or path.resolve() != path or not path.parent.is_dir():
            raise ValueError('Use the existing canonical host lease directory.')
        if not isinstance(transport, LocalDirectorTransport) or not callable(runtime_idle):
            raise ValueError('Supply a local transport and an exact-runtime idle probe.')
        self.transport, self.lease_path, self.runtime_idle = transport, path, runtime_idle
        self._gate = threading.Lock()
        self._fd = None
        self._idle_since = None
        self._state = 'ready'

    @property
    def state(self) -> str:
        return self._state

    def _release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._idle_since = None
        self._state = 'ready'

    def _acquire(self):
        import fcntl
        fd = os.open(self.lease_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('The host lease must be a regular file owned by this user.')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def __call__(self, owner: str, messages: list[dict]) -> str:
        # Owner was authenticated by studio_director; never use it as a path.
        if not self._gate.acquire(blocking=False):
            raise RuntimeError('Sparky is busy.')
        try:
            if self._fd is not None:
                raise RuntimeError('Sparky is waiting for its previous runtime request to finish.')
            self._acquire()
            try:
                if self.runtime_idle() is not True:
                    raise RuntimeError('The selected local runtime is not confirmed idle.')
            except BaseException:
                self._release()  # No inference request has been submitted.
                raise
            self._state = 'running'
            try:
                answer = self.transport.exchange_under_lease(messages)
            except BaseException:
                self._state = 'draining'
                self._idle_since = None
                # Keep the descriptor open, even though the HTTP caller returns.
                raise
            self._release()
            return answer
        finally:
            self._gate.release()

    def poll_recovery(self) -> bool:
        """Release uncertain work only after two idle observations >=2s apart.

        Unknown/busy/probe exceptions reset recovery. The configured probe must
        validate both scheduler gauge families and exact runtime identity. This
        method never cancels a job, starts a model or forcibly releases a lock.
        """
        if not self._gate.acquire(blocking=False):
            return False
        try:
            if self._fd is None:
                return True
            try:
                idle = self.runtime_idle() is True
            except Exception:
                idle = False
            if not idle:
                self._idle_since = None
                return False
            now = time.monotonic()
            if self._idle_since is None:
                self._idle_since = now
                return False
            if now - self._idle_since < 2:
                return False
            self._release()
            return True
        finally:
            self._gate.release()
