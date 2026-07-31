# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Approve blocked research sources from a terminal, with no audio in the way.

This is the same boundary the voice bot drives, minus the speech. Use it to
verify the recipe end to end, or when you are already at a keyboard.

    python -m gatekeeper.console --sandbox nc
    python -m gatekeeper.console --sandbox nc --auto yes,no
    python -m gatekeeper.console --sandbox nc --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from .approver import PolicyApprover
from .denials import DenialWatcher
from .service import Gatekeeper

DIM, BOLD, YELLOW, GREEN, RED, RESET = (
    "\033[2m", "\033[1m", "\033[33m", "\033[32m", "\033[31m", "\033[0m",
)


async def _report(keeper: Gatekeeper) -> None:
    """Print gatekeeper events as they happen."""
    colors = {"asked": YELLOW, "resolved": GREEN, "auto": GREEN, "error": RED}
    while True:
        event = await keeper.events.get()
        color = colors.get(event.kind, "")
        if event.kind == "asked":
            print(f"\n{color}{BOLD}ASK{RESET} {event.text}")
            print(f"{DIM}    {event.data.get('detail', '')}{RESET}")
        else:
            print(f"{color}{event.kind.upper():<4}{RESET} {event.text}")


async def _answer_loop(keeper: Gatekeeper, scripted: list[str] | None) -> None:
    """Feed answers to whatever is pending.

    With no scripted answers and no terminal to read from - a CI run, or a
    verification script - there is nobody to ask. Idle instead of prompting:
    reading stdin would raise EOF immediately and take the watcher down with
    it, which looks exactly like "no source was ever blocked".
    """
    interactive = sys.stdin is not None and sys.stdin.isatty()
    if scripted is None and not interactive:
        print(f"{DIM}Not a terminal: questions will be reported, not prompted.{RESET}")
        while True:
            await asyncio.sleep(3600)

    answered = 0
    while True:
        await asyncio.sleep(0.4)
        if not keeper.pending:
            continue
        if scripted is not None:
            if answered >= len(scripted):
                return
            answer = scripted[answered]
            print(f"{DIM}    (scripted answer: {answer}){RESET}")
        else:
            try:
                answer = await asyncio.to_thread(input, "    allow? > ")
            except EOFError:
                print(f"{DIM}Input closed; leaving remaining sources blocked.{RESET}")
                return
        answered += 1
        await keeper.answer(answer)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", default="nc")
    parser.add_argument("--openshell", default="openshell")
    parser.add_argument("--dry-run", action="store_true", help="Never modify policy.")
    parser.add_argument("--auto", help="Comma-separated scripted answers (e.g. yes,no).")
    parser.add_argument(
        "--auto-allow",
        default="",
        help="Comma-separated hosts to open without asking.",
    )
    parser.add_argument("--timeout", type=float, help="Exit after this many seconds.")
    args = parser.parse_args()

    # Line-buffer stdout so a redirected run still has a log after the process
    # is killed. Block buffering silently loses the whole session otherwise.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)

    scripted = [a.strip() for a in args.auto.split(",")] if args.auto else None
    auto_allow = frozenset(h.strip() for h in args.auto_allow.split(",") if h.strip())

    watcher = DenialWatcher(args.sandbox, openshell=args.openshell)
    approver = PolicyApprover(args.sandbox, openshell=args.openshell, dry_run=args.dry_run)

    print(f"{BOLD}Watching {args.sandbox} for blocked research sources.{RESET}")
    if args.dry_run:
        print(f"{DIM}Dry run: approvals will not change policy.{RESET}")

    async with watcher:
        keeper = Gatekeeper(watcher, approver, auto_allow=auto_allow)
        tasks = [
            asyncio.create_task(keeper.run()),
            asyncio.create_task(_report(keeper)),
            asyncio.create_task(_answer_loop(keeper, scripted)),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=args.timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
