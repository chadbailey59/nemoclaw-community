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

from gatekeeper.service import Gatekeeper

GATEKEEPER_WORKER = "gatekeeper"

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

RESOLVED_INSTRUCTION = (
    "Tell the user this outcome in one short sentence, then stop. "
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
                if event.kind == "asked":
                    logger.info(f"Awaiting approval: {event.data.get('endpoint')}")
                    content = f"{ASK_INSTRUCTION} Question: {event.text}"
                else:
                    content = f"{RESOLVED_INSTRUCTION} Outcome: {event.text}"
                await self.send_job_update(
                    message.job_id,
                    {
                        "kind": event.kind,
                        "content": content,
                        "host": event.data.get("host"),
                        "detail": event.data.get("detail", ""),
                    },
                    # A stalled agent is worth cutting into whatever the voice
                    # loop was saying; an outcome report is not.
                    urgent=event.kind == "asked",
                )
        except asyncio.CancelledError:
            raise
        finally:
            if self._watcher is not None:
                self._watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await self._watcher

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
        await self.send_job_response(
            message.job_id,
            {
                "kind": "answered",
                "host": host,
                "applied": bool(outcome and outcome.applied),
                "status": outcome.detail if outcome else "Left it blocked.",
            },
            urgent=True,
        )

    async def _consume_denials(self) -> None:
        try:
            await self._keeper.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"Stopped watching for blocked sources: {exc}")
