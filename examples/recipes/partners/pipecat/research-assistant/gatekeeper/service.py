# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Turns blocked research sources into questions, and answers into policy.

This is the seam between a stalled agent and a human who is not watching a
terminal. It owns no transport: the voice bot drives it, and so do the tests
and the console. Nothing in this module knows that audio exists.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from .approver import ApprovalOutcome, PolicyApprover, UnsafeEndpoint
from .audit import SourceLedger
from .denials import Deduplicator, Denial, DenialWatcher, ResearchScope

Choice = Literal["approved", "rejected", "unclear"]

# What the agent is told once a source it wanted is finally open.
#
# Without this the approval is wasted. Observed live: the agent hit arxiv.org
# four times in 200ms, treated the denials as final, and finished its run seven
# seconds before the operator said yes. The policy changed correctly and no
# longer mattered, because nothing told the agent the world had moved.
#
# This lives here rather than with the Pipecat worker that sends it: it is what
# the gatekeeper wants said, and keeping it here means `gatekeeper/` stays
# importable - and testable - without Pipecat installed.
RESUME_TEMPLATE = (
    "{host} is now open to you. Fetch it and continue the research you were "
    "already doing; do not start over, and do not repeat work you have "
    "finished. If you had set that line of inquiry aside because it was "
    "blocked, pick it back up now and fold what you find into your answer."
)

_AFFIRMATIVE = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "approve", "approved",
    "allow", "allow it", "do it", "go ahead", "go", "fine", "open it", "let it",
}
_NEGATIVE = {
    "no", "nope", "nah", "deny", "reject", "block", "don't", "dont", "skip",
    "no thanks", "leave it", "not that one",
}


def interpret(text: str) -> Choice:
    """Read a spoken answer conservatively.

    Only an unambiguous affirmative opens a network endpoint. Everything else
    - including silence, a question, or a half-sentence - leaves the policy
    exactly as it was. Widening egress is the thing this whole recipe exists to
    make deliberate, so an unclear answer must never be the one that does it.
    """
    normalized = " ".join(text.lower().strip().split()).rstrip(".!?").strip()
    if not normalized:
        return "unclear"
    for phrases, choice in ((_NEGATIVE, "rejected"), (_AFFIRMATIVE, "approved")):
        for phrase in phrases:
            if normalized == phrase or normalized.startswith((f"{phrase} ", f"{phrase},")):
                return choice
    return "unclear"


@dataclass
class Question:
    """One pending decision about one source."""

    denial: Denial
    future: asyncio.Future[Choice]
    asked_at: float = 0.0

    @property
    def host(self) -> str:
        return self.denial.host

    @property
    def prompt(self) -> str:
        return f"The research agent wants to read {self.denial.host}. Should I allow it?"

    @property
    def detail(self) -> str:
        return (
            f"{self.denial.binary} was blocked reaching {self.denial.endpoint}. "
            f"Approving opens that host read-only, for that binary only, "
            f"for the life of this sandbox."
        )


@dataclass
class GatekeeperEvent:
    kind: Literal["asked", "resolved", "auto", "error"]
    text: str
    data: dict[str, Any] = field(default_factory=dict)


