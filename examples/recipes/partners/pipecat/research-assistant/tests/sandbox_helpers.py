# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Host-side helpers for the live suites. No test logic, just plumbing."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable

SANDBOX_ENV = "RESEARCH_SANDBOX"
NEMOCLAW = os.getenv("NEMOCLAW_BIN", "nemoclaw")
OPENSHELL = os.getenv("OPENSHELL_BIN", "openshell")


def run(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    """Run a host command, capturing output and never raising on failure."""
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def output(result: subprocess.CompletedProcess) -> str:
    """Both streams together.

    `openshell` writes its progress ("Policy version N loaded") to stderr, so
    a caller reading only stdout sees nothing. PolicyApprover merges the two
    for exactly this reason, and tests must do the same or they assert against
    an empty string and pass for the wrong reason.
    """
    return (result.stdout or "") + (result.stderr or "")


def wait_for(
    predicate: Callable[[], bool], timeout: float = 30, interval: float = 1.0
) -> bool:
    """Poll until `predicate()` is true. Policy loads are not instant."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
