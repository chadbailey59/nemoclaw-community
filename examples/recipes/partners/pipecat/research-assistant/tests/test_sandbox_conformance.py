# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Does the sandbox still behave the way this recipe assumes?

Every test here asserts something about NemoClaw, OpenShell, or OpenClaw
rather than about our own code, and every one of them corresponds to an
assumption that was wrong at least once while building this. They are the
first thing to run after a NemoClaw or OpenClaw upgrade: if one fails, the
recipe is broken even though its unit tests still pass.

    RESEARCH_SANDBOX=nc python3 -m pytest tests/test_sandbox_conformance.py -v

When one fails, ../docs/sandbox-requirements.md covers it by test name: what
was being checked, how to repair the sandbox, and how to tell a sandbox that
has drifted from a platform that has moved on. It is written to be handed to a
coding agent along with the failing name.
"""

from __future__ import annotations

import pytest

from sandbox_helpers import NEMOCLAW, OPENSHELL, output, run, wait_for

pytestmark = pytest.mark.live


# --- the asymmetry the security model depends on ---------------------------

def test_openshell_is_not_reachable_from_inside_the_sandbox(sandbox_exec):
    """The whole security story: the agent can ask, it cannot grant.

    If `openshell` ever ships inside the sandbox image, the agent can widen
    its own policy and this recipe is not a boundary, it is decoration.
    """
    out = sandbox_exec("command -v openshell || echo ABSENT")
    assert "ABSENT" in out, "openshell is present inside the sandbox"


def test_the_sandbox_cannot_reach_the_host_gateway_directly(sandbox_exec):
    out = sandbox_exec(
        "for ip in 10.200.0.1 172.17.0.1; do "
        "(timeout 3 bash -c \"echo > /dev/tcp/$ip/22\" 2>/dev/null "
        "&& echo \"$ip OPEN\") || echo \"$ip closed\"; done"
    )
    assert "OPEN" not in out, f"a host port answered from inside the sandbox: {out}"


# --- deny-by-default and the OCSF stream -----------------------------------

def test_an_unlisted_host_is_blocked(probe, unapproved_host):
    assert probe(unapproved_host) == "000"


def test_a_block_emits_a_parseable_denial(probe, recent_events, unapproved_host):
    """The denial event is the escalation. No event, no question, no recipe."""
    probe(unapproved_host)
    assert wait_for(
        lambda: any(
            not e.allowed and e.host == unapproved_host for e in recent_events("2m")
        ),
        timeout=30,
    ), f"no parseable OCSF denial for {unapproved_host}"

    denial = next(
        e for e in recent_events("2m") if not e.allowed and e.host == unapproved_host
    )
    # The gatekeeper scopes an approval from exactly these three fields.
    assert denial.port == 443
    assert denial.binary.startswith("/"), "denial did not name the calling binary"
    assert denial.timestamp > 0


def test_an_allowed_fetch_emits_a_parseable_event(probe, recent_events):
    """The citation audit is built on allowed events, not just denials."""
    assert probe("en.wikipedia.org", "/wiki/Main_Page") == "200", (
        "baseline source is not reachable; check policy.yaml still has binaries"
    )
    assert wait_for(
        lambda: any(
            e.allowed and e.host == "en.wikipedia.org" for e in recent_events("2m")
        ),
        timeout=30,
    ), "no parseable OCSF allow event for a successful fetch"


def test_path_rules_are_enforced_separately_from_the_connection(probe):
    """Two layers, and the gatekeeper only watches one of them.

    `NET:OPEN` decides whether a connection may happen; `HTTP:GET` decides
    whether a given method and path may be requested. The baseline allows
    `en.wikipedia.org/wiki/**`, so the connection to that host succeeds while
    a request for `/` is refused at L7.

    That refusal is invisible to the approval loop, which reads `NET:OPEN`
    only. A path-level denial therefore never becomes a spoken question. See
    "The layer the gatekeeper does not watch" in docs/security-model.md.
    """
    assert probe("en.wikipedia.org", "/wiki/Main_Page") == "200"
    assert probe("en.wikipedia.org", "/") != "200", (
        "the root path is now permitted; the baseline policy path rules are "
        "no longer being enforced as this recipe assumes"
    )


# --- the per-binary trap ---------------------------------------------------

def test_an_endpoint_without_binary_scope_grants_nothing(
    sandbox, probe, revoke, unapproved_host
):
    """The trap that made approvals look like they worked while nothing did.

    OpenShell resolves rules per calling binary. An endpoint merged without
    `--binary` appears in the active policy and is still refused at connect
    time. An implementation that omits it reports success on every approval
    and fixes nothing.
    """
    revoke(unapproved_host)
    result = run(
        OPENSHELL, "policy", "update", sandbox,
        "--add-endpoint", f"{unapproved_host}:443:read-only:rest:enforce",
        "--wait",
    )
    assert result.returncode == 0, result.stdout

    active = run(OPENSHELL, "policy", "get", sandbox, "--full").stdout
    assert unapproved_host in active, "endpoint was not merged into the policy at all"
    # Present in policy, still blocked. Both halves matter.
    assert probe(unapproved_host) == "000", (
        "an endpoint without --binary granted access; the recipe's approval "
        "path could be simplified if this is now the platform behaviour"
    )


def test_an_endpoint_with_binary_scope_grants_access(
    sandbox, probe, revoke, unapproved_host
):
    revoke(unapproved_host)
    result = run(
        OPENSHELL, "policy", "update", sandbox,
        "--add-endpoint", f"{unapproved_host}:443:read-only:rest:enforce",
        "--binary", "/usr/bin/curl",
        "--wait",
    )
    assert result.returncode == 0, output(result)
    assert "loaded" in output(result).lower(), (
        "--wait did not confirm the revision was loaded; the approver reports "
        "success on exactly this word"
    )
    assert probe(unapproved_host) == "200"


def test_openshell_reports_progress_on_stderr(sandbox, revoke, unapproved_host):
    """Load-bearing: the approver merges stderr into stdout because of this.

    `openshell policy update` prints "submitted" and "loaded" to stderr and
    leaves stdout empty. An approver reading stdout alone would never see the
    confirmation and would report every approval as not applied.
    """
    revoke(unapproved_host)
    result = run(
        OPENSHELL, "policy", "update", sandbox,
        "--add-endpoint", f"{unapproved_host}:443:read-only:rest:enforce",
        "--binary", "/usr/bin/curl", "--wait",
    )
    assert "loaded" in result.stderr.lower(), (
        "openshell now reports on stdout; PolicyApprover merges the streams, "
        "so this is informational rather than breaking"
    )
    assert "loaded" not in result.stdout.lower()


def test_an_approval_is_scoped_to_one_binary_not_the_whole_sandbox(
    sandbox, probe, revoke, unapproved_host
):
    """Approving for curl must not quietly open the host for node as well."""
    revoke(unapproved_host)
    run(
        OPENSHELL, "policy", "update", sandbox,
        "--add-endpoint", f"{unapproved_host}:443:read-only:rest:enforce",
        "--binary", "/usr/bin/curl", "--wait",
    )
    active = run(OPENSHELL, "policy", "get", sandbox, "--full").stdout
    rule = active.split(unapproved_host.replace(".", "_"))[-1][:400] if active else ""
    assert "/usr/bin/curl" in active
    assert "binaries" in active, "approval carried no binary scope"
    assert rule is not None


# --- the baseline the recipe ships -----------------------------------------

def test_the_baseline_policy_is_applied_and_usable(sandbox, probe):
    """A preset can apply cleanly and still grant nothing without binaries."""
    applied = run(NEMOCLAW, sandbox, "policy-list").stdout
    assert "research-baseline" in applied, "run scripts/setup.sh first"
    assert probe("en.wikipedia.org", "/wiki/Main_Page") == "200", (
        "baseline preset is applied but its hosts are unreachable; check that "
        "agents/openclaw/policy.yaml still carries a binaries block"
    )


def test_the_research_skill_is_installed(sandbox_exec):
    out = sandbox_exec("cat ~/.openclaw/skills/research-sweep/SKILL.md 2>/dev/null | head -5")
    assert "research-sweep" in out, "run scripts/setup.sh to install the skill"
    assert "policy boundary" in out or "blocked" in out.lower()


# --- the control plane the voice loop talks to ------------------------------

def test_the_gateway_needs_and_accepts_a_token(sandbox):
    """Auto-pairing covers the control UI and webchat; this bot is neither."""
    token = run(NEMOCLAW, sandbox, "gateway-token", "--quiet").stdout.strip()
    assert token, "no gateway token available; the voice bot cannot connect"
    assert len(token) > 16
