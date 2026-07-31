# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gatekeeper import (  # noqa: E402
    Gatekeeper,
    PolicyApprover,
    ResearchScope,
    UnsafeEndpoint,
    interpret,
    parse_denial,
    validate_host,
)
from gatekeeper.approver import (  # noqa: E402
    ApprovalOutcome,
    outcome_from_output,
    validate_binary,
)

# Captured verbatim from `openshell logs nc --level debug` on a live sandbox.
REAL_DENIAL = (
    "[1785462960.759] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED "
    "/usr/bin/curl(74441) -> arxiv.org:443 [policy:- engine:opa] "
    "[reason:endpoint arxiv.org:443 not in policy 'brew'; endpoint arxiv.org:443 not in polic...]"
)
NOISE = (
    "[1785462841.762] [gateway] [INFO ] [openshell_server::supervisor_session] "
    "supervisor session: relay opened successfully"
)


def run(coro):
    """Run one coroutine test. Keeps the recipe free of pytest-asyncio."""
    return asyncio.run(coro)


class FakeWatcher:
    def __init__(self, lines):
        self._lines = lines

    async def denials(self):
        for line in self._lines:
            denial = parse_denial(line)
            if denial is not None:
                yield denial
            await asyncio.sleep(0)


class FakeApprover(PolicyApprover):
    def __init__(self, **kwargs):
        super().__init__("nc", **kwargs)
        self.calls: list[tuple[str, int, str | None]] = []

    async def allow(
        self, host: str, port: int = 443, binary: str | None = None
    ) -> ApprovalOutcome:
        validate_host(host)  # keep the real guards in the fake
        if binary is not None:
            validate_binary(binary)
        self.calls.append((host, port, binary))
        return ApprovalOutcome(self.endpoint_spec(host, port), True, "policy version 9 loaded")


# --- parsing ---------------------------------------------------------------

def test_parses_a_real_denial_line():
    denial = parse_denial(REAL_DENIAL)
    assert denial is not None
    assert denial.host == "arxiv.org"
    assert denial.port == 443
    assert denial.binary == "/usr/bin/curl"
    assert denial.pid == 74441
    assert denial.is_tls
    assert "not in policy" in denial.reason


def test_ignores_non_denial_lines():
    assert parse_denial(NOISE) is None
    assert parse_denial("") is None
    assert parse_denial("NET:OPEN [MED] ALLOWED /usr/bin/curl(1) -> example.com:443") is None


# --- the never-allow guard -------------------------------------------------

@pytest.mark.parametrize(
    "host",
    ["localhost", "169.254.169.254", "127.0.0.1", "10.200.0.1", "foo.internal", "192.168.1.5"],
)
def test_dangerous_hosts_are_refused(host):
    with pytest.raises(UnsafeEndpoint):
        validate_host(host)


@pytest.mark.parametrize("host", ["arxiv.org", "www.nature.com", "API.GitHub.com", "example.org."])
def test_ordinary_hosts_are_accepted(host):
    assert validate_host(host) == host.strip().rstrip(".").lower()


def test_shell_metacharacters_are_refused():
    for host in ["evil.com; rm -rf /", "a b.com", "$(whoami).com", "x.com&&curl"]:
        with pytest.raises(UnsafeEndpoint):
            validate_host(host)


# --- reading spoken answers ------------------------------------------------

@pytest.mark.parametrize(
    ("text", "choice"),
    [
        ("yes", "approved"), ("Yes, allow it.", "approved"), ("go ahead", "approved"),
        ("no", "rejected"), ("nope", "rejected"), ("deny that", "rejected"),
        ("what is arxiv?", "unclear"), ("", "unclear"), ("hmm", "unclear"),
    ],
)
def test_interpret(text, choice):
    assert interpret(text) == choice


def test_only_a_clear_yes_opens_a_source():
    for text in ("maybe", "I think so?", "which one", "not sure", "hold on"):
        assert interpret(text) != "approved", text


def test_a_leading_affirmative_still_approves():
    # Known and accepted: the classifier reads the leading word, so a trailing
    # qualifier is lost. The voice loop is expected to resolve hedged answers
    # into a clean yes or no before calling answer(); this keyword pass is the
    # backstop, not the primary reader. See docs/security-model.md.
    assert interpret("sure thing, but check the date") == "approved"


# --- the loop --------------------------------------------------------------

