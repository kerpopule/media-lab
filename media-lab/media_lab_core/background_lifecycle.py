"""Host/install lifetime exclusion, separate from individual CPU jobs."""
import contextlib
import os
from pathlib import Path


@contextlib.contextmanager
def lifecycle_slot(root):
    if os.name != 'posix':
        raise RuntimeError('Background runtime maintenance is not yet supported on this platform.')
    import fcntl
    root = Path(root).absolute()
    if root.resolve() != root:
        raise ValueError('Use a canonical artifact path without symbolic links.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root/'.background-lifecycle.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('This background runtime is in use. Stop its host before maintenance.') from None
        yield fd
    finally:
        os.close(fd)