class Gatekeeper:
    """Watches for blocked sources, asks a human, applies the answer."""

    def __init__(
        self,
        watcher: DenialWatcher,
        approver: PolicyApprover,
        *,
        scope: ResearchScope | None = None,
        auto_allow: frozenset[str] = frozenset(),
        since: float | None = None,
    ):
        self.watcher = watcher
        self.approver = approver
        self.scope = scope or ResearchScope()
        self.auto_allow = auto_allow
        # `openshell logs --tail` replays recent history on attach. Denials
        # from before this run are already-answered questions from a previous
        # session, and re-asking them buries the live one.
        self.since = since
        self.events: asyncio.Queue[GatekeeperEvent] = asyncio.Queue()
        # What the agent actually reached, so an answer's cited sources can be
        # checked against reality rather than taken on trust.
        self.ledger = SourceLedger()
        self._asked: Question | None = None
        self._waiting: list[Question] = []
        self._seen = Deduplicator()

    @property
    def pending(self) -> tuple[Question, ...]:
        """The outstanding question first, then anything queued behind it."""
        return tuple(q for q in (self._asked, *self._waiting) if q is not None)

    @property
    def asked(self) -> Question | None:
        """The one question a human has actually been asked."""
        return self._asked

    async def run(self) -> None:
        """Consume gateway traffic until the watcher stops.

        Allowed fetches are recorded and otherwise ignored; denials become
        questions. Watching both is what makes the ledger possible.
        """
        events = getattr(self.watcher, "network_events", None)
        if events is None:  # a watcher that only yields denials
            async for denial in self.watcher.denials():
                self.ledger.record(denial.host, allowed=False)
                await self._handle(denial)
            return

        async for event in events():
            self.ledger.record(event.host, allowed=event.allowed)
            denial = event.as_denial()
            if denial is not None:
                await self._handle(denial)

    async def _handle(self, denial: Denial) -> None:
        # Replayed history is not a live request for permission.
        if self.since is not None and denial.timestamp < self.since:
            return
        # Retries of an already-decided endpoint must not re-ask.
        if not self._seen.is_new(denial):
            return
        if denial.host in self.auto_allow:
            self._seen.record(denial, "auto")
            await self._apply(denial, "approved", automatic=True)
            return

        self._seen.record(denial, "pending")
        future: asyncio.Future[Choice] = asyncio.get_running_loop().create_future()
        question = Question(denial=denial, future=future, asked_at=denial.timestamp)
        self._waiting.append(question)
        await self._ask_next()

    async def _ask_next(self) -> None:
        """Put exactly one question to the human at a time.

        Asking several at once is how a spoken answer ends up attached to the
        wrong host: the listener hears the last question and the queue resolves
        the first. One outstanding question means "yes" is never ambiguous.
        """
        if self._asked is not None or not self._waiting:
            return
        question = self._waiting.pop(0)
        self._asked = question
        await self.events.put(
            GatekeeperEvent(
                "asked",
                question.prompt,
                {
                    "host": question.denial.host,
                    "endpoint": question.denial.endpoint,
                    "detail": question.detail,
                    "binary": question.denial.binary,
                    "reason": question.denial.reason,
                },
            )
        )

    async def answer(self, text: str, host: str | None = None) -> ApprovalOutcome | None:
        """Apply a spoken answer to the question that was actually asked.

        `host` is what the caller believes it asked about. If that disagrees
        with the outstanding question, nothing is opened. Consent is given for
        a specific source, and applying it to a different one - even one the
        person would probably also have approved - is not an approval, it is a
        substitution.
        """
        question = self._asked
        if question is None:
            return None

        if host is not None and host != question.host:
            await self.events.put(
                GatekeeperEvent(
                    "error",
                    (
                        f"I was asked about {question.host} but the answer came "
                        f"back for {host}, so nothing was opened."
                    ),
                    {"asked_about": question.host, "answered_about": host,
                     "refused": True},
                )
            )
            return None

        choice = interpret(text)
        if choice == "unclear":
            await self.events.put(
                GatekeeperEvent(
                    "error",
                    "I did not catch a clear yes or no, so nothing was opened.",
                    {"heard": text, "host": question.host},
                )
            )
            return None

        self._asked = None
        self._seen.record(question.denial, choice)
        if not question.future.done():
            question.future.set_result(choice)
        outcome = await self._apply(question.denial, choice)
        await self._ask_next()
        return outcome

    async def _apply(
        self, denial: Denial, choice: Choice, *, automatic: bool = False
    ) -> ApprovalOutcome | None:
        if choice != "approved":
            self.scope.deny(denial.host)
            await self.events.put(
                GatekeeperEvent(
                    "resolved",
                    f"Left {denial.host} blocked.",
                    {"host": denial.host, "choice": choice},
                )
            )
            return None

        try:
            outcome = await self.approver.allow(denial.host, denial.port, denial.binary)
        except UnsafeEndpoint as exc:
            # An approval that must not be honoured. Say so out loud rather
            # than failing quietly, because the human believes they said yes.
            await self.events.put(
                GatekeeperEvent(
                    "error",
                    f"I cannot open {denial.host}: {exc}",
                    {"host": denial.host, "refused": True},
                )
            )
            return None

        if outcome.applied:
            self.scope.allow(denial.host)
        await self.events.put(
            GatekeeperEvent(
                "auto" if automatic else "resolved",
                (
                    f"Opened {denial.host} read-only."
                    if outcome.applied
                    else f"Could not open {denial.host}: {outcome.detail}"
                ),
                {"host": denial.host, "applied": outcome.applied, "automatic": automatic},
            )
        )
        return outcome

    async def stream(self) -> AsyncIterator[GatekeeperEvent]:
        while True:
            yield await self.events.get()
