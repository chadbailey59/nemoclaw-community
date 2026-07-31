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
from .denials import Deduplicator, Denial, DenialWatcher, ResearchScope

Choice = Literal["approved", "rejected", "unclear"]

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
    ):
        self.watcher = watcher
        self.approver = approver
        self.scope = scope or ResearchScope()
        self.auto_allow = auto_allow
        self.events: asyncio.Queue[GatekeeperEvent] = asyncio.Queue()
        self._questions: list[Question] = []
        self._seen = Deduplicator()

    @property
    def pending(self) -> tuple[Question, ...]:
        return tuple(self._questions)

    async def run(self) -> None:
        """Consume denials until the watcher stops."""
        async for denial in self.watcher.denials():
            await self._handle(denial)

    async def _handle(self, denial: Denial) -> None:
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
        self._questions.append(question)
        await self.events.put(
            GatekeeperEvent(
                "asked",
                question.prompt,
                {
                    "host": denial.host,
                    "endpoint": denial.endpoint,
                    "detail": question.detail,
                    "binary": denial.binary,
                    "reason": denial.reason,
                },
            )
        )

    async def answer(self, text: str) -> ApprovalOutcome | None:
        """Apply a spoken answer to the oldest pending question."""
        if not self._questions:
            return None
        choice = interpret(text)
        if choice == "unclear":
            await self.events.put(
                GatekeeperEvent(
                    "error",
                    "I did not catch a clear yes or no, so nothing was opened.",
                    {"heard": text},
                )
            )
            return None

        question = self._questions.pop(0)
        self._seen.record(question.denial, choice)
        if not question.future.done():
            question.future.set_result(choice)
        return await self._apply(question.denial, choice)

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