def test_a_denial_becomes_a_question_and_a_yes_opens_it():
    async def main():
        approver = FakeApprover()
        keeper = Gatekeeper(FakeWatcher([NOISE, REAL_DENIAL]), approver)
        await keeper.run()

        assert len(keeper.pending) == 1
        assert "arxiv.org" in keeper.pending[0].prompt

        outcome = await keeper.answer("yes")
        assert outcome is not None and outcome.applied
        # Scoped to the exact binary the denial named, not opened world-wide.
        assert approver.calls == [("arxiv.org", 443, "/usr/bin/curl")]
        assert keeper.scope.status("arxiv.org") == "allowed"
    run(main())

def test_no_leaves_the_policy_untouched():
    async def main():
        approver = FakeApprover()
        keeper = Gatekeeper(FakeWatcher([REAL_DENIAL]), approver)
        await keeper.run()

        assert await keeper.answer("no") is None
        assert approver.calls == []
        assert keeper.scope.status("arxiv.org") == "denied"
    run(main())

def test_an_unclear_answer_opens_nothing_and_keeps_asking():
    async def main():
        approver = FakeApprover()
        keeper = Gatekeeper(FakeWatcher([REAL_DENIAL]), approver)
        await keeper.run()

        assert await keeper.answer("uh, what is that") is None
        assert approver.calls == []
        # The question is still outstanding: an unclear answer is not an answer.
        assert len(keeper.pending) == 1
    run(main())

def test_repeat_denials_ask_only_once():
    async def main():
        approver = FakeApprover()
        keeper = Gatekeeper(FakeWatcher([REAL_DENIAL] * 5), approver)
        await keeper.run()
        assert len(keeper.pending) == 1
    run(main())

def test_auto_allow_skips_the_question():
    async def main():
        approver = FakeApprover()
        keeper = Gatekeeper(
            FakeWatcher([REAL_DENIAL]), approver, auto_allow=frozenset({"arxiv.org"})
        )
        await keeper.run()
        assert keeper.pending == ()
        assert approver.calls == [("arxiv.org", 443, "/usr/bin/curl")]
    run(main())

def test_approval_of_an_unsafe_host_is_refused_out_loud():
    async def main():
        denial = (
            "[1785462960.759] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED "
            "/usr/bin/curl(9) -> 169.254.169.254:80 [policy:- engine:opa]"
        )
        approver = FakeApprover()
        keeper = Gatekeeper(FakeWatcher([denial]), approver)
        await keeper.run()

        assert await keeper.answer("yes") is None
        assert approver.calls == []
        kinds = []
        while not keeper.events.empty():
            kinds.append(keeper.events.get_nowait())
        assert any(e.kind == "error" and e.data.get("refused") for e in kinds)
    run(main())

def test_answering_with_nothing_pending_is_a_no_op():
    async def main():
        keeper = Gatekeeper(FakeWatcher([]), FakeApprover())
        assert await keeper.answer("yes") is None
    run(main())

def test_endpoint_spec_is_read_only_by_default():
    spec = PolicyApprover("nc").endpoint_spec("arxiv.org", 443)
    assert spec == "arxiv.org:443:read-only:rest:enforce"


@pytest.mark.parametrize(
    "path", ["/usr/bin/curl; rm -rf /", "curl", "../../bin/sh", "/usr/bin/../../etc/x", ""]
)
def test_bad_binary_paths_are_refused(path):
    with pytest.raises(UnsafeEndpoint):
        validate_binary(path)


def test_good_binary_paths_are_accepted():
    for path in ["/usr/bin/curl", "/usr/local/bin/node", "/opt/openclaw/bin/openclaw"]:
        assert validate_binary(path) == path


def test_approval_command_carries_binary_scope_and_wait():
    """Verified live: without --binary the endpoint merges but still 403s.

    OpenShell resolves network rules per calling binary, so an endpoint added
    without a binary scope is present in the policy and still refused at
    connect time. --wait makes the gateway confirm the revision is live.
    """
    async def main():
        approver = PolicyApprover("nc", dry_run=True)
        outcome = await approver.allow("arxiv.org", 443, "/usr/bin/curl")
        assert "--binary /usr/bin/curl" in outcome.detail
        assert "--wait" in outcome.detail
        assert "arxiv.org:443:read-only:rest:enforce" in outcome.detail
    run(main())


def test_an_approval_is_not_announced_until_the_gateway_loads_it():
    """A zero exit is not proof. Only a loaded revision is.

    Both strings are real `openshell policy update` output; submitting and
    loading can be seconds apart, and announcing the first would tell the
    operator a source is open while the next fetch still fails.
    """
    spec = "arxiv.org:443:read-only:rest:enforce"
    submitted = "! expanded access preset\n✓ Policy version 9 submitted (hash: 377b2)"
    loaded = f"{submitted}\n✓ Policy version 9 loaded (active version: 9)"

    assert not outcome_from_output(spec, 0, submitted).applied
    assert outcome_from_output(spec, 0, loaded).applied
    assert not outcome_from_output(spec, 1, loaded).applied


