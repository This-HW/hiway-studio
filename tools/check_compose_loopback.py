#!/usr/bin/env python3
"""G7: every published port in docker-compose.yml binds to 127.0.0.1 only.

The studio is a single-user local tool with no authentication, so a port
published on 0.0.0.0 would expose the API (and its file serving) to the LAN.

Runs `docker compose config --format json` (the normalized form, so short
syntax like "8000:8000" shows up as host_ip "" = all interfaces) in a
temporary project directory, with .env.example standing in for .env (the
compose file requires an env_file; the real .env is never read or created).

Exit 0 when every published port has host_ip 127.0.0.1, 1 otherwise,
2 when docker compose itself fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOOPBACK = "127.0.0.1"


def compose_config(compose_file: Path, env_example: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="compose-loopback-") as tmp:
        tmp_dir = Path(tmp)
        shutil.copy(compose_file, tmp_dir / "docker-compose.yml")
        shutil.copy(env_example, tmp_dir / ".env")
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(tmp_dir),
                "-f",
                str(tmp_dir / "docker-compose.yml"),
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        raise SystemExit(2)
    return json.loads(proc.stdout)


def violations(config: dict) -> list[str]:
    """Every entry of service.ports is a HOST publication (a bare "8000" means
    an automatically assigned host port on all interfaces), so each one must
    carry host_ip 127.0.0.1. `expose` is container-only and not checked.
    network_mode "host" bypasses port mapping entirely and is refused
    (review B8-H1, B8-M1)."""
    bad: list[str] = []
    for name, service in sorted((config.get("services") or {}).items()):
        if str(service.get("network_mode") or "").strip() == "host":
            bad.append(f"{name}: network_mode host (binds host interfaces directly)")
        for port in service.get("ports") or []:
            host_ip = port.get("host_ip") or "0.0.0.0"
            if host_ip != LOOPBACK:
                published = port.get("published") or "<auto>"
                bad.append(f"{name}: {host_ip}:{published} -> {port.get('target')}")
    return bad


_API_PORT_LINE = '      - "127.0.0.1:${API_PORT:-8000}:${API_PORT:-8000}"\n'
_PORTS_BLOCK_HEAD = "    ports:\n"


def self_test() -> int:
    """Run the check on variants of the real compose file through the real
    `docker compose config` normalization: loopback and expose-only pass;
    explicit 0.0.0.0, an auto-assigned host port and host networking fail."""
    src = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    if src.count(_API_PORT_LINE) != 1:
        print(
            "self-test: API port line not found in docker-compose.yml", file=sys.stderr
        )
        return 2
    without_ports = src.replace(_API_PORT_LINE, "")
    # Drop the now-empty ports: key (and its comment lines) for the variants
    # that publish nothing.
    head = without_ports.index(_PORTS_BLOCK_HEAD)
    tail = without_ports.index("\n\n", head)
    no_ports = without_ports[:head] + without_ports[tail + 1 :]
    cases = {
        "loopback (current file)": (src, True),
        "explicit 0.0.0.0": (
            src.replace(_API_PORT_LINE, '      - "8000:8000"\n'),
            False,
        ),
        "auto-assigned host port": (
            src.replace(_API_PORT_LINE, '      - "8000"\n'),
            False,
        ),
        "expose only": (
            no_ports[:head] + '    expose:\n      - "8000"\n' + no_ports[head:],
            True,
        ),
        "network_mode host": (
            no_ports[:head] + "    network_mode: host\n" + no_ports[head:],
            False,
        ),
    }
    failed = 0
    with tempfile.TemporaryDirectory(prefix="compose-selftest-") as tmp:
        for label, (text, should_pass) in cases.items():
            path = Path(tmp) / "docker-compose.yml"
            path.write_text(text, encoding="utf-8")
            bad = violations(compose_config(path, REPO / ".env.example"))
            passed = not bad
            ok = passed == should_pass
            failed += not ok
            verdict = "pass" if passed else f"fail {bad}"
            print(f"{'OK ' if ok else 'BAD'} {label}: {verdict}")
    return 1 if failed else 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return self_test()
    config = compose_config(REPO / "docker-compose.yml", REPO / ".env.example")
    ports = [
        (name, p)
        for name, s in (config.get("services") or {}).items()
        for p in s.get("ports") or []
    ]
    bad = violations(config)
    if bad:
        print("FAIL: services reachable beyond loopback:")
        for line in bad:
            print(f"  {line}")
        return 1
    print(f"OK: {len(ports)} published port(s), all bound to {LOOPBACK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
