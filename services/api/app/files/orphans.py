"""Orphan file detection (design report §11.3).

An orphan is a file under PROJECTS_ROOT with no referencing DB row.
Detection only — automatic deletion is forbidden (ARCHITECTURE §4.8).
The weekly schedule is wired in a later phase; this module provides the scan.
"""
from __future__ import annotations

from pathlib import Path

from app.files.paths import projects_root


def candidate_files() -> list[Path]:
    root = projects_root()
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".tmp")]


def find_orphans(known_relative_paths: set[str]) -> list[str]:
    """Return relative paths of files not present in known_relative_paths."""
    orphans: list[str] = []
    root = projects_root()
    for path in candidate_files():
        rel = str(path.relative_to(root))
        if rel not in known_relative_paths:
            orphans.append(rel)
    return sorted(orphans)
