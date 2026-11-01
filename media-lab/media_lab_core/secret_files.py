"""Files that hold a secret are born private (0600) and stay private.

The studio keeps its door codes, its signing secret, the local tool token, the
web-push key and the provider keys as plain files under the data root. A plain
``Path.write_text`` creates them with the process umask (0644 or 0664), so for a
moment -- or forever, when nothing chmods them afterwards -- anyone else on the
box can read them, and ``rsync -a`` carries that mode into every backup.

Everything here writes through a temporary file that is created 0600 from the
start and then renamed over the target, so no reader ever sees the secret in a
wider mode. Standard library only: the CLI and the runner timers import it
without a virtualenv.
"""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Callable, Iterable

PRIVATE_MODE = 0o600


def write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``; the file is 0600 from birth."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # unique per call: two threads writing the same file never share a temp file
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, PRIVATE_MODE)          # O_CREAT's mode is ignored for an old file
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure(path: Path, make: Callable[[], str]) -> bool:
    """Create ``path`` with ``make()`` when it is missing or empty.

    Never overwrites a file that already has content: an existing install keeps
    its codes until the owner rotates them. Returns True when it wrote.
    """
    path = Path(path)
    try:
        if path.is_file() and path.stat().st_size > 0:
            return False
    except OSError:
        pass
    write_private(path, make())
    return True


def tighten(paths: Iterable[Path]) -> list[Path]:
    """chmod 0600 every existing file in ``paths`` that group/other can reach.

    Returns the files it changed. A file owned by someone else (or on a
    filesystem without POSIX modes) is left as is rather than crashing startup.
    """
    changed = []
    for path in paths:
        path = Path(path)
        try:
            st = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or not (st.st_mode & 0o077):
            continue
        try:
            os.chmod(path, PRIVATE_MODE)
            changed.append(path)
        except OSError:
            continue
    return changed


def mode(path: Path) -> int | None:
    """The permission bits of ``path`` (e.g. 0o600), or None when it is absent."""
    try:
        return stat.S_IMODE(Path(path).stat().st_mode)
    except OSError:
        return None
