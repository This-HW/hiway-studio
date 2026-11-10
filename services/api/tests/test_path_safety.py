"""B2 path-safety regression tests (fixed behavior).

1. LoRA weight file name never contains user input (trigger_word is data only).
2. A symlink inside PROJECTS_ROOT pointing outside is not served.
3. write_new never overwrites, even when a competitor wins the race, and
   leaves no temp files behind.
"""

from __future__ import annotations

import errno
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
from app.core.config import get_settings
from app.core.errors import ErrorCode, StudioError
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.files import immutability, paths
from app.models.character import Character
from app.models.lora import LoraModel
from app.services import file_serve
from app.services import lora as lora_service


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # Deep root so a ../ chain stays inside tmp_path and is detectable.
    root = tmp_path / "a" / "b" / "c" / "root"
    root.mkdir(parents=True)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("PROJECTS_ROOT", str(root))
    monkeypatch.setenv("SSE_BACKEND", "memory")
    get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setattr(sess_mod, "get_engine", lambda: engine)
    reset_engine()
    sess_mod._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    yield tmp_path, root.resolve()
    get_settings.cache_clear()
    reset_engine()


def test_lora_trigger_word_cannot_escape_projects_root(env):
    tmp_path, root = env
    trigger = "../../../../../../escaped"
    with session_scope() as db:
        ch = Character(
            id=uuid.uuid4(), project_id=None, name="H", appearance_prompt="x"
        )
        db.add(ch)
        db.flush()
        lora = LoraModel(character_id=ch.id, trigger_word=trigger, status="training")
        db.add(lora)
        db.flush()
        lora_id, char_id = lora.id, ch.id
        result = lora_service.complete_train(db, str(lora_id))
        assert result["status"] == "ready"
        model_path = result["model_path"]

    assert model_path == f"library/characters/{char_id}/lora/{lora_id}.safetensors"
    assert (root / model_path).is_file()
    assert list(tmp_path.rglob("*escaped*")) == []
    # Every weight file created under tmp_path lives inside the root.
    found = list(tmp_path.rglob("*.safetensors"))
    assert len(found) == 1
    assert root in found[0].resolve().parents


