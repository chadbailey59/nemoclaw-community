# SPDX-FileCopyrightText: Copyright (c) 2026 Daily and the Pipecat authors. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Vendored from https://github.com/chadbailey59/agent-voice-bot at commit
# de9d6de. Pinned on purpose: this recipe owns its copy so its behavior
# does not change underneath it when upstream moves.

"""OpenClaw Gateway runtime, reached through a NemoClaw sandbox.

The bot talks to one backend: an OpenClaw agent whose Gateway websocket is
published by a NemoClaw sandbox. The Gateway is the only surface used here —
`chat.send` to start a run, the `chat` event stream to follow it, `sessions.steer`
to inject a follow-up mid-run, and `chat.abort` to preempt it.

This module owns the vocabulary the agent worker speaks — runs, events, results
— alongside the wire client that produces them. It must not import Pipecat: the
worker adapts these types to bus messages, and keeping that direction one-way is
what lets the Gateway client be tested without media timing.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from voice.config import AGENT_LOOP_INSTRUCTION, OpenClawConfig

EventKind = Literal["text_delta", "completed", "cancelled", "failed"]


@dataclass
class RunHandle:
    """The session's current run, and the connection streaming it.

    Mutable on purpose. Steering does not merge a follow-up into the running
    turn — it interrupts that run and starts a new one — so `run_id` moves to
    the replacement and the stream follows it. See `send_followup`.
    """

    run_id: str
    _connection: _GatewayConnection | None = None


@dataclass(frozen=True)
class AgentEvent:
    kind: EventKind
    text: str = ""


@dataclass(frozen=True)
class AgentResult:
    summary: str
    status: Literal["completed", "cancelled", "error"] = "completed"


async def collect_result(events: AsyncIterator[AgentEvent]) -> AgentResult:
    """Fold a run's event stream into the single answer the user hears."""
    parts: list[str] = []
    async for event in events:
        if event.kind == "text_delta" and event.text:
            parts.append(event.text)
        elif event.kind == "completed":
            return AgentResult(event.text or "".join(parts).strip())
        elif event.kind == "cancelled":
            return AgentResult(event.text or "The agent run was cancelled.", "cancelled")
        elif event.kind == "failed":
            return AgentResult(event.text or "The agent run failed.", "error")
    return AgentResult("The agent run ended without a final response.")


class OpenClawRuntime:
    """Runs agent work on an OpenClaw agent behind a NemoClaw sandbox.

    OpenClaw supports the full lifecycle: it streams text, accepts a live steer
    while a run is in flight, and can abort a run. Nothing here fakes a
    capability the Gateway does not actually confirm.
    """

    def __init__(self, config: OpenClawConfig):
        self._config = config

    async def start(self, user_input: str) -> RunHandle:
        conn = _GatewayConnection(self._config)
        try:
            await conn.connect()
            run_id = uuid.uuid4().hex
            payload = await conn.request(
                "chat.send",
                {
                    "sessionKey": self._config.session_key,
                    "message": f"{user_input.rstrip()}\n\n{AGENT_LOOP_INSTRUCTION}",
                    "timeoutMs": int(self._config.timeout_secs * 1000),
                    "idempotencyKey": run_id,
                },
            )
            return RunHandle(
                run_id=str((payload or {}).get("runId") or run_id),
                _connection=conn,
            )
        except BaseException:
            await conn.close()
            raise

    async def events(self, handle: RunHandle) -> AsyncIterator[AgentEvent]:
        conn = _connection_from_handle(handle)
        try:
            while True:
                frame = await conn.next_event()
                if frame is None:
                    # The socket dropped before a terminal state arrived. Fail
                    # the run rather than waiting on a queue nothing will fill.
                    yield AgentEvent(
                        "failed",
                        text="The connection to the OpenClaw Gateway closed before the run finished.",
                    )
                    return
                if frame.get("event") != "chat":
                    continue
                payload = frame.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("runId") != handle.run_id:
                    continue
                state = payload.get("state")
                text = _extract_text(payload.get("message"))
                logger.debug("OpenClaw chat frame: {}", payload)
                if state == "delta":
                    yield AgentEvent("text_delta", text=text)
                elif state == "final":
                    yield AgentEvent("completed", text=text)
                    return
                elif state == "aborted":
                    yield AgentEvent("cancelled", text=text)
                    return
                elif state == "error":
                    yield AgentEvent(
                        "failed",
                        text=str(payload.get("errorMessage") or text),
                    )
                    return
        finally:
            await conn.close()

    async def send_followup(self, handle: RunHandle, user_input: str) -> None:
        """Redirect the session onto a follow-up, and follow it.

        `sessions.steer` does not inject into the running turn. Verified against
        v2026.5.22: it answers `{"status": "started", "interruptedActiveRun":
        true}` — the active run is aborted and a *new* run carries the follow-up.
        Its frames arrive on the same connection, so moving `handle.run_id` onto
        the replacement is enough for `events()` to keep streaming.

        The id is set before the request is sent, not after. The old run's
        `aborted` frame can arrive first, and if the handle still pointed at it
        the stream would end there — the user would be told their task was
        cancelled while the steered run continued unwatched.

        On its own connection for the same reason `stop()` uses one: the stream
        connection may be closing (the run just ended) and a request on a socket
        whose reader has stopped never gets a reply.
        """
        new_run_id = uuid.uuid4().hex
        previous = handle.run_id
        handle.run_id = new_run_id
        conn = _GatewayConnection(self._config)
        try:
            await conn.connect()
            payload = await conn.request(
                "sessions.steer",
                {
                    "key": self._config.session_key,
                    "message": user_input,
                    "idempotencyKey": new_run_id,
                },
            )
        finally:
            await conn.close()
        if isinstance(payload, dict) and payload.get("runId"):
            handle.run_id = str(payload["runId"])
        logger.info(
            "Steered session onto run {} (was {}, interrupted={})",
            handle.run_id,
            previous,
            (payload or {}).get("interruptedActiveRun") if isinstance(payload, dict) else None,
        )

    async def stop(self, handle: RunHandle, reason: str | None = None) -> bool:
        """Abort the run, on a connection of its own. True if one was running.

        Deliberately not the handle's connection. Cancellation reaches this
        method by way of `events()`, whose `finally` has already closed that
        socket while unwinding — reusing it aborts nothing and raises "not
        connected". `chat.abort` is addressed by session key and run id rather
        than by connection identity, so a fresh connection carries it fine.

        Verified against OpenClaw v2026.5.22, which answers a live run with
        `{"ok": true, "aborted": true, "runIds": [id]}` and both a finished run
        and an unknown one with `{"ok": true, "aborted": false, "runIds": []}`.
        So `aborted: false` means there was nothing to stop — the routine race
        when the user says "stop" a moment after the agent finished — not a
        failure. A genuine Gateway error arrives as `ok: false` and is already
        raised by `request()`.
        """
        logger.info("Aborting OpenClaw run {}: {}", handle.run_id, reason)
        conn = _GatewayConnection(self._config)
        try:
            await conn.connect()
            payload = await conn.request(
                "chat.abort",
                {"sessionKey": self._config.session_key, "runId": handle.run_id},
            )
            if not isinstance(payload, dict):
                raise RuntimeError(f"Unexpected chat.abort response: {payload!r}")
            return payload.get("aborted") is True
        finally:
            await conn.close()


