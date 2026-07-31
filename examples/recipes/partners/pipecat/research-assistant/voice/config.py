# SPDX-FileCopyrightText: Copyright (c) 2026 Daily and the Pipecat authors. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Vendored from https://github.com/chadbailey59/agent-voice-bot at commit
# de9d6de. Pinned on purpose: this recipe owns its copy so its behavior
# does not change underneath it when upstream moves.

"""Configuration for the bot.

Two axes, both deliberately narrow:

- The agent loop is always an OpenClaw agent reached through a NemoClaw
  sandbox's Gateway websocket.
- The voice loop is one of two profiles. `hosted` runs entirely on third-party
  APIs; `local` runs entirely on NVIDIA NIMs you host yourself. The profile
  picks STT, LLM, and TTS together — mixing them is not a supported shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

MAIN_WORKER = "main"
AGENT_LOOP_WORKER = "agent-loop"

Profile = Literal["hosted", "local"]

# --- hosted profile defaults ---------------------------------------------
# Nemotron on Baseten's Model APIs. Nano and Super are served from dedicated
# deployments whose slugs come from the Baseten dashboard; Ultra is the one
# slug the shared Model APIs endpoint exposes by name, so it is the default a
# fresh BASETEN_API_KEY can actually call.
DEFAULT_BASETEN_MODEL = "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B"
DEFAULT_BASETEN_BASE_URL = "https://inference.baseten.co/v1"
# Gradium's own default voice. Named here so the value is visible in config
# rather than buried in the service's constructor.
DEFAULT_GRADIUM_VOICE = "_6Aslh2DxfmnRLmP"

# --- local profile defaults ----------------------------------------------
# Self-hosted NVIDIA Riva/NIM speech. Each NIM serves gRPC on 50051 in its own
# container, so ASR and TTS only share a port when they are on separate hosts;
# the TTS default assumes both run on one box with the port remapped.
DEFAULT_NVIDIA_ASR_SERVER = "localhost:50051"
DEFAULT_NVIDIA_TTS_SERVER = "localhost:50052"

# Riva picks the acoustic model at NIM deploy time (CONTAINER_ID and
# NIM_TAGS_SELECTOR), and the client sends an empty model name. These names
# therefore only label metrics and logs; changing one does not reroute audio to
# a different model.
#
# The ASR default names what a parakeet-1-1b-ctc-en-us NIM deployed with
# `mode=str` reports back. Not every Parakeet NIM can stream: parakeet-0.6b-tdt
# ships offline-only profiles and cannot serve this pipeline at all.
DEFAULT_NVIDIA_ASR_MODEL = "parakeet-1.1b-en-US-asr-streaming"
DEFAULT_NVIDIA_TTS_MODEL = "magpie-tts-multilingual"

# The voice loop only ever decides "answer now" or "hand this to the agent
# loop", so a Nano-class Nemotron is the right size. This points at a NIM you
# run on the DGX, not at NVIDIA's cloud endpoint.
DEFAULT_NVIDIA_LLM_BASE_URL = "http://localhost:8000/v1"
DEFAULT_NVIDIA_LLM_MODEL = "nvidia/nvidia-nemotron-3-nano"

PLAIN_SPOKEN_OUTPUT_INSTRUCTION = (
    "Use plain spoken text only. Do not use markdown, bullets, numbered lists, "
    "code fences, backticks, asterisks, emojis, links, citations, or special "
    "formatting characters."
)

# Appended to everything forwarded to the agent. The agent's answer is spoken
# aloud, so it has to come back as one short plain-text reply rather than the
# formatted, question-ending output a coding agent would normally produce.
# The upstream bot tells the agent to answer concisely and immediately, which
# is right for a general voice assistant and wrong here: it produces an answer
# from the model's own memory in two seconds, never touches a source, and never
# reaches the policy boundary this recipe exists to demonstrate.
AGENT_LOOP_INSTRUCTION = (
    "This research request was forwarded from a voice loop. Use the "
    "research-sweep skill and actually consult sources: this is a research "
    "task, not a recall task. Do not answer from what you already know. Even "
    "when you are confident, verify against primary sources and cite what you "
    "used. Taking twenty minutes is expected and correct; answering in two "
    "seconds means you did not do the work.\n\n"
    "Fetches to sources outside your policy will be blocked. That is the "
    "boundary between research you are authorized to do and research you are "
    "not. Note the blocked source, move to another line of inquiry, and keep "
    "going; your operator is asked out loud whether to open it. Never retry in "
    "a loop and never seek the same content through a mirror, cache, archive, "
    "proxy, or search snapshot.\n\n"
    "When you are done, return one final answer for the user, naming the "
    "sources it rests on and stating plainly anything you could not verify "
    "because a source stayed closed. If you cannot determine the answer, say "
    "so instead of guessing. Do not ask a follow-up question, offer to do more "
    f"work, or add a call to action. {PLAIN_SPOKEN_OUTPUT_INSTRUCTION}"
)


VOICE_LOOP_SYSTEM_PROMPT = """\
You are the voice loop for a research assistant.