def test_dry_run_never_touches_policy():
    async def main():
        outcome = await PolicyApprover("nc", dry_run=True).allow("arxiv.org")
        assert not outcome.applied and "dry run" in outcome.detail
    run(main())

def _denial(host, ts="1785462960.759", binary="/usr/bin/curl"):
    return (
        f"[{ts}] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED "
        f"{binary}(74441) -> {host}:443 [policy:- engine:opa]"
    )


def test_an_answer_for_the_wrong_host_opens_nothing():
    """Observed live: the bot asked about one host and opened another.

    Three denials queued at once, the voice loop read out the last, and the
    answer resolved the first. The person approved a host they were never
    asked about. Consent is for one source; applying it to another is a
    substitution, not an approval.
    """
    async def main():
        approver = FakeApprover()
        keeper = Gatekeeper(
            FakeWatcher([_denial("arxiv.org"), _denial("en.wikipedia.org")]), approver
        )
        await keeper.run()

        # Only one question is ever outstanding, so there is no ambiguity.
        assert keeper.asked is not None
        assert keeper.asked.host == "arxiv.org"

        # An answer attributed to a different host must not open anything.
        assert await keeper.answer("yes", host="en.wikipedia.org") is None
        assert approver.calls == []
        assert keeper.asked.host == "arxiv.org"  # still outstanding

        # Answering the host actually asked about works.
        outcome = await keeper.answer("yes", host="arxiv.org")
        assert outcome is not None and outcome.applied
        assert approver.calls == [("arxiv.org", 443, "/usr/bin/curl")]
    run(main())


def test_only_one_question_is_outstanding_at_a_time():
    async def main():
        keeper = Gatekeeper(
            FakeWatcher([_denial("a.example"), _denial("b.example"), _denial("c.example")]),
            FakeApprover(),
        )
        await keeper.run()
        assert keeper.asked.host == "a.example"
        assert len(keeper.pending) == 3  # two queued behind it

        await keeper.answer("no", host="a.example")
        assert keeper.asked.host == "b.example"  # next one surfaces only now
    run(main())


def test_denials_from_before_the_session_are_ignored():
    """`openshell logs --tail` replays history; those are not live requests."""
    async def main():
        keeper = Gatekeeper(
            FakeWatcher([_denial("old.example", ts="1000.0"), _denial("new.example", ts="3000.0")]),
            FakeApprover(),
            since=2000.0,
        )
        await keeper.run()
        assert [q.host for q in keeper.pending] == ["new.example"]
    run(main())


def test_resume_message_tells_the_agent_to_continue_not_restart():
    """An approval nobody acts on is a no-op.

    Observed live: the agent gave up on arxiv.org and finished its run seven
    seconds before the operator approved it. The policy changed correctly and
    changed nothing, because the agent was never told.
    """
    from gatekeeper.service import RESUME_TEMPLATE

    message = RESUME_TEMPLATE.format(host="arxiv.org")
    assert "arxiv.org is now open" in message
    assert "do not start over" in message.lower()
    assert "continue" in message.lower()


def test_agent_instructions_forbid_burst_retry_and_early_finish():
    root = Path(__file__).resolve().parents[1] / "agents/openclaw"
    soul = (root / "SOUL.md").read_text().lower()
    skill = (root / "skills/research-sweep/SKILL.md").read_text().lower()
    for text in (soul, skill):
        # A denial is not permanent, and a human cannot answer in 200ms.
        assert "retry" in text
        assert "opened" in text or "opens" in text


def test_shipped_baseline_policy_scopes_binaries():
    """A baseline without `binaries` loads cleanly and grants nothing.

    OpenShell resolves rules per calling binary, so every endpoint in the
    preset is refused at connect time unless the fetching binary is listed.
    The failure is silent - the preset applies, the policy shows the host,
    and every request still returns 000 - so it is worth a test.
    """
    policy = (Path(__file__).resolve().parents[1] / "agents/openclaw/policy.yaml").read_text()
    assert "binaries:" in policy, "baseline policy grants nothing without a binaries block"
    for expected in ("/usr/local/bin/openclaw", "/usr/local/bin/node"):
        assert expected in policy


def test_scope_change_callback_fires():
    changes = []
    scope = ResearchScope(on_change=lambda host, allowed: changes.append((host, allowed)))
    scope.allow("arxiv.org")
    scope.deny("sketchy.example")
    assert changes == [("arxiv.org", True), ("sketchy.example", False)]
