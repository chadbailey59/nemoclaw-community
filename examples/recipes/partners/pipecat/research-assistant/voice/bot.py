# SPDX-FileCopyrightText: Copyright (c) 2026 Daily and the Pipecat authors. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Vendored from https://github.com/chadbailey59/agent-voice-bot at commit
# de9d6de. Pinned on purpose: this recipe owns its copy so its behavior
# does not change underneath it when upstream moves.

"""Pipecat bot: a fast voice loop in front of an OpenClaw agent loop."""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.bus.messages import BusJobResponseMessage, BusJobUpdateMessage
from pipecat.evals.transport import EvalTransportParams
from pipecat.frames.frames import (
    FunctionCallResultProperties,
    LLMMessagesAppendFrame,
    LLMRunFrame,
)
from pipecat.pipeline.job_context import JobStatus
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.run import main
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from voice.agent_worker import AgentWorker
from voice.config import (
    AGENT_LOOP_WORKER,
    MAIN_WORKER,
    PLAIN_SPOKEN_OUTPUT_INSTRUCTION,
    AppConfig,
)
from voice.gatekeeper_worker import GATEKEEPER_WORKER, GatekeeperWorker
from voice.openclaw import OpenClawRuntime
from voice.voice import build_voice_stack

from gatekeeper.approver import PolicyApprover
from gatekeeper.denials import DenialWatcher
from gatekeeper.service import Gatekeeper

if os.getenv("AGENT_VOICE_SKIP_DOTENV") != "1":
    # override=False so a loaded profile (or anything already exported by the
    # shell) wins; .env only fills in variables that are otherwise unset.
    load_dotenv(override=False)

transport_params = {
    "eval": lambda: EvalTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
    # Pipecat's runner maps the "webrtc" transport key to SmallWebRTC.
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}


@tool_options(cancel_on_interruption=False, timeout_secs=5)
async def send_to_agent_loop(  # noqa: D417 — `params` is framework plumbing, not tool-schema Args
    params: FunctionCallParams, user_input: str):
    """Forward a user request or follow-up to the agent loop.

    Use this for anything you can't answer immediately yourself: research,
    tool use, multi-step work, code/file/web access, or external agents.
    Use it BOTH to start new agent work and to pass along a follow-up,
    correction, or refinement while agent work is already running — just
    forward what the user said. The agent loop decides whether that input
    starts a new task or steers the running one; you do not.

    After forwarding, say only a very short acknowledgement of one to four
    words, such as "One sec.", "Hang on.", or "On it." Do not add filler,
    status details, or calls to action. Results arrive later and you'll
    relay them.

    Args:
        user_input: What the user wants done or wants to add, preserving
            important details.
    """
    job_id = await params.pipeline_worker.request_job(
        AGENT_LOOP_WORKER,
        name="run",
        payload={"input": user_input},
        timeout=None,
    )
    logger.info(f"Forwarded to agent loop as job {job_id}: {user_input!r}")
    await params.result_callback(
        {"status": "sent"},
        properties=FunctionCallResultProperties(run_llm=True),
    )


@tool_options(cancel_on_interruption=False, timeout_secs=30)
async def answer_policy_question(  # noqa: D417 — `params` is framework plumbing, not tool-schema Args
    params: FunctionCallParams, answer: str):
    """Relay the user's decision about a blocked research source.

    Use this ONLY when the research agent has asked whether to open a source
    and the user has given a clear yes or no. Pass along what they actually
    said - do not decide for them, do not infer a yes from hesitation, and do
    not call this if they asked a clarifying question instead of answering.

    Approving opens that one host, read-only, for the life of the sandbox.

    Args:
        answer: What the user said, verbatim enough to tell yes from no.
    """
    job_id = await params.pipeline_worker.request_job(
        GATEKEEPER_WORKER,
        name="answer",
        payload={"answer": answer},
        timeout=None,
    )
    logger.info(f"Relayed policy answer as job {job_id}: {answer!r}")
    await params.result_callback(
        {"status": "sent"},
        properties=FunctionCallResultProperties(run_llm=False),
    )