def test_symlink_pointing_outside_root_is_not_served(env):
    tmp_path, root = env
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (root / "projects").mkdir()
    (root / "projects" / "link.txt").symlink_to(outside)
    (root / "projects" / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    (root / "projects" / "ok.txt").write_text("fine")

    for rel in ("projects/link.txt", "projects/dirlink/secret.txt"):
        with pytest.raises(StudioError) as ei:
            file_serve.resolve_file(rel)
        assert ei.value.code == ErrorCode.NOT_FOUND
        with pytest.raises(StudioError) as ei2:
            paths.resolve(rel)
        assert ei2.value.code == ErrorCode.VALIDATION_ERROR

    assert file_serve.resolve_file("projects/ok.txt").read_text() == "fine"


def _no_tmp_leftovers(directory: Path) -> None:
    assert [p.name for p in directory.iterdir() if p.name.endswith(".tmp")] == []


def test_write_new_race_does_not_overwrite_competitor(tmp_path, monkeypatch):
    target = tmp_path / "out" / "a.bin"
    target.parent.mkdir()
    state = {"done": False}

    def competitor() -> None:
        if not state["done"]:
            state["done"] = True
            target.write_bytes(b"competitor")

    real_exists = Path.exists
    real_link = os.link

    def exists(self, *a, **k):
        result = real_exists(self, *a, **k)
        if self == target:
            competitor()  # appears right after the existence check
        return result

    def link(src, dst, *a, **k):
        if Path(dst) == target:
            competitor()  # appears right before publish
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(os, "link", link)

    with pytest.raises(immutability.ImmutableViolation):
        immutability.write_new(target, b"mine")
    assert target.read_bytes() == b"competitor"
    _no_tmp_leftovers(target.parent)


def test_write_new_second_write_raises_and_no_tmp(tmp_path):
    target = tmp_path / "x" / "b.bin"
    immutability.write_new(target, b"one")
    with pytest.raises(immutability.ImmutableViolation):
        immutability.write_new(target, b"two")
    assert target.read_bytes() == b"one"
    _no_tmp_leftovers(target.parent)


def test_write_new_without_hardlinks_still_never_overwrites(tmp_path, monkeypatch):
    def no_link(*a, **k):
        raise OSError(errno.EPERM, "hard links unsupported")

    monkeypatch.setattr(os, "link", no_link)
    target = tmp_path / "c.bin"
    immutability.write_new(target, b"one")
    assert target.read_bytes() == b"one"
    with pytest.raises(immutability.ImmutableViolation):
        immutability.write_new(target, b"two")
    assert target.read_bytes() == b"one"
    _no_tmp_leftovers(tmp_path)


def test_write_new_fallback_write_failure_leaves_no_partial_final(tmp_path, monkeypatch):
    # Review WA-M2: hard links unsupported (EPERM) and the exclusive-create
    # fallback fails mid-write (ENOSPC). The partial final file was created by
    # this call, so it is removed; a later normal call then succeeds.
    def no_link(*a, **k):
        raise OSError(errno.EPERM, "hard links unsupported")

    monkeypatch.setattr(os, "link", no_link)
    target = tmp_path / "d.bin"
    real_open = open
    state = {"fail": True}

    class _PartialWriter:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            self._fh.write(data[:3])
            self._fh.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

        def fileno(self):
            return self._fh.fileno()

        def close(self):
            self._fh.close()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def fake_open(file, mode="r", *a, **k):
        fh = real_open(file, mode, *a, **k)
        if Path(file) == target and state["fail"]:
            state["fail"] = False
            return _PartialWriter(fh)
        return fh

    monkeypatch.setattr(immutability, "open", fake_open, raising=False)
    with pytest.raises(OSError) as ei:
        immutability.write_new(target, b"complete-data")
    assert ei.value.errno == errno.ENOSPC
    assert not target.exists(), target.read_bytes()
    _no_tmp_leftovers(tmp_path)

    immutability.write_new(target, b"complete-data")
    assert target.read_bytes() == b"complete-data"
    _no_tmp_leftovers(tmp_path)


def test_write_new_fallback_never_deletes_competitor_file(tmp_path, monkeypatch):
    def no_link(*a, **k):
        raise OSError(errno.EPERM, "hard links unsupported")

    monkeypatch.setattr(os, "link", no_link)
    target = tmp_path / "e.bin"
    real_open = open

    def fake_open(file, mode="r", *a, **k):
        if Path(file) == target and "x" in mode and not target.exists():
            target.write_bytes(b"competitor")  # wins right before our create
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(immutability, "open", fake_open, raising=False)
    with pytest.raises(immutability.ImmutableViolation):
        immutability.write_new(target, b"mine")
    assert target.read_bytes() == b"competitor"
    _no_tmp_leftovers(tmp_path)


# ---------------------------------------------------------------------------
# Review WA-M3: seed / contract directory overrides fail fast when incomplete
# ---------------------------------------------------------------------------


def _write_json(path: Path, text: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_empty_seeds_override_fails_fast(tmp_path, monkeypatch):
    from app.core import contracts
    from app.models_lib import registry

    empty = tmp_path / "empty-seeds"
    empty.mkdir()
    monkeypatch.setenv(contracts.SEEDS_DIR_ENV, str(empty))
    for call in (contracts.seeds_dir, registry.seed_dir):
        with pytest.raises(FileNotFoundError) as ei:
            call()
        msg = str(ei.value)
        assert contracts.SEEDS_DIR_ENV in msg
        assert "models" in msg and "category_packs" in msg


def test_partial_seeds_override_fails_fast(tmp_path, monkeypatch):
    from app.core import contracts

    root = tmp_path / "seeds"
    _write_json(root / "models" / "m.json", '{"models": []}')
    (root / "category_packs").mkdir()  # present but without *.json
    monkeypatch.setenv(contracts.SEEDS_DIR_ENV, str(root))
    with pytest.raises(FileNotFoundError) as ei:
        contracts.seeds_dir()
    assert "category_packs" in str(ei.value)
    assert contracts.SEEDS_DIR_ENV in str(ei.value)

    _write_json(root / "category_packs" / "p.json")
    assert contracts.seeds_dir() == root


def test_empty_contracts_override_fails_fast(tmp_path, monkeypatch):
    from app.core import contracts

    empty = tmp_path / "empty-contracts"
    empty.mkdir()
    monkeypatch.setenv(contracts.CONTRACTS_DIR_ENV, str(empty))
    with pytest.raises(FileNotFoundError) as ei:
        contracts.contracts_dir()
    msg = str(ei.value)
    assert contracts.CONTRACTS_DIR_ENV in msg
    for name in ("enums.json", "errors.json", "sse_events.json"):
        assert name in msg

    for name in ("enums.json", "errors.json", "sse_events.json"):
        _write_json(empty / name)
    assert contracts.contracts_dir() == empty


def test_default_seed_and_contract_dirs_are_complete(monkeypatch):
    from app.core import contracts

    monkeypatch.delenv(contracts.SEEDS_DIR_ENV, raising=False)
    monkeypatch.delenv(contracts.CONTRACTS_DIR_ENV, raising=False)
    assert list((contracts.seeds_dir() / "models").glob("*.json"))
    assert (contracts.contracts_dir() / "enums.json").is_file()
