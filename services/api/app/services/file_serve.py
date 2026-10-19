"""Static file serving (design §5: local-only, no CDN).

DB stores paths relative to PROJECTS_ROOT; this resolves them per request so
a changed PROJECTS_ROOT (tests) is honored. Path traversal is rejected.
"""
from __future__ import annotations

from pathlib import Path

from app.core.errors import ErrorCode, StudioError
from app.files import paths


def resolve_file(rel_path: str) -> Path:
    """Return the absolute path of a project-root-relative file.

    Raises NOT_FOUND for traversal attempts, absolute paths, and missing files.
    """
    try:
        abs_path = paths.resolve(rel_path.strip())
    except StudioError:
        raise StudioError(ErrorCode.NOT_FOUND, "File not found.")
    if not abs_path.is_file():
        raise StudioError(ErrorCode.NOT_FOUND, "File not found.")
    return abs_path