@tool_options(cancel_on_interruption=False, timeout_secs=5)
async def stop_agent_loop(  # noqa: D417 — `params` is framework plumbing, not tool-schema Args
    params: FunctionCallParams, reason: str):
    """Stop or cancel the agent work currently running in the agent loop.

    Use this when the user wants to abort, cancel, or call off the task the
    agent loop is working on. This is preemptive — it tries to halt the work
    now, not queue another instruction. Whether a given backend can truly
    preempt is backend-specific; if nothing is running, say so.

    Args:
        reason: Why the user wants to stop (brief).
    """
    job_id = await params.pipeline_worker.stop_active_agent_job(reason)
    if job_id is None:
        await params.result_callback(
            {"status": "nothing_running"},
            properties=FunctionCallResultProperties(run_llm=True),
        )
        return
    # The worker replies with a CANCELLED response; narrate from there.
    await params.result_callback(
        {"status": "stopping", "job_id": job_id},
        properties=FunctionCallResultProperties(run_llm=False),
    )


@tool_options(cancel_on_interruption=False)
async def end_conversation(  # noqa: D417 — `params` is framework plumbing, not tool-schema Args
    params: FunctionCallParams, reason: str):
    """End the conversation when the user clearly says goodbye.

    Args:
        reason: Why the conversation is ending.
    """
    await params.pipeline_worker.say_goodbye_and_end(
        reason=reason,
        result_callback=params.result_callback,
    )


