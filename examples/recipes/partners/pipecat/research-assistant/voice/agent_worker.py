# SPDX-FileCopyrightText: Copyright (c) 2026 Daily and the Pipecat authors. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Vendored from https://github.com/chadbailey59/agent-voice-bot at commit
# de9d6de. Pinned on purpose: this recipe owns its copy so its behavior
# does not change underneath it when upstream moves.

"""Agent-loop Pipecat worker."""

from __future__ import annotations

import asyncio

from loguru import logger
from pipecat.bus.messages import BusJobRequestMessage
from pipecat.pipeline.job_context import JobStatus
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker

from voice.config import AGENT_LOOP_WORKER
from voice.openclaw import OpenClawRuntime, RunHandle, collect_result

# How a run's outcome reaches the voice loop. The bus status matters as much as
# the kind: the voice loop narrates from `message.status`, so a cancelled or
# failed run reported as COMPLETED would be spoken to the user as an answer.
_TERMINAL_STATUS = {
    "completed": ("final", JobStatus.COMPLETED),
    "cancelled": ("cancelled", JobStatus.CANCELLED),
    "error": ("error", JobStatus.ERROR),
}


class AgentWorker(BaseWorker):
    """Bus worker that owns agent-loop state and routes forwarded input.

    It decides whether forwarded input starts a new task or steers the one
    already running, runs the work through the OpenClaw runtime, and supports
    preemptive cancellation. All agent-side variance lives here; the voice loop
    just forwards.
    """

    def __init__(self, client: OpenClawRuntime):
        super().__init__(AGENT_LOOP_WORKER)
        self._client = client
        self._active_job_id: str | None = None
        self._active_run_handle: RunHandle | None = None

    @job(name="run")
    async def run_agent_loop(self, message: BusJobRequestMessage) -> None:
        payload = message.payload or {}
        user_input = str(payload.get("input", ""))

        # Already busy: this input refines the running task rather than starting
        # a new one. OpenClaw steers the live run, so this reaches the agent
        # mid-task instead of queueing behind it.
        if self._active_job_id is not None:
            logger.info(
                f"Agent loop busy ({self._active_job_id}); treating job "
                f"{message.job_id} as steering: {user_input!r}"
            )
            if self._active_run_handle is None:
                applied = False
                status = "No active backend run handle yet."
            else:
                try:
                    await self._client.send_followup(self._active_run_handle, user_input)
                    applied = True
                    status = "steered"
                except Exception as exc:
                    # Must not escape: this is a separate bus job from the run,
                    # and letting it throw fails the follow-up while the
                    # original task keeps going, with nothing said to the user.
                    logger.exception(f"Could not steer the running job: {exc}")
                    applied = False
                    status = f"The agent could not accept the update: {exc}"
            await self.send_job_response(
                message.job_id,
                {
                    "kind": "steering",
                    "active_job_id": self._active_job_id,
                    "applied": applied,
                    "status": status,
                },
                urgent=True,
            )
            return

        self._active_job_id = message.job_id

        handle: RunHandle | None = None
        try:
            handle = await self._client.start(user_input)
            self._active_run_handle = handle
            # Tell the voice loop which job is now the cancellable active task.
            await self.send_job_update(
                message.job_id,
                {"kind": "started"},
                urgent=True,
            )
            result = await collect_result(self._client.events(handle))
        except asyncio.CancelledError:
            # Cancelled by stop_agent_loop; the bus cancel path replies CANCELLED.
            if handle is not None:
                try:
                    if not await self._client.stop(handle, "Cancelled by the voice loop."):
                        # The agent finished a moment before the user said stop.
                        # Routine, and the result is discarded either way.
                        logger.info(f"Nothing to abort for run {handle.run_id}; already done")
                except Exception:
                    # The local job is cancelled either way. Never let a failed
                    # abort replace the CancelledError: swallowing it would
                    # break cancellation for everything upstream. Log loudly,
                    # because it means the agent may still be running.
                    logger.exception("Could not abort the OpenClaw run after cancellation")
            raise
        except Exception as exc:
            logger.exception(f"Agent-loop job failed: {exc}")
            await self.send_job_response(
                message.job_id,
                {"kind": "error", "error": str(exc)},
                status=JobStatus.ERROR,
                urgent=True,
            )
            return
        finally:
            self._active_job_id = None
            self._active_run_handle = None

        kind, status = _TERMINAL_STATUS[result.status]
        # Deliver urgently so the finished result preempts queued bus traffic
        # and the voice loop can speak it promptly.
        await self.send_job_response(
            message.job_id,
            {"kind": kind, "summary": result.summary},
            status=status,
            urgent=True,
        )
