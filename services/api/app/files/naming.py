"""File naming (design report §11.2).

Human-readable names live in DB columns (title/name); file names are opaque
`{uuid}[_{batch}].{ext}` to avoid collisions and encoding problems.
"""
from __future__ import annotations


def frame_filename(generation_id: str, batch_index: int, ext: str) -> str:
    ext = ext.lstrip(".").lower()
    return f"{generation_id}_{batch_index}.{ext}"


def single_filename(object_id: str, ext: str) -> str:
    ext = ext.lstrip(".").lower()
    return f"{object_id}.{ext}"
