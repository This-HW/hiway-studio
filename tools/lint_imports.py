#!/usr/bin/env python3
"""Enforce module dependency direction for services/api/app.

Scans Python sources with the AST (stdlib only) and checks that each
top-level module only depends on the modules listed in ALLOWED.
The alembic/ directory is excluded. Violations or dependency cycles
are reported and cause exit 1; on success prints "OK" and exits 0.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "services" / "api" / "app"

# Allowed import targets per top-level module (directed acyclic by design).
# NOTE: tasks may import db/models because the worker must read its own
# generation_id row before delegating to services.
ALLOWED = {
    "core": set(),
    "db": {"core"},
    "models": {"db", "core"},
    "schemas": {"core"},
    "models_lib": {"db", "models", "core"},
    "files": {"core"},
    "media": {"files", "core"},
    "services": {"models", "models_lib", "files", "media", "core", "db", "schemas"},
    "tasks": {"services", "models_lib", "media", "files", "core", "db", "models"},
    # routers may import db.session.get_db only (FastAPI Depends wiring);
    # domain access still goes through services.
    "routers": {"services", "schemas", "core", "db"},
}

EXCLUDED_DIRS = {"alembic"}


def module_path(file: Path) -> list[str]:
    """Dotted module path of a file relative to APP_ROOT, e.g. tasks/image.py -> ['tasks', 'image']."""
    rel = file.relative_to(APP_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def iter_sources():
    for file in sorted(APP_ROOT.rglob("*.py")):
        if any(part in EXCLUDED_DIRS for part in file.relative_to(APP_ROOT).parts):
            continue
        yield file


def resolve_target(importer: list[str], name: str, level: int, is_package: bool) -> str | None:
    """Resolve an import to an absolute dotted name; return None for external imports.

    ``importer`` is the module path relative to APP_ROOT (e.g. ['routers', 'bad']).
    Relative imports anchor at the importing *package*, then walk up ``level - 1``
    packages (level 1 == current package).
    """
    if level == 0:
        abs_name = name
    else:
        package = importer if is_package else importer[:-1]
        base = package[: len(package) - (level - 1)]
        # Prepend the app package so the external-import check below treats it as internal.
        abs_name = "app." + (".".join([*base, name]) if name else ".".join(base))
    parts = abs_name.split(".")
    if parts[0] != "app":
        return None  # third-party or stdlib
    return ".".join(parts[1:])


def collect_imports(file: Path) -> set[tuple[str, frozenset[str]]]:
    """(dotted target module, imported names) per import statement.

    Names are empty for plain ``import x`` statements.
    """
    importer = module_path(file)
    is_package = file.stem == "__init__"
    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    targets: set[tuple[str, frozenset[str]]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = resolve_target(importer, alias.name, 0, is_package)
                if resolved:
                    targets.add((resolved, frozenset()))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            resolved = resolve_target(importer, node.module, node.level, is_package)
            if resolved:
                names = frozenset(a.name for a in node.names)
                targets.add((resolved, names))
    return targets


def top_module(dotted: str) -> str | None:
    return dotted.split(".")[0] if dotted else None


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str):
        if node in stack:
            cycles.append(stack[stack.index(node):] + [node])
            return
        if node in visited:
            return
        visited.add(node)
        stack.append(node)
        for dep in graph.get(node, ()):
            visit(dep)
        stack.pop()

    for node in sorted(graph):
        visit(node)
    return cycles


def main() -> int:
    if not APP_ROOT.is_dir():
        print("OK")
        return 0

    violations: list[str] = []
    graph: dict[str, set[str]] = {}

    for file in iter_sources():
        src_top = top_module(".".join(module_path(file)))
        if src_top not in ALLOWED:
            continue  # unknown top-level module: no rule defined
        for target, names in collect_imports(file):
            tgt_top = top_module(target)
            if tgt_top == src_top:
                continue  # self-dependency is fine
            graph.setdefault(src_top, set())
            if tgt_top not in ALLOWED:
                continue  # external to the app layering
            graph[src_top].add(tgt_top)
            if tgt_top not in ALLOWED[src_top]:
                rel = file.relative_to(APP_ROOT)
                violations.append(
                    f"{rel}: '{src_top}' must not import '{tgt_top}' (from {target})"
                )
            # routers -> db is get_db only (FastAPI Depends wiring); any other
            # db import (Base, Session, models...) is a layering violation.
            if src_top == "routers" and tgt_top == "db":
                if not (target == "db.session" and names <= {"get_db"}):
                    rel = file.relative_to(APP_ROOT)
                    violations.append(
                        f"{rel}: 'routers' may import only get_db from 'db' (from {target})"
                    )

    cycles = find_cycles(graph)

    if violations or cycles:
        for v in violations:
            print(f"VIOLATION {v}")
        for c in cycles:
            print(f"CYCLE {' -> '.join(c)}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