class _GatewayConnection:
    """Minimal OpenClaw Gateway websocket client for chat runs."""

    def __init__(self, config: OpenClawConfig):
        self._config = config
        self._ws: Any | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        # A None on this queue means the reader stopped: the socket closed or
        # errored. Consumers must treat it as the end of the stream.
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._hello: asyncio.Future = asyncio.get_running_loop().create_future()

    async def connect(self) -> None:
        import websockets

        # Log the resolved gateway/session so it is obvious which sandbox the
        # bot is actually talking to (profiles and .env can disagree).
        logger.info(
            "OpenClaw connecting to gateway {url} (session {session}, token {token})",
            url=self._config.gateway_url,
            session=self._config.session_key,
            token="set" if self._config.token else "unset",
        )
        self._ws = await websockets.connect(
            self._config.gateway_url,
            max_size=25 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._reader())
        await asyncio.wait_for(self._hello, timeout=self._config.timeout_secs)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._ws is None:
            raise RuntimeError("OpenClaw Gateway is not connected")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._ws.send(
            json.dumps(
                {"type": "req", "id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            )
        )
        return await asyncio.wait_for(future, timeout=self._config.timeout_secs)

    async def next_event(self) -> dict[str, Any] | None:
        """The next Gateway event, or None once the reader has stopped."""
        return await self._events.get()

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                frame = json.loads(raw)
                frame_type = frame.get("type")
                if frame_type == "event":
                    if frame.get("event") == "connect.challenge":
                        await self._send_connect()
                    else:
                        await self._events.put(frame)
                    continue
                if frame_type == "res":
                    request_id = frame.get("id")
                    future = self._pending.pop(str(request_id), None)
                    if future is None or future.done():
                        continue
                    if frame.get("ok"):
                        if not self._hello.done() and _is_hello_ok(frame.get("payload")):
                            self._hello.set_result(frame.get("payload"))
                        future.set_result(frame.get("payload"))
                    else:
                        error = frame.get("error") or {}
                        future.set_exception(RuntimeError(error.get("message") or str(error)))
        except Exception as exc:
            if not self._hello.done():
                self._hello.set_exception(exc)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)
            self._pending.clear()
        finally:
            # Whether the socket closed cleanly or blew up, nothing more will
            # arrive. Wake any consumer parked on next_event().
            await self._events.put(None)

    async def _send_connect(self) -> None:
        auth: dict[str, str] = {}
        if self._config.token:
            auth["token"] = self._config.token
        if self._config.password:
            auth["password"] = self._config.password

        params: dict[str, Any] = {
            "minProtocol": 4,
            "maxProtocol": 4,
            "client": {
                "id": "gateway-client",
                "displayName": "agent-voice-bot",
                "version": "0.1.0",
                "platform": sys.platform,
                "mode": "backend",
            },
            "caps": [],
            "role": "operator",
            "scopes": ["operator.admin"],
        }
        if auth:
            params["auth"] = auth
        task = asyncio.create_task(self.request("connect", params))
        task.add_done_callback(self._finish_connect)

    def _finish_connect(self, task: asyncio.Task) -> None:
        if task.cancelled():
            if not self._hello.done():
                self._hello.cancel()
            return
        if self._hello.done():
            with suppress(Exception):
                task.result()
            return
        try:
            self._hello.set_result(task.result())
        except Exception as exc:
            self._hello.set_exception(exc)


def _connection_from_handle(handle: RunHandle) -> _GatewayConnection:
    if handle._connection is None:
        raise RuntimeError("OpenClaw run handle is missing its Gateway connection")
    return handle._connection


def _is_hello_ok(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("type") == "hello-ok"


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output", "summary"):
            text = value.get(key)
            if isinstance(text, str):
                return text
        content = value.get("content")
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            return "".join(parts)
    return json.dumps(value, ensure_ascii=False)
