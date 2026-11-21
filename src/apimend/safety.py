"""Keep apimend inside the folder you chose.

apimend only reads and changes ordinary files inside the selected folder. A symbolic
link could point anywhere on the computer, so links are never followed: linked files
and folders are reported, not read and never changed. Before any file is written the
checks run again, and the write itself refuses to follow a link, in case the file was
replaced while apimend was working.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

LINKED_FILE = "symbolic link (not followed, for safety)"
LINKED_FOLDER = "symbolic link to a folder (not followed, for safety)"
OUTSIDE = "outside the selected folder (not followed, for safety)"
NOT_REGULAR = "not a regular file"
HARD_LINKED = "has other hard links, so changing it could change another file"

# Never follow a link when opening a file for writing (not available on Windows,
# where the checks before the write still apply).
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class UnsafePathError(Exception):
    """Raised instead of reading or writing a file outside the selected folder."""


def _link_on_the_way(root: Path, file: Path) -> bool:
    """True if `file`, or any folder between `root` and it, is a symbolic link."""
    try:
        relative = file.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def unsafe_reason(root: str | Path, file: str | Path, *, for_writing: bool = False) -> str | None:
    """Why apimend must not use `file`, or None if it is safe.

    `file` must be a path under `root` as found by walking `root`.
    """
    root, file = Path(root), Path(file)
    if _link_on_the_way(root, file):
        return LINKED_FILE
    try:
        real = file.resolve(strict=True)
        info = file.lstat()
    except OSError:
        return NOT_REGULAR
    if not real.is_relative_to(root.resolve()):
        return OUTSIDE
    if not stat.S_ISREG(info.st_mode):
        return NOT_REGULAR
    if for_writing and info.st_nlink > 1:
        return HARD_LINKED
    return None


def check_writable(root: str | Path, file: str | Path) -> None:
    """Raise UnsafePathError unless `file` is an ordinary file inside `root`."""
    reason = unsafe_reason(root, file, for_writing=True)
    if reason:
        raise UnsafePathError(f"refusing to change {file}: {reason}")


def write_file(root: str | Path, file: str | Path, data: bytes) -> None:
    """Replace the contents of `file`, checking again first and never following a link."""
    check_writable(root, file)
    try:
        descriptor = os.open(file, os.O_WRONLY | os.O_TRUNC | _NOFOLLOW | _BINARY)
    except OSError as exc:
        raise UnsafePathError(f"refusing to change {file}: {exc.strerror}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
