"""Immutability guard (design report §11.3).

A succeeded artifact is never overwritten. Retakes and edits always create a
new file (and a new job row). This module is the mechanical enforcement.
"""
from __future__ import annotations

import errno
import os
import uuid
from pathlib import Path

from app.core.errors import ErrorCode, StudioError


class ImmutableViolation(StudioError):
    def __init__(self, path: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "Refusing to overwrite an existing artifact; create a new file instead.",
            details={"path": Path(path).name},  # file name only, never the absolute path
        )


_NO_HARDLINK_ERRNOS = {errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP}


def write_new(absolute_path: Path, data: bytes) -> Path:
    """Write bytes to a path that must not exist yet; never overwrite.

    Data goes to a unique temp file in the target directory, then is published
    with ``os.link`` (FileExistsError if the target exists). On filesystems
    without hard links, falls back to an exclusive create.
    """
    if absolute_path.exists():
        raise ImmutableViolation(str(absolute_path))
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = absolute_path.parent / f".{absolute_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
        try:
            os.link(tmp, absolute_path)
        except FileExistsError:
            raise ImmutableViolation(str(absolute_path)) from None
        except OSError as exc:
            if exc.errno not in _NO_HARDLINK_ERRNOS:
                raise
            _write_exclusive(absolute_path, data)
    finally:
        tmp.unlink(missing_ok=True)
    return absolute_path


def _write_exclusive(absolute_path: Path, data: bytes) -> None:
    """No-hard-link fallback: exclusive create + write.

    The final file is created by THIS call (``xb``), so if writing/closing
    fails the partial file is ours and is removed before re-raising; a later
    call can then publish normally. A file that already existed (someone
    else's) is never touched: that case is ImmutableViolation before any
    write. The unlink is skipped if the path no longer refers to the inode we
    created.
    """
    try:
        fh = open(absolute_path, "xb")
    except FileExistsError:
        raise ImmutableViolation(str(absolute_path)) from None
    created = None
    try:
        created = os.fstat(fh.fileno())
        with fh:
            fh.write(data)
    except BaseException:
        try:
            fh.close()
        except Exception:  # noqa: BLE001 — the original error is what matters
            pass
        try:
            now = os.stat(absolute_path)
            if created is None or (now.st_dev, now.st_ino) == (created.st_dev, created.st_ino):
                absolute_path.unlink()
        except OSError:
            pass
        raise
