# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures, including the opt-in gate for tests that need a sandbox.

The unit tests run anywhere. The suites in `test_sandbox_conformance.py` and
`test_agent_behavior.py` talk to a real NemoClaw sandbox and are skipped unless
`RESEARCH_SANDBOX` names one:

    RESEARCH_SANDBOX=nc python3 -m pytest tests -q

They exist because this recipe rests on assumptions about the platform rather
than about its own code, and every one of those assumptions was wrong at least
once during development. A mock would have agreed with each mistake.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gatekeeper.denials import NetworkEvent, parse_network_event  # noqa: E402
from sandbox_helpers import NEMOCLAW, OPENSHELL, SANDBOX_ENV, run  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: needs a running NemoClaw sandbox (set RESEARCH_SANDBOX)"
    )
    config.addinivalue_line(
        "markers", "slow: runs a real agent sweep; minutes, and model-dependent"
    )


@pytest.fixture(scope="session")
def sandbox() -> str:
    """The sandbox under test, or skip the whole live suite."""
    name = os.getenv(SANDBOX_ENV)
    if not name:
        pytest.skip(f"set {SANDBOX_ENV}=<sandbox> to run live tests")
    for tool in (NEMOCLAW, OPENSHELL):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    listed = run(NEMOCLAW, "list")
    if name not in listed.stdout:
        pytest.skip(f"sandbox {name!r} not found; `nemoclaw list` does not know it")
    return name


@pytest.fixture(scope="session")
def sandbox_exec(sandbox: str):
    """Run a shell snippet inside the sandbox."""

    def _exec(script: str, timeout: float = 90) -> str:
        result = run(NEMOCLAW, sandbox, "exec", "--", "bash", "-lc", script, timeout=timeout)
        return (result.stdout or "") + (result.stderr or "")

    return _exec


@pytest.fixture(scope="session")
def probe(sandbox_exec):
    """HTTP status the sandbox gets for a URL. '000' means the connection failed.

    `path` matters. OpenShell enforces at two layers: `NET:OPEN` decides
    whether the connection may be made at all, and `HTTP:GET` decides whether
    that specific method and path may be requested. A host can be reachable
    while `/` is refused, which is exactly what the shipped baseline does -
    it allows `en.wikipedia.org/wiki/**`, not the whole site.
    """

    def _probe(host: str, path: str = "/", timeout: float = 20) -> str:
        out = sandbox_exec(
            f"curl -s -m 15 -o /dev/null -w '%{{http_code}}' https://{host}{path} || true",
            timeout=timeout + 60,
        )
        codes = [line.strip() for line in out.splitlines() if line.strip().isdigit()]
        return codes[-1] if codes else "000"

    return _probe


@pytest.fixture(scope="session")
def recent_events(sandbox: str):
    """Parsed OCSF network events from the recent past."""

    def _recent(since: str = "3m", lines: int = 600) -> list[NetworkEvent]:
        result = run(
            OPENSHELL, "logs", sandbox, "-n", str(lines),
            "--level", "debug", "--since", since,
            timeout=90,
        )
        events = (parse_network_event(line) for line in result.stdout.splitlines())
        return [event for event in events if event is not None]

    return _recent


@pytest.fixture
def revoke(sandbox: str):
    """Remove endpoints a test opened, so the suite leaves no policy behind."""
    opened: list[str] = []

    def _track(host: str, port: int = 443) -> None:
        opened.append(f"{host}:{port}")

    yield _track

    for endpoint in opened:
        run(OPENSHELL, "policy", "update", sandbox, "--remove-endpoint", endpoint,
            "--wait", timeout=90)


@pytest.fixture
def unapproved_host(probe) -> str:
    """A host that is currently blocked, so a test can watch it become allowed.

    Chosen at runtime rather than hardcoded: a previous run may have left any
    given host open, and a test that silently starts from "already allowed"
    proves nothing.
    """
    for candidate in (
        "example.com", "www.iana.org", "www.rfc-editor.org.invalid",
        "www.gutenberg.org", "www.w3.org", "neverssl.com",
    ):
        if probe(candidate) == "000":
            return candidate
    pytest.skip("no reliably blocked host available; run scripts/reset-approvals.sh")
