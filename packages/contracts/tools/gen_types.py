#!/usr/bin/env python3
"""Phase 0 stub for contract codegen.

Reads the three contract JSON files and validates them (JSON parse + error
count check). Real TS codegen is Phase 1+.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent.parent

EXPECTED_ERRORS = 14


def load(name: str) -> dict:
    path = CONTRACTS_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    errors = load("errors.json")
    codes = [e.get("code") for e in errors.get("errors", [])]
    if len(codes) != EXPECTED_ERRORS:
        print(f"FAIL: expected {EXPECTED_ERRORS} errors, found {len(codes)}")
        return 1
    if len(set(codes)) != len(codes):
        print("FAIL: duplicate error codes")
        return 1
    for e in errors["errors"]:
        for key in ("code", "http", "retryable"):
            if key not in e:
                print(f"FAIL: error entry missing key '{key}': {e}")
                return 1

    sse = load("sse_events.json")
    if "events" not in sse:
        print("FAIL: sse_events.json missing 'events'")
        return 1

    enums = load("enums.json")
    if not isinstance(enums, dict) or not enums:
        print("FAIL: enums.json is empty or invalid")
        return 1

    print("TS codegen is a Phase 1+ stub (contracts are source of truth)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
