# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bridges the policy boundary into the voice loop.

The research agent runs for a long time and the operator is not watching it.
When OpenShell blocks a source, this worker turns that into a spoken question,
and turns the spoken answer back into a policy decision.

It is deliberately the only place the two halves meet. The gatekeeper knows
nothing about audio; the voice loop knows nothing about OpenShell. Both stay
independently testable, which is what keeps the boundary honest.

The voice loop opens one long-lived `watch` job here at startup. Questions
arrive back as job updates, which is the same path the agent loop already uses
to report progress, so nothing new had to be invented to carry them.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from loguru import logger
from pipecat.bus.messages import BusJobRequestMessage
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker

from gatekeeper.audit import audit_answer
from gatekeeper.service import RESUME_TEMPLATE, Gatekeeper
from voice.config import AGENT_LOOP_WORKER

GATEKEEPER_WORKER = "gatekeeper"

# AgentWorker.run does the right thing with RESUME_TEMPLATE in both states: it
# steers a run that is still going, and starts a fresh one in the same session
# if the agent has already stopped. OpenClaw session continuity means a new run
# still has the earlier research in context, so it reads as "carry on" rather
# than "start over". The text itself lives in gatekeeper.service, which stays
# importable without Pipecat.

# The voice model is told how to speak the question and, more importantly,
# what it must not do with it: it relays a decision, it does not make one.
ASK_INSTRUCTION = (
    "The research agent has been blocked from reading a source and is waiting "
    "on a decision. Read the question to the user in one short sentence, then "
    "stop talking. Do not guess what the user would prefer, do not answer on "
    "their behalf, and do not offer to decide for them. If they ask what the "
    "source is, say the host name plainly. Use plain spoken text only: no "
    "markdown, no bullets, and do not spell out URLs character by character."
)

# Only reached when something did not go the way the operator asked. A
# successful approval is silent: they said yes, it opened, the agent carried
# on, and there is nothing to report.
PROBLEM_INSTRUCTION = (
    "Something did not go the way the user asked. Tell them in one short "
    "sentence, then stop. Do not soften it and do not imply the source is "
    "usable if it is not. If the problem is that you did not understand their "
    "answer, ask the question again plainly as a yes or no. "
    "Use plain spoken text only."
)


class GatekeeperWorker(BaseWorker):
    """Runs the gatekeeper and reports its questions to the voice loop."""

    def __init__(self, keeper: Gatekeeper):
        super().__init__(GATEKEEPER_WORKER)
        self._keeper = keeper
        self._watcher: asyncio.Task | None = None

    @job(name="watch")
    async def watch(self, message: BusJobRequestMessage) -> None:
        """Long-lived job: stream approval questions to the voice loop."""
        self._watcher = asyncio.create_task(self._consume_denials())
        try:
            while True:
                event = await self._keeper.events.get()
                if event.kind == "auto":
                    # Pre-approved hosts are not worth interrupting a human for.
                    logger.info(f"Auto-allowed without asking: {event.text}")
                    continue
                if event.kind == "resolved" and event.data.get("expected"):
                    # It did what was just asked for. Saying "that worked"
                    # after every approval is noise, and noise is how someone
                    # learns to stop listening to the one report that matters.
                    logger.info(f"As expected: {event.text}")
                    continue
                if event.kind == "asked":
                    logger.info(f"Awaiting approval: {event.data.get('endpoint')}")
                    content = f"{ASK_INSTRUCTION} Question: {event.text}"
                else:
                    logger.warning(f"Unexpected outcome: {event.text}")
                    content = f"{PROBLEM_INSTRUCTION} Problem: {event.text}"
                await self.send_job_update(
                    message.job_id,
                    {
                        "kind": event.kind,
                        "content": content,
                        "host": event.data.get("host"),
                        "detail": event.data.get("detail", ""),
                    },
                    # Everything here goes out urgently so it keeps the order
                    # the gatekeeper produced it in. Marking only questions
                    # urgent let the next question overtake the previous
                    # answer's outcome: observed live, the bot asked about
                    # developer.nvidia.com five seconds before reporting that
                    # duckduckgo had opened, which reads as an answer to the
                    # wrong question.
                    urgent=True,
                )
        except asyncio.CancelledError:
            raise
        finally:
            if self._watcher is not None:
                self._watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await self._watcher

    @job(name="audit")
    async def audit(self, message: BusJobRequestMessage) -> None:
        """Check an answer's cited sources against what was actually fetched."""
        payload = message.payload or {}
        result = audit_answer(str(payload.get("answer", "")), self._keeper.ledger)
        if not result.clean:
            logger.warning(
                f"Answer credits sources never fetched: {', '.join(result.unverified)}"
            )
        await self.send_job_response(
            message.job_id,
            {
                "kind": "audit",
                "clean": result.clean,
                "warning": result.spoken_warning(),
                "cited": list(result.cited),
                "consulted": list(result.consulted),
                "unverified": list(result.unverified),
            },
            urgent=True,
        )

    @job(name="answer")
    async def answer(self, message: BusJobRequestMessage) -> None:
        """Apply a spoken answer to the oldest pending question."""
        payload = message.payload or {}
        text = str(payload.get("answer", ""))
        # What the voice loop believes it asked about. The gatekeeper refuses
        # the answer if that disagrees with the outstanding question.
        asked_about = payload.get("host")

        if not self._keeper.pending:
            # Answering a question nobody asked must not silently look like
            # success, or the operator will believe they opened something.
            await self.send_job_response(
                message.job_id,
                {"kind": "no_question", "status": "Nothing is waiting on approval."},
                urgent=True,
            )
            return

        asked = self._keeper.asked
        host = asked.host if asked else None
        outcome = await self._keeper.answer(text, host=asked_about)
        applied = bool(outcome and outcome.applied)

        # An answer that was not understood leaves the same question
        # outstanding. That is a different thing from a refusal and must be
        # said differently: observed live, "Yes. That's fine." was misread and
        # reported back as "developer.nvidia.com remains blocked", so the
        # operator believed their clear approval had been declined.
        not_understood = self._keeper.asked is asked and asked is not None

        if applied and host:
            await self._resume_research(host)

        await self.send_job_response(
            message.job_id,
            {
                "kind": "not_understood" if not_understood else "answered",
                "host": host,
                "applied": applied,
                "resumed": applied,
                "heard": text,
                "status": outcome.detail if outcome else "Left it blocked.",
            },
            urgent=True,
        )

    async def _resume_research(self, host: str) -> None:
        """Send the agent back for a source that just opened."""
        try:
            job_id = await self.request_job(
                AGENT_LOOP_WORKER,
                name="run",
                payload={"input": RESUME_TEMPLATE.format(host=host)},
                timeout=None,
            )
            logger.info(f"Sent the agent back to {host} as job {job_id}")
        except Exception as exc:
            # An approval that opens a source the agent never revisits is a
            # silent no-op, so this failing must be visible.
            logger.exception(f"Approved {host} but could not resume research: {exc}")

    async def _consume_denials(self) -> None:
        try:
            await self._keeper.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"Stopped watching for blocked sources: {exc}")
