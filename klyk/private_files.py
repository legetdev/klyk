"""Owner-only local files for screenshots, diagnostics, and control state."""

import os
import stat
from pathlib import Path


def private_directory(path: Path) -> None:
    """Create or tighten a private directory without following a final symlink."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise PermissionError("Klyk's private directory belongs to another user")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def open_private(path, mode="a", *, encoding="utf-8"):
    """Open a regular, single-link owned file; restrict access before writing."""
    if mode not in ("a", "a+", "wb"):
        raise ValueError("Unsupported private file mode")
    flags = os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= os.O_RDWR if mode == "a+" else os.O_WRONLY
    if mode.startswith("a"):
        flags |= os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise PermissionError("Klyk requires a regular, single-link file owned by this user")
        os.fchmod(fd, 0o600)
        if mode == "wb":
            os.ftruncate(fd, 0)
        stream = os.fdopen(fd, mode, encoding=None if "b" in mode else encoding)
    except BaseException:
        os.close(fd)
        raise
    return stream
