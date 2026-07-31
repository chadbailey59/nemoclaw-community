# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read OpenShell's OCSF denial stream and turn it into approval requests.

When the research agent reaches for a source that is not in its policy, the
OpenShell gateway blocks the connection and emits an OCSF `NET:OPEN` event with
disposition `DENIED`. That event is the escalation: the agent is stalled, and
only a human can decide whether that source is worth opening.

This module only reads. Nothing here can widen a policy - see `approver.py`,
which is deliberately a separate seam.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

# The gateway emits NET:OPEN in two shapes. Research fetches name the calling
# binary and the matched policy:
#
#   NET:OPEN [MED] DENIED /usr/bin/curl(74441) -> arxiv.org:443
#       [policy:- engine:opa] [reason:endpoint arxiv.org:443 not in policy...]
#   NET:OPEN [INFO] ALLOWED /usr/local/bin/node(364) -> arxiv.org:443
#       [policy:allow_arxiv_org_443 engine:opa]
#
# The sandbox's own inference traffic uses a bare form with neither:
#
#   NET:OPEN [INFO] ALLOWED inference.local:443
#
# One pattern reads both, so an allowed fetch is as visible as a blocked one.
# That matters: knowing what the agent *did* read is what makes it possible to
# check an answer's cited sources against reality.
NET_EVENT = re.compile(
    r"\[(?P<ts>[\d.]+)\]\s+"
    r"\[(?P<stream>\w+)\]\s+"
    r"\[OCSF\s*\]\s+\[ocsf\]\s+"
    r"NET:OPEN\s+\[(?P<severity>\w+)\]\s+(?P<disposition>ALLOWED|DENIED)\s+"
    r"(?:(?P<binary>/\S+?)\((?P<pid>\d+)\)\s+->\s+)?"
    r"(?P<host>[A-Za-z0-9._-]+):(?P<port>\d+)"
    r"(?:.*?\[reason:(?P<reason>[^\]]*)\])?"
)


@dataclass(frozen=True)
class Denial:
    """One blocked outbound connection attempt."""

    host: str
    port: int
    binary: str
    pid: int
    timestamp: float
    severity: str = "MED"
    reason: str = ""

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def is_tls(self) -> bool:
        return self.port == 443

    def describe(self) -> str:
        """One spoken sentence. The host is the part a human decides on."""
        return f"The research agent tried to reach {self.host} and was blocked."


@dataclass(frozen=True)
class NetworkEvent:
    """One connection attempt the gateway saw, allowed or denied."""

    host: str
    port: int
    allowed: bool
    timestamp: float
    binary: str = ""
    pid: int = 0
    severity: str = "INFO"
    reason: str = ""

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def as_denial(self) -> Denial | None:
        if self.allowed:
            return None
        return Denial(
            host=self.host,
            port=self.port,
            binary=self.binary,
            pid=self.pid,
            timestamp=self.timestamp,
            severity=self.severity,
            reason=self.reason,
        )


def parse_network_event(line: str) -> NetworkEvent | None:
    """Parse one log line into a network event, or None if it is not one."""
    match = NET_EVENT.search(line)
    if match is None:
        return None
    return NetworkEvent(
        host=match["host"],
        port=int(match["port"]),
        allowed=match["disposition"] == "ALLOWED",
        timestamp=float(match["ts"]),
        binary=match["binary"] or "",
        pid=int(match["pid"] or 0),
        severity=match["severity"],
        reason=(match["reason"] or "").strip(),
    )


def parse_denial(line: str) -> Denial | None:
    """Parse one log line, or return None if it is not a denial."""
    event = parse_network_event(line)
    return event.as_denial() if event is not None else None


class DenialWatcher:
    """Tails `openshell logs` and yields denials as they happen.

    The command is injected so tests can drive this with a scripted stream
    instead of a live gateway.
    """

    def __init__(
        self,
        sandbox: str,
        *,
        openshell: str = "openshell",
        command: list[str] | None = None,
    ):
        self.sandbox = sandbox
        self._command = command or [
            openshell, "logs", sandbox,
            "--tail", "--level", "debug", "--source", "sandbox",
        ]
        self._process: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> DenialWatcher:
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._process is None or self._process.returncode is not None:
            return
        self._process.terminate()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._process.kill()

    async def network_events(self) -> AsyncIterator[NetworkEvent]:
        """Every connection the gateway saw, allowed or denied."""
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("Watcher is not started; use it as a context manager.")
        async for raw in self._process.stdout:
            event = parse_network_event(raw.decode("utf-8", "replace"))
            if event is not None:
                yield event

    async def denials(self) -> AsyncIterator[Denial]:
        async for event in self.network_events():
            denial = event.as_denial()
            if denial is not None:
                yield denial


class Deduplicator:
    """Collapses repeat denials for the same endpoint.

    A blocked fetch retries, and every retry emits another OCSF event. Asking
    a human the same question four times is how you train them to say yes
    without listening, so each endpoint is only ever asked once per run.
    """

    def __init__(self):
        self.seen: dict[str, Any] = {}

    def is_new(self, denial: Denial) -> bool:
        return denial.endpoint not in self.seen

    def record(self, denial: Denial, decision: Any = None) -> None:
        self.seen[denial.endpoint] = decision

    def decision_for(self, denial: Denial) -> Any:
        return self.seen.get(denial.endpoint)


@dataclass
class ResearchScope:
    """What the operator has already agreed this run may read.

    Seeded from the recipe's baseline policy so the agent does not stop to ask
    about the sources it was configured to use.
    """

    allowed: set[str] = field(default_factory=set)
    denied: set[str] = field(default_factory=set)
    on_change: Callable[[str, bool], None] | None = None

    def allow(self, host: str) -> None:
        self.allowed.add(host)
        self.denied.discard(host)
        if self.on_change:
            self.on_change(host, True)

    def deny(self, host: str) -> None:
        self.denied.add(host)
        self.allowed.discard(host)
        if self.on_change:
            self.on_change(host, False)

    def status(self, host: str) -> str:
        if host in self.allowed:
            return "allowed"
        return "denied" if host in self.denied else "unknown"
