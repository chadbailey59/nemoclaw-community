# SPDX-FileCopyrightText: Copyright (c) 2026 Daily and the Pipecat authors. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Vendored from https://github.com/chadbailey59/agent-voice-bot at commit
# de9d6de. Pinned on purpose: this recipe owns its copy so its behavior
# does not change underneath it when upstream moves.

"""The two supported voice-loop profiles.

`hosted` and `local` are all-or-nothing. Each builds STT, LLM, and TTS from one
family so there is exactly one deployment story per profile: hosted needs three
API keys and no GPU, local needs three NIMs and no network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer

from voice.config import (
    DEFAULT_BASETEN_BASE_URL,
    DEFAULT_BASETEN_MODEL,
    DEFAULT_GRADIUM_VOICE,
    DEFAULT_NVIDIA_ASR_MODEL,
    DEFAULT_NVIDIA_ASR_SERVER,
    DEFAULT_NVIDIA_LLM_BASE_URL,
    DEFAULT_NVIDIA_LLM_MODEL,
    DEFAULT_NVIDIA_TTS_MODEL,
    DEFAULT_NVIDIA_TTS_SERVER,
    VOICE_LOOP_SYSTEM_PROMPT,
    Profile,
)


@dataclass(frozen=True)
class VoiceStack:
    """Everything the media pipeline needs, chosen together."""

    stt: Any
    llm: Any
    tts: Any
    vad: Any


def build_voice_stack(profile: Profile) -> VoiceStack:
    if profile == "hosted":
        return _hosted_stack()
    if profile == "local":
        return _local_stack()
    raise ValueError(f"Unsupported VOICE_PROFILE: {profile!r}")


def _hosted_stack() -> VoiceStack:
    """Deepgram STT, Nemotron on Baseten, Gradium TTS."""
    from pipecat.services.baseten.llm import BasetenLLMService
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.services.gradium.tts import GradiumTTSService

    return VoiceStack(
        stt=DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"]),
        llm=BasetenLLMService(
            api_key=os.environ["BASETEN_API_KEY"],
            # A dedicated Baseten deployment has its own /sync/v1 URL and its
            # own served model name; both move together.
            base_url=os.getenv("BASETEN_BASE_URL", DEFAULT_BASETEN_BASE_URL),
            settings=BasetenLLMService.Settings(
                model=os.getenv("BASETEN_MODEL", DEFAULT_BASETEN_MODEL),
                system_instruction=VOICE_LOOP_SYSTEM_PROMPT,
            ),
        ),
        tts=GradiumTTSService(
            api_key=os.environ["GRADIUM_API_KEY"],
            settings=GradiumTTSService.Settings(
                voice=os.getenv("GRADIUM_VOICE_ID", DEFAULT_GRADIUM_VOICE)
            ),
        ),
        vad=SileroVADAnalyzer(),
    )


def _local_stack() -> VoiceStack:
    """Parakeet ASR, Nemotron, and Magpie TTS, all on NIMs you host.

    Pipecat's NVIDIA services default to NVIDIA Cloud Functions, so most of the
    arguments here exist to point them at a local deployment instead: plain-text
    gRPC, no credentials, and no NVCF function routing.
    """
    # Imported lazily so the hosted profile keeps working without the optional
    # `nvidia` extra and its riva-client dependency.
    from pipecat.services.nvidia.llm import NvidiaLLMService
    from pipecat.services.nvidia.stt import NvidiaSTTService
    from pipecat.services.nvidia.tts import NvidiaTTSService

    api_key = os.getenv("NVIDIA_API_KEY", "")
    voice = os.getenv("NVIDIA_TTS_VOICE")
    return VoiceStack(
        stt=NvidiaSTTService(
            server=os.getenv("NVIDIA_ASR_SERVER", DEFAULT_NVIDIA_ASR_SERVER),
            api_key=api_key or None,
            use_ssl=_env_flag("NVIDIA_ASR_USE_SSL"),
            model_function_map={
                "model_name": os.getenv("NVIDIA_ASR_MODEL", DEFAULT_NVIDIA_ASR_MODEL)
            },
            settings=NvidiaSTTService.Settings(interim_results=True),
        ),
        llm=NvidiaLLMService(
            # A local NIM authenticates nothing, but the OpenAI client this
            # wraps refuses to construct without some credential, so send a
            # placeholder the NIM will ignore.
            api_key=api_key or "local-nim",
            base_url=os.getenv("NVIDIA_LLM_BASE_URL", DEFAULT_NVIDIA_LLM_BASE_URL),
            settings=NvidiaLLMService.Settings(
                model=os.getenv("NVIDIA_LLM_MODEL", DEFAULT_NVIDIA_LLM_MODEL),
                system_instruction=VOICE_LOOP_SYSTEM_PROMPT,
            ),
        ),
        tts=NvidiaTTSService(
            server=os.getenv("NVIDIA_TTS_SERVER", DEFAULT_NVIDIA_TTS_SERVER),
            # NvidiaTTSService sends both gRPC metadata headers unconditionally,
            # unlike NvidiaSTTService, which omits them when unset. Passing None
            # here would send a literal "Bearer None" to the local NIM, so send
            # empty strings the server can ignore instead.
            api_key=api_key,
            use_ssl=_env_flag("NVIDIA_TTS_USE_SSL"),
            model_function_map={
                "function_id": os.getenv("NVIDIA_TTS_FUNCTION_ID", ""),
                "model_name": os.getenv("NVIDIA_TTS_MODEL", DEFAULT_NVIDIA_TTS_MODEL),
            },
            # Left unset, Pipecat's own Magpie default voice applies, which
            # suits the default model. A FastPitch NIM serves different voices
            # and needs an explicit name.
            settings=NvidiaTTSService.Settings(voice=voice) if voice else None,
        ),
        vad=SileroVADAnalyzer(),
    )


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
