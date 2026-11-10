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

CONTRACTS_DIR_ENV = "AI_STUDIO_CONTRACTS_DIR"
SEEDS_DIR_ENV = "AI_STUDIO_SEEDS_DIR"


def _repo_root() -> Path:
    # services/api/app/core/contracts.py -> repo root is parents[4]. Only
    # evaluated when the matching env override is not set.
    return Path(__file__).resolve().parents[4]


# Required content of each resolved directory: files that must exist, and
# subdirectories that must hold at least one *.json. A directory that exists
# but lacks these (e.g. a mis-mounted, empty override) fails fast instead of
# silently yielding an empty catalog (review WA-M3).
_CONTRACT_FILES = ("enums.json", "errors.json", "sse_events.json")
_SEED_JSON_SUBDIRS = ("models", "category_packs")


def _check_contents(path: Path, env_var: str, what: str, *,
                    files: tuple[str, ...] = (),
                    json_subdirs: tuple[str, ...] = ()) -> None:
    missing = [name for name in files if not (path / name).is_file()]
    missing += [
        f"{sub}/*.json"
        for sub in json_subdirs
        if not (path / sub).is_dir() or not any((path / sub).glob("*.json"))
    ]
    if missing:
        raise FileNotFoundError(
            f"{what} directory {path} is incomplete; missing: {', '.join(missing)}. "
            f"Set {env_var} to a complete {what} directory."
        )


def _resolve_dir(env_var: str, default_rel: tuple[str, ...], what: str) -> Path:
    override = os.environ.get(env_var)
    if override:
        path = Path(override)
    else:
        try:
            path = _repo_root().joinpath(*default_rel)
        except IndexError:
            raise FileNotFoundError(
                f"Cannot locate {what} relative to the source tree; set {env_var}."
            ) from None
    if not path.is_dir():
        raise FileNotFoundError(f"{what} directory not found: {path}. Set {env_var} to its location.")
    return path


def contracts_dir() -> Path:
    """packages/contracts (override: AI_STUDIO_CONTRACTS_DIR).

    Must contain enums.json, errors.json and sse_events.json."""
    path = _resolve_dir(CONTRACTS_DIR_ENV, ("packages", "contracts"), "contracts")
    _check_contents(path, CONTRACTS_DIR_ENV, "contracts", files=_CONTRACT_FILES)
    return path


def seeds_dir() -> Path:
    """Root of infra/seeds (override: AI_STUDIO_SEEDS_DIR).

    Must contain models/ and category_packs/, each with >= 1 *.json."""
    path = _resolve_dir(SEEDS_DIR_ENV, ("infra", "seeds"), "seeds")
    _check_contents(path, SEEDS_DIR_ENV, "seeds", json_subdirs=_SEED_JSON_SUBDIRS)
    return path


@functools.lru_cache(maxsize=1)
def load_enums() -> dict[str, list[str]]:
    with open(contracts_dir() / "enums.json", encoding="utf-8") as f:
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
