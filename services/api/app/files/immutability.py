"""Immutability guard (design report §11.3).

A succeeded artifact is never overwritten. Retakes and edits always create a
new file (and a new job row). This module is the mechanical enforcement.
"""
from __future__ import annotations

from pathlib import Path

from app.core.errors import ErrorCode, StudioError


class ImmutableViolation(StudioError):
    def __init__(self, path: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "Refusing to overwrite an existing artifact; create a new file instead.",
            details={"path": Path(path).name},  # file name only, never the absolute path
        )


def write_new(absolute_path: Path, data: bytes) -> Path:
    """Write bytes to a path that must not exist yet. Atomic-ish via tmp rename."""
    if absolute_path.exists():
        raise ImmutableViolation(str(absolute_path))
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = absolute_path.with_suffix(absolute_path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(absolute_path)
    return absolute_path
