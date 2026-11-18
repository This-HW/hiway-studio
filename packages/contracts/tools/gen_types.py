#!/usr/bin/env python3
"""Contract codegen: validate packages/contracts/*.json and generate the web's
TypeScript mirror (apps/web/lib/contracts.gen.ts).

    python packages/contracts/tools/gen_types.py           # validate + write
    python packages/contracts/tools/gen_types.py --check   # validate + fail if stale

Validation has no hard-coded counts. It checks the structure of each file and
cross-checks errors.json against the canonical ErrorCode in
services/api/app/core/errors.py (parsed with the AST, never imported, so this
tool runs without the API's dependencies).

Stdlib only. Output is deterministic (contract order preserved), so `--check`
is a byte comparison.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CONTRACTS_DIR.parent.parent
ERRORS_PY = REPO_ROOT / "services" / "api" / "app" / "core" / "errors.py"
OUTPUT = REPO_ROOT / "apps" / "web" / "lib" / "contracts.gen.ts"

SSE_FIELD_TYPES = {"string": "string", "number": "number", "boolean": "boolean"}
CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*\??$")
ENUM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ContractError(Exception):
    pass


def load(name: str) -> dict:
    path = CONTRACTS_DIR / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{name}: cannot read/parse ({exc})") from exc
    if not isinstance(data, dict):
        raise ContractError(f"{name}: top level must be an object")
    return data


# --- validation ---------------------------------------------------------------


def _python_error_codes() -> dict[str, tuple[int | None, bool | None]] | None:
    """{code: (http, retryable)} parsed from errors.py, or None if absent."""
    if not ERRORS_PY.exists():
        return None
    tree = ast.parse(ERRORS_PY.read_text(encoding="utf-8"))
    codes: dict[str, str] = {}
    tables: dict[str, dict[str, object]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ErrorCode":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    codes[stmt.targets[0].id] = stmt.value.value
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target in ("ERROR_HTTP_STATUS", "ERROR_RETRYABLE") and isinstance(
            node.value, ast.Dict
        ):
            table: dict[str, object] = {}
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Attribute) and isinstance(v, ast.Constant):
                    table[k.attr] = v.value
            tables[target] = table
    if not codes:
        return None
    http = tables.get("ERROR_HTTP_STATUS", {})
    retry = tables.get("ERROR_RETRYABLE", {})
    return {value: (http.get(attr), retry.get(attr)) for attr, value in codes.items()}


def validate_errors(errors: dict) -> list[dict]:
    entries = errors.get("errors")
    if not isinstance(entries, list) or not entries:
        raise ContractError("errors.json: 'errors' must be a non-empty list")
    seen: set[str] = set()
    for e in entries:
        if not isinstance(e, dict):
            raise ContractError(f"errors.json: entry is not an object: {e!r}")
        for key, typ in (("code", str), ("http", int), ("retryable", bool)):
            if key not in e:
                raise ContractError(f"errors.json: entry missing '{key}': {e}")
            if not isinstance(e[key], typ) or (typ is int and isinstance(e[key], bool)):
                raise ContractError(f"errors.json: '{key}' has the wrong type in {e}")
        if not CODE_RE.match(e["code"]):
            raise ContractError(f"errors.json: bad code name {e['code']!r}")
        if not 400 <= e["http"] <= 599:
            raise ContractError(f"errors.json: http out of range in {e}")
        if e["code"] in seen:
            raise ContractError(f"errors.json: duplicate code {e['code']}")
        seen.add(e["code"])

    py = _python_error_codes()
    if py is not None:
        missing = sorted(set(py) - seen)
        extra = sorted(seen - set(py))
        if missing or extra:
            raise ContractError(
                f"errors.json vs errors.py ErrorCode mismatch: missing={missing} extra={extra}"
            )
        for e in entries:
            http, retry = py[e["code"]]
            if http is not None and http != e["http"]:
                raise ContractError(
                    f"errors.json {e['code']}: http {e['http']} != errors.py {http}"
                )
            if retry is not None and retry != e["retryable"]:
                raise ContractError(
                    f"errors.json {e['code']}: retryable {e['retryable']} != errors.py {retry}"
                )
    return entries


def validate_sse(sse: dict) -> dict[str, dict[str, str]]:
    events = sse.get("events")
    if not isinstance(events, dict) or not events:
        raise ContractError("sse_events.json: 'events' must be a non-empty object")
    for name, fields in events.items():
        if not EVENT_RE.match(name):
            raise ContractError(f"sse_events.json: bad event name {name!r}")
        if not isinstance(fields, dict):
            raise ContractError(f"sse_events.json: {name} fields must be an object")
        bare: set[str] = set()
        for field, typ in fields.items():
            if not FIELD_RE.match(field):
                raise ContractError(
                    f"sse_events.json: {name}: bad field name {field!r}"
                )
            if typ not in SSE_FIELD_TYPES:
                raise ContractError(
                    f"sse_events.json: {name}.{field}: unknown type {typ!r} "
                    f"(allowed: {sorted(SSE_FIELD_TYPES)})"
                )
            key = field.rstrip("?")
            if key in bare:
                raise ContractError(
                    f"sse_events.json: {name}: field {key} declared twice"
                )
            bare.add(key)
    return events


def validate_enums(enums: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, values in enums.items():
        if key.startswith("_"):
            continue
        if not ENUM_KEY_RE.match(key):
            raise ContractError(f"enums.json: bad enum name {key!r}")
        if not isinstance(values, list) or not values:
            raise ContractError(f"enums.json: {key} must be a non-empty list")
        if not all(isinstance(v, str) and v for v in values):
            raise ContractError(f"enums.json: {key} values must be non-empty strings")
        if len(set(values)) != len(values):
            raise ContractError(f"enums.json: {key} has duplicate values")
        out[key] = values
    if not out:
        raise ContractError("enums.json: no enums")
    return out


# --- generation -----------------------------------------------------------------


def _pascal(name: str) -> str:
    return "".join(
        part[:1].upper() + part[1:] for part in re.split(r"[._]", name) if part
    )


def _upper(name: str) -> str:
    return name.upper()


def _lit(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _const_list(name: str, values: list[str]) -> list[str]:
    lines = [f"export const {name} = ["]
    lines += [f"  {_lit(v)}," for v in values]
    lines.append("] as const;")
    return lines


def render(
    errors: list[dict], events: dict[str, dict[str, str]], enums: dict[str, list[str]]
) -> str:
    out: list[str] = [
        "// GENERATED FILE — DO NOT EDIT.",
        "// Source: packages/contracts/{errors,sse_events,enums}.json",
        "// Regenerate: make gen-types (packages/contracts/tools/gen_types.py).",
        "// CI/G5 runs `gen_types.py --check`, which fails when this file is stale.",
        "",
        "// --- error codes (errors.json; canonical: services/api/app/core/errors.py) ---",
        "",
    ]
    out += _const_list("ERROR_CODES", [e["code"] for e in errors])
    out += [
        "export type ErrorCode = (typeof ERROR_CODES)[number];",
        "",
        "export const ERROR_SPEC: Record<ErrorCode, { http: number; retryable: boolean }> = {",
    ]
    out += [
        f"  {e['code']}: {{ http: {e['http']}, retryable: {'true' if e['retryable'] else 'false'} }},"
        for e in errors
    ]
    out += ["};", "", "// --- SSE events (sse_events.json) ---", ""]

    names = list(events)
    out += _const_list("SSE_EVENT_NAMES", names)
    out += [
        "export type SseEventName = (typeof SSE_EVENT_NAMES)[number];",
        "",
        "/** Named constants for the SSE event names. */",
        "export const SSE_EVENT = {",
    ]
    out += [f"  {_upper(n.replace('.', '_'))}: {_lit(n)}," for n in names]
    out += ["} as const satisfies Record<string, SseEventName>;", ""]

    for name, fields in events.items():
        out.append(f"/** Payload of the `{name}` SSE event. */")
        out.append(f"export interface {_pascal(name)}Payload {{")
        for field, typ in fields.items():
            optional = field.endswith("?")
            out.append(
                f"  {field.rstrip('?')}{'?' if optional else ''}: {SSE_FIELD_TYPES[typ]};"
            )
        out += ["}", ""]
    out.append("export interface SseEventPayloads {")
    out += [f"  {_lit(n)}: {_pascal(n)}Payload;" for n in names]
    out += ["}", "", "// --- enums (enums.json) ---", ""]

    for key, values in enums.items():
        out += _const_list(_upper(key), values)
        out.append(f"export type {_pascal(key)} = (typeof {_upper(key)})[number];")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def build() -> str:
    errors = validate_errors(load("errors.json"))
    events = validate_sse(load("sse_events.json"))
    enums = validate_enums(load("enums.json"))
    return render(errors, events, enums)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the generated file is missing or stale",
    )
    args = parser.parse_args(argv)
    try:
        text = build()
    except ContractError as exc:
        print(f"FAIL: {exc}")
        return 1
    rel = OUTPUT.relative_to(REPO_ROOT)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else None
        if current != text:
            print(f"FAIL: {rel} is stale — run `make gen-types`")
            return 1
        print(f"OK: contracts valid, {rel} up to date")
        return 0
    if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != text:
        OUTPUT.write_text(text, encoding="utf-8")
        print(f"wrote {rel}")
    else:
        print(f"unchanged {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
