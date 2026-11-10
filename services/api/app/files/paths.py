"""Path resolution (design report §11.1).

The DB stores paths relative to PROJECTS_ROOT only (e.g.
"projects/<uuid>/frames/<uuid>_0.png"). Absolute paths are resolved at runtime.
Private directories (projects/, library/) are accessed only through here
(ARCHITECTURE §14 rule 2).
"""
from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings
from app.core.errors import ErrorCode, StudioError


def projects_root() -> Path:
    return get_settings().projects_root.resolve()


def to_relative(absolute: Path) -> str:
    """Convert an absolute path under PROJECTS_ROOT to a DB-safe relative path."""
    try:
        return str(absolute.resolve().relative_to(projects_root()))
    except ValueError:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR, "Path is outside PROJECTS_ROOT; refusing to store it."
        )


def resolve(relative_path: str) -> Path:
    """Convert a DB relative path back to an absolute path. Rejects escapes."""
    rel = Path(relative_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Invalid stored path.")
    root = projects_root()
    # Follow symlinks (strict=False: the target may not exist yet) and require
    # the real location to stay inside PROJECTS_ROOT.
    resolved = (root / rel).resolve()
    if not resolved.is_relative_to(root):
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Invalid stored path.")
    return resolved


def assert_relative(path: str) -> str:
    """Guard for anything about to be written to the DB."""
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Only relative paths may be stored in the DB.")
    return path


def project_dir(project_id: str) -> Path:
    return projects_root() / "projects" / str(project_id)


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
