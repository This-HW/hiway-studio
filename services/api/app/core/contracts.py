"""Contract enum loader (core-owned: no app imports, disk read only).

Canonical enum vocabularies live in packages/contracts/enums.json. Services use
this helper to validate enum-valued fields at runtime instead of freezing
the list in a schema Literal.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path

_CONTRACTS_DIR = Path(
    os.environ.get("AI_STUDIO_CONTRACTS_DIR", Path(__file__).resolve().parents[4] / "packages" / "contracts")
)


@functools.lru_cache(maxsize=1)
def load_enums() -> dict[str, list[str]]:
    with open(_CONTRACTS_DIR / "enums.json", encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if isinstance(v, list)}


def enum_values(name: str) -> list[str]:
    return load_enums().get(name, [])


def check_enum(name: str, value: str | None) -> str | None:
    """Return value if it is a member of the named enum; raise otherwise."""
    from app.core.errors import ErrorCode, StudioError

    if value is None:
        return None
    allowed = enum_values(name)
    if value not in allowed:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Invalid {name}: {value}.",
            details={"allowed": allowed},
        )
    return value