class VoiceBotWorker(PipelineWorker):
    """Pipeline worker that is both the media path and the voice loop.

    The inline voice LLM makes exactly one judgment per turn: answer the user
    directly, or forward the input to the agent loop with the tools above. It
    does NOT decide whether a forward starts a new task or refines a running
    one — the agent loop owns that, and all other backend-specific variance.
    This worker keeps a single job handle (learned from the agent loop) so it
    can stop in-flight work and narrate status honestly.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The agent loop's current active task, as reported back to us. Used to
        # target stop_agent_loop and to clear state when work finishes.
        self._active_job_id: str | None = None
        # The host the user is currently being asked about, if any. Kept so a
        # late or ambiguous answer can be attributed to the right question.
        self._pending_policy_host: str | None = None

    async def stop_active_agent_job(self, reason: str) -> str | None:
        """Cancel the active agent-loop job, returning its id, or None if idle."""
        if self._active_job_id is None:
            return None
        job_id = self._active_job_id
        logger.info(f"Stopping agent-loop job {job_id}: {reason!r}")
        await self.cancel_job_group(job_id, reason=reason)
        return job_id

    async def say_goodbye_and_end(self, *, reason: str, result_callback) -> None:
        """Speak a brief goodbye, resolve the tool call, and end the session."""
        await self.queue_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "developer", "content": "Say goodbye briefly."}],
                run_llm=True,
            )
        )
        await self.flush_pipeline()
        await result_callback(None, properties=FunctionCallResultProperties(run_llm=False))
        await self.flush_pipeline()
        await self.end(reason=reason)

    async def on_job_update(self, message: BusJobUpdateMessage) -> None:
        await super().on_job_update(message)

        if message.source == GATEKEEPER_WORKER:
            # A research source was blocked, or a decision about one landed.
            # The gatekeeper has already composed how this should be spoken.
            update = message.update or {}
            if update.get("kind") == "asked":
                self._pending_policy_host = update.get("host")
            else:
                self._pending_policy_host = None
            await self.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "developer", "content": update.get("content", "")}],
                    run_llm=True,
                )
            )
            return

        if message.source != AGENT_LOOP_WORKER:
            return
        update = message.update or {}
        if update.get("kind") == "started":
            # The agent loop accepted this as the active, cancellable task.
            # The send tool already acked verbally, so just record the handle.
            self._active_job_id = message.job_id
            logger.info(f"Agent loop started job {message.job_id}")

    async def on_job_response(self, message: BusJobResponseMessage) -> None:
        await super().on_job_response(message)

        if message.source == GATEKEEPER_WORKER:
            response = message.response or {}
            if response.get("kind") == "no_question":
                content = (
                    "There was no source waiting on approval, so nothing "
                    "changed. Tell the user that briefly. "
                    f"{PLAIN_SPOKEN_OUTPUT_INSTRUCTION}"
                )
            elif response.get("applied"):
                content = (
                    f"{response.get('host')} is now open to the research agent, "
                    "which has resumed. Say so in one short sentence. "
                    f"{PLAIN_SPOKEN_OUTPUT_INSTRUCTION}"
                )
            else:
                # Covers a spoken "no" and a refused or failed approval. Never
                # imply a source opened when the policy did not actually change.
                content = (
                    f"{response.get('host')} was left blocked. Say so in one "
                    f"short sentence. Backend note: {response.get('status', '')} "
                    f"{PLAIN_SPOKEN_OUTPUT_INSTRUCTION}"
                )
            await self.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "developer", "content": content}],
                    run_llm=True,
                )
            )
            return

        if message.source != AGENT_LOOP_WORKER:
            return

        response = message.response or {}
        kind = response.get("kind")
        finished_active = message.job_id == self._active_job_id

        if message.status == JobStatus.CANCELLED:
            if finished_active:
                self._active_job_id = None
            content = (
                "The agent task was stopped. Tell the user it's cancelled. "
                f"{PLAIN_SPOKEN_OUTPUT_INSTRUCTION}"
            )
        elif message.status != JobStatus.COMPLETED or kind == "error":
            if finished_active:
                self._active_job_id = None
            content = (
                "The agent task did not finish successfully. Tell the user it "
                f"failed (status {message.status}). "
                f"{PLAIN_SPOKEN_OUTPUT_INSTRUCTION}"
            )
        elif kind == "steering":
            # A follow-up reached the agent loop while a task was running.
            if response.get("applied") is False:
                content = (
                    "The user's follow-up reached the agent loop, but this "
                    "backend could not apply it to the active run. Briefly tell "
                    "the user the current task is still running and they may need "
                    f"to stop and resend the update. Backend note: {response.get('status', '')}"
                    f" {PLAIN_SPOKEN_OUTPUT_INSTRUCTION}"
                )
            else:
                # OpenClaw interrupts the running turn and restarts on the
                # update rather than merging into it, so do not tell the user
                # their note was added to work already in progress.
                content = (
                    "The agent has switched to the user's update and is working "
                    "on it now. Briefly acknowledge that, in a few words. Do not "
                    "imply the earlier version is still being worked on. "
                    f"{PLAIN_SPOKEN_OUTPUT_INSTRUCTION}"
                )
        else:  # final result
            if finished_active:
                self._active_job_id = None
            content = (
                "Agent-loop result is ready. Turn it into one concise spoken "
                "answer for the user, keeping any specific codes, numbers, or "
                "names accurate. If the result says it cannot determine the "
                "answer, state that limitation clearly. Do not add a follow-up "
                "question, offer, or call to action. "
                f"{PLAIN_SPOKEN_OUTPUT_INSTRUCTION} "
                f"Result: {response.get('summary', response)}"
            )

        await self.queue_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "developer", "content": content}],
                run_llm=True,
            )
        )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    logger.info("Starting agent-voice-bot")

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)

    config = AppConfig.from_env()
    logger.info(f"Voice profile: {config.profile}")
    voice = build_voice_stack(config.profile)

    context = LLMContext(
        tools=[send_to_agent_loop, answer_policy_question, stop_agent_loop, end_conversation]
    )
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=voice.vad),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            voice.stt,
            aggregators.user(),
            voice.llm,
            voice.tts,
            transport.output(),
            aggregators.assistant(),
        ]
    )

    main_worker = VoiceBotWorker(
        pipeline,
        name=MAIN_WORKER,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    sandbox = os.getenv("NEMOCLAW_SANDBOX", "nc")
    watcher = DenialWatcher(sandbox, openshell=os.getenv("OPENSHELL_BIN", "openshell"))
    keeper = Gatekeeper(
        watcher,
        PolicyApprover(sandbox, openshell=os.getenv("OPENSHELL_BIN", "openshell")),
        auto_allow=frozenset(
            host.strip()
            for host in os.getenv("RESEARCH_AUTO_ALLOW", "").split(",")
            if host.strip()
        ),
    )
    gatekeeper_worker = GatekeeperWorker(keeper)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        context.add_message(
            {
                "role": "developer",
                "content": (
                    "Greet the user briefly. Say you can start a research sweep "
                    "in the background, and that you will interrupt them if the "
                    "agent needs a source approved."
                ),
            }
        )
        await main_worker.queue_frame(LLMRunFrame())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await watcher.close()
        await runner.cancel()

    await runner.add_workers(
        AgentWorker(OpenClawRuntime(config.agent)),
        gatekeeper_worker,
        main_worker,
    )

    # Open the long-lived watch job so blocked sources reach the voice loop for
    # the whole session, not just while a research run happens to be active.
    await watcher.__aenter__()
    await main_worker.request_job(GATEKEEPER_WORKER, name="watch", timeout=None)

    try:
        await runner.run()
    finally:
        await watcher.close()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point compatible with Pipecat Cloud."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


def cli_main():
    """Console script entry point that keeps Pipecat runner discovery working."""
    sys.modules["__main__"].bot = bot
    main()


if __name__ == "__main__":
    main()
