# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The only component that can widen a running sandbox's policy.

This runs on the host and shells out to `openshell`, which is not installed
inside the sandbox and is not reachable from it. That asymmetry is the whole
security story: the agent can ask for a source, but it cannot grant itself one,
and no text the model produces reaches this module. Only a host-side operator
decision does.

Read `docs/security-model.md` before changing anything here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass

# A conservative hostname grammar. Anything that does not match is refused
# rather than escaped, because this value becomes a subprocess argument.
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)

# Hosts that must never be opened by a spoken approval, whatever the agent
# says it needs. Link-local covers cloud instance metadata, which is the
# classic sandbox-escape target.
NEVER_ALLOW = frozenset({"localhost", "metadata.google.internal"})
NEVER_ALLOW_SUFFIXES = (".internal", ".local", ".localdomain")


# Absolute path to a real binary, nothing clever. This also becomes a
# subprocess argument, so it gets the same treatment as the hostname.
BINARY_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,255}$")


class UnsafeEndpoint(ValueError):
    """Raised when an endpoint must not be opened, regardless of approval."""


def validate_binary(path: str) -> str:
    """Return the binary path if it is safe to scope an approval to."""
    candidate = path.strip()
    if not BINARY_PATH.match(candidate) or ".." in candidate:
        raise UnsafeEndpoint(f"{path!r} is not a valid absolute binary path")
    return candidate


@dataclass(frozen=True)
class ApprovalOutcome:
    endpoint: str
    applied: bool
    detail: str


def validate_host(host: str) -> str:
    """Return the host if it is safe to open, else raise `UnsafeEndpoint`."""
    candidate = host.strip().rstrip(".").lower()
    if not candidate or not HOSTNAME.match(candidate):
        raise UnsafeEndpoint(f"{host!r} is not a valid hostname")
    if candidate in NEVER_ALLOW or candidate.endswith(NEVER_ALLOW_SUFFIXES):
        raise UnsafeEndpoint(f"{candidate!r} is on the never-allow list")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return candidate  # a name, not a literal address
    # Literal addresses skip DNS and are how metadata services get hit.
    if address.is_loopback or address.is_link_local or address.is_private:
        raise UnsafeEndpoint(f"{candidate!r} is a private or link-local address")
    return candidate


class PolicyApprover:
    """Applies an approved endpoint to a live sandbox via the OpenShell CLI."""

    def __init__(
        self,
        sandbox: str,
        *,
        openshell: str = "openshell",
        mode: str = "read-only",
        protocol: str = "rest",
        enforcement: str = "enforce",
        dry_run: bool = False,
        timeout: float = 30.0,
    ):
        self.sandbox = sandbox
        self.openshell = openshell
        self.mode = mode
        self.protocol = protocol
        self.enforcement = enforcement
        self.dry_run = dry_run
        self.timeout = timeout

    def endpoint_spec(self, host: str, port: int) -> str:
        # host:port:mode:protocol:enforcement, per `openshell policy update`.
        return f"{host}:{port}:{self.mode}:{self.protocol}:{self.enforcement}"

    async def allow(
        self, host: str, port: int = 443, binary: str | None = None
    ) -> ApprovalOutcome:
        """Open one endpoint, for one binary. Read-only: research reads, it does not post.

        `binary` is load-bearing, not decorative. An endpoint added without
        `--binary` is merged into the policy and still refused at connect time,
        because OpenShell resolves rules per calling binary. The denial event
        names the binary that was blocked, so an approval opens exactly the
        host, port, and binary that asked - and nothing else.
        """
        safe_host = validate_host(host)
        spec = self.endpoint_spec(safe_host, port)
        command = [
            self.openshell, "policy", "update", self.sandbox, "--add-endpoint", spec,
        ]
        if binary is not None:
            command += ["--binary", validate_binary(binary)]
        command.append("--wait")

        if self.dry_run:
            return ApprovalOutcome(spec, False, f"dry run; would run: {' '.join(command)}")

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            process.kill()
            return ApprovalOutcome(spec, False, "openshell timed out")

        detail = stdout.decode("utf-8", "replace").strip()
        return outcome_from_output(spec, process.returncode, detail)


def outcome_from_output(spec: str, returncode: int | None, detail: str) -> ApprovalOutcome:
    """Decide whether an approval actually took effect.

    A zero exit only means the CLI ran. `openshell` prints "submitted" as soon
    as it hands the revision to the gateway and "loaded" once the sandbox is
    enforcing it, and those can be seconds apart. Reporting success on
    "submitted" would tell the operator a source is open while the next fetch
    still fails, so only "loaded" counts.
    """
    applied = returncode == 0 and "loaded" in detail.lower()
    return ApprovalOutcome(spec, applied, detail)