The user is speaking, so keep responses concise and natural. Answer simple,
low-risk questions directly when you can do so immediately. For any task that
requires research, tool use, long planning, code changes, file or web access,
multi-step execution, integration with an external agent, or waiting on slow
work, call send_to_agent_loop instead of trying to solve it yourself.

Use send_to_agent_loop both to start new agent work and to forward a follow-up,
correction, or refinement while agent work is already running — just pass along
what the user said and let the agent loop sort out the rest. While agent work
runs, keep answering simple questions directly. If the user wants to abort the
running work, call stop_agent_loop.

When you forward work, say only a very short acknowledgement of one to four
words, such as "One sec.", "Hang on.", or "On it." Do not add filler, status
details, or calls to action to delegation acknowledgements.

If an agent-loop result arrives later in a developer message, summarize it
conversationally. Do not expose internal worker names unless the user asks
about the architecture.

Sometimes the research agent is blocked from reading a source and needs the
user's permission. You will be told to ask about it. When that happens, ask the
question and then stop talking. When the user answers, call
answer_policy_question with what they said.

That decision is the user's alone. Never call answer_policy_question with an
answer they did not give: not from silence, not from hesitation, not from "I
guess so" if you are unsure, and never because the source seems obviously fine
to you. If they ask a clarifying question, answer it and wait. If they change
the subject, let the question stand and come back to it. Approving a source
widens what the agent can reach for the rest of the session, so a guess is
worse than a delay.

Use plain spoken text only. Do not use markdown, bullets, numbered lists, code
fences, backticks, asterisks, emojis, links, citations, or special formatting
characters.
"""


@dataclass(frozen=True)
class OpenClawConfig:
    """Connection settings for the OpenClaw Gateway in a NemoClaw sandbox.

    The default port is the one a NemoClaw sandbox publishes, not OpenClaw's
    own 18789 — this bot always goes through the sandbox.
    """

    gateway_url: str = "ws://127.0.0.1:18790"
    token: str | None = None
    password: str | None = None
    session_key: str = "agent:main:main"
    timeout_secs: float = 300.0

    @classmethod
    def from_env(cls) -> OpenClawConfig:
        return cls(
            gateway_url=os.getenv("OPENCLAW_GATEWAY_URL", "ws://127.0.0.1:18790"),
            token=os.getenv("OPENCLAW_TOKEN"),
            password=os.getenv("OPENCLAW_PASSWORD"),
            session_key=os.getenv("OPENCLAW_SESSION_KEY", "agent:main:main"),
            timeout_secs=float(os.getenv("OPENCLAW_TIMEOUT_SECS", "300")),
        )


@dataclass(frozen=True)
class AppConfig:
    profile: Profile = "hosted"
    agent: OpenClawConfig = field(default_factory=OpenClawConfig)

    @classmethod
    def from_env(cls) -> AppConfig:
        profile = os.getenv("VOICE_PROFILE", "hosted").strip().lower()
        if profile not in {"hosted", "local"}:
            raise ValueError(
                f"VOICE_PROFILE must be 'hosted' or 'local', got {profile!r}"
            )
        return cls(profile=profile, agent=OpenClawConfig.from_env())
