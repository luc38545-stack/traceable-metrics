#!/usr/bin/env python3
"""Opt-in Docker acceptance for ADR-13 runtime track isolation.

This suite is intentionally not collected by the normal pytest run. It requires
Docker Desktop and creates the compose stack. Run it explicitly after Docker is
available; the stack is removed in a finally block.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "infra" / "compose.yml"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from casekit import run_cli  # noqa: E402


def _docker_executable() -> str:
    found = shutil.which("docker")
    if found:
        return found
    fallback = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / (
        "Docker/Docker/resources/bin/docker.exe"
    )
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("Docker CLI not found")


DOCKER = _docker_executable()
_stack_started = False


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [DOCKER, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise AssertionError(f"docker {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run("compose", "-f", str(COMPOSE), *args, check=check)


def _ensure_stack() -> None:
    global _stack_started
    if _stack_started:
        return
    _compose("up", "-d")
    _stack_started = True
    time.sleep(12)


def _down() -> None:
    global _stack_started
    if not _stack_started:
        return
    _compose("down", "--remove-orphans", check=False)
    _stack_started = False


def _inspect() -> dict[str, dict]:
    _ensure_stack()
    items = json.loads(_run("inspect", "traceable-commerce", "traceable-research").stdout)
    return {item["Name"].lstrip("/"): item for item in items}


def dkr1_compose_config_valid() -> bool:
    _compose("config", "--quiet")
    return True


def dkr2_services_stay_running() -> bool:
    items = _inspect()
    for name in ("traceable-commerce", "traceable-research"):
        item = items[name]
        assert item["State"]["Status"] == "running", f"{name} status={item['State']['Status']}"
        assert item["RestartCount"] == 0, f"{name} restart_count={item['RestartCount']}"
    return True


def dkr3_actual_mounts_are_single_track() -> bool:
    items = _inspect()
    expected = {
        "traceable-commerce": "/data/commerce",
        "traceable-research": "/data/research",
    }
    for name, own_mount in expected.items():
        destinations = {mount["Destination"] for mount in items[name]["Mounts"]}
        other_mount = "/data/research" if own_mount.endswith("commerce") else "/data/commerce"
        assert own_mount in destinations, f"{name} missing {own_mount}: {sorted(destinations)}"
        assert other_mount not in destinations, f"{name} unexpectedly mounts {other_mount}"
    return True


def dkr4_actual_networks_are_disjoint() -> bool:
    items = _inspect()
    commerce = set(items["traceable-commerce"]["NetworkSettings"]["Networks"])
    research = set(items["traceable-research"]["NetworkSettings"]["Networks"])
    assert commerce == {"infra_traceable-commerce-net"}, commerce
    assert research == {"infra_traceable-research-net"}, research
    assert commerce.isdisjoint(research)
    bridge = _run(
        "network", "ls", "--filter", "name=infra_traceable-bridge-net", "--format", "{{.Name}}"
    ).stdout.strip()
    assert bridge == "", f"unused bridge network was created: {bridge}"
    return True


def _run_probe(service: str, code: str) -> str:
    return _compose(
        "run", "--rm", "--no-deps", "--entrypoint", "python", service, "-c", code
    ).stdout.strip()


def dkr5_cross_track_paths_raise_file_not_found() -> bool:
    _ensure_stack()
    probes = {
        "commerce": "/data/research/forbidden-probe",
        "research": "/data/commerce/forbidden-probe",
    }
    for service, path in probes.items():
        code = (
            "from pathlib import Path\n"
            f"p=Path({path!r})\n"
            "try:\n p.read_bytes()\n"
            "except FileNotFoundError:\n print('FileNotFoundError')\n"
            "else:\n raise SystemExit('cross-track path unexpectedly exists')\n"
        )
        assert _run_probe(service, code) == "FileNotFoundError"
    return True


def dkr6_cross_track_dns_is_unreachable() -> bool:
    _ensure_stack()
    probes = {
        "commerce": "traceable-research",
        "research": "traceable-commerce",
    }
    for service, hostname in probes.items():
        code = (
            "import socket\n"
            f"hostname={hostname!r}\n"
            "try:\n socket.getaddrinfo(hostname, 80)\n"
            "except socket.gaierror:\n print('gaierror')\n"
            "else:\n raise SystemExit('cross-track hostname unexpectedly resolved')\n"
        )
        assert _run_probe(service, code) == "gaierror"
    return True


CASES = [
    ("DKR1 compose config parses", dkr1_compose_config_valid),
    ("DKR2 services remain running without restarts", dkr2_services_stay_running),
    ("DKR3 actual mounts contain only the owning track", dkr3_actual_mounts_are_single_track),
    ("DKR4 actual networks are disjoint and bridge is absent", dkr4_actual_networks_are_disjoint),
    ("DKR5 cross-track paths raise FileNotFoundError", dkr5_cross_track_paths_raise_file_not_found),
    ("DKR6 cross-track DNS names do not resolve", dkr6_cross_track_dns_is_unreachable),
]
REJECT_CASES: list = []
FAILMSG_CASES: list = []


def main() -> int:
    try:
        return run_cli("ADR-13 Docker runtime isolation", CASES, REJECT_CASES, FAILMSG_CASES)
    finally:
        _down()


if __name__ == "__main__":
    raise SystemExit(main())
