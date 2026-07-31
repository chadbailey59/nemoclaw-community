# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check an answer's cited sources against what the agent actually fetched.

An agent's answer is not evidence of what it read. Two observed cases from
this recipe:

- Asked the same question twice in one session, the second run made no network
  request at all and still opened with "I checked arXiv and used it as the
  primary source base". True of an earlier turn, presented as fresh.
- Denied arxiv.org, an agent fetched the same papers through an allowlisted
  mirror and cited the papers, not the route it actually took.

Neither is visible in the transcript. Both are plainly visible at the gateway,
which the gatekeeper is already watching for denials. Recording the allowed
fetches too costs nothing and turns "trust the answer" into "check it".

This is a reporting aid, not a security control. It cannot see *what* was in a
response, only which hosts were contacted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Hosts that are the agent's own plumbing rather than research sources. Fetches
# here say nothing about whether a question was researched.
INFRASTRUCTURE = frozenset({
    "inference.local",
    "host.openshell.internal",
    "clawhub.ai",
    "openclaw.ai",
    "docs.openclaw.ai",
    "openrouter.ai",
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "pypi.org",
    "files.pythonhosted.org",
    "formulae.brew.sh",
})

# A hostname as it appears in prose. Requires a dot and a plausible TLD so that
# "FP8" and "NVFP4" are not mistaken for hosts.
HOSTNAME_IN_TEXT = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|org|net|edu|gov|io|ai|co|dev|me|info|uk|de|fr|jp|cn)\b",
    re.IGNORECASE,
)

# Sources a spoken answer names without a hostname. Voice answers are told not
# to read URLs aloud, so "the arXiv paper" is the normal way to cite one.
SOURCE_ALIASES: dict[str, str] = {
    "arxiv": "arxiv.org",
    "wikipedia": "en.wikipedia.org",
    "hugging face": "huggingface.co",
    "huggingface": "huggingface.co",
    "github": "github.com",
    "rfc editor": "www.rfc-editor.org",
}


@dataclass
class SourceLedger:
    """What the agent actually reached, as seen at the gateway."""

    fetched: set[str] = field(default_factory=set)
    blocked: set[str] = field(default_factory=set)

    def record(self, host: str, allowed: bool) -> None:
        if host in INFRASTRUCTURE:
            return
        (self.fetched if allowed else self.blocked).add(host)

    @property
    def consulted(self) -> tuple[str, ...]:
        return tuple(sorted(self.fetched))


@dataclass(frozen=True)
class Audit:
    cited: tuple[str, ...]
    consulted: tuple[str, ...]
    unverified: tuple[str, ...]
    """Cited in the answer, never successfully fetched."""
    blocked_but_cited: tuple[str, ...]
    """Cited and genuinely attempted, but denied. Naming these is honest."""
    never_attempted: tuple[str, ...]
    """Cited, and never contacted at all. The sharp signal."""
    uncited: tuple[str, ...]
    """Contacted but not named. Usually fine; occasionally the mirror it used."""

    @property
    def clean(self) -> bool:
        # Only an unattempted claim is worth interrupting a listener over. An
        # answer that says "I could not reach X" and really did try X is doing
        # exactly what it was asked to do.
        return not self.never_attempted

    def spoken_warning(self) -> str:
        """One sentence for the voice loop, or empty when nothing is wrong."""
        if self.clean:
            return ""
        names = ", ".join(self.never_attempted)
        it = "it" if len(self.never_attempted) == 1 else "them"
        return (
            f"Heads up: the answer refers to {names}, but the agent never "
            f"actually tried to reach {it} during this run."
        )


def cited_hosts(answer: str) -> tuple[str, ...]:
    """Hosts an answer claims to rest on, by hostname or by common name."""
    found = {match.group(0).lower().rstrip(".") for match in HOSTNAME_IN_TEXT.finditer(answer)}
    lowered = answer.lower()
    for alias, host in SOURCE_ALIASES.items():
        if alias in lowered:
            found.add(host)
    return tuple(sorted(found - INFRASTRUCTURE))


def _covered_by(host: str, known: set[str]) -> bool:
    """Whether `host` names something in `known`, allowing for subdomains.

    Prose and the gateway disagree about precision. An answer says "nvidia.com"
    where the log records `www.nvidia.com`, and treating those as different
    hosts would report an honest answer as a fabricated one. Matching in either
    direction keeps the warning for cases that are actually wrong.
    """
    return any(
        candidate == host
        or candidate.endswith(f".{host}")
        or host.endswith(f".{candidate}")
        for candidate in known
    )


def audit_answer(answer: str, ledger: SourceLedger) -> Audit:
    """Compare what an answer credits against what was actually fetched."""
    cited = cited_hosts(answer)
    consulted = ledger.consulted
    unverified = tuple(h for h in cited if not _covered_by(h, ledger.fetched))
    # Naming a source you tried and were denied is the honest behaviour this
    # recipe asks for. Naming one you never touched is the case worth raising:
    # observed live, an answer said it had attempted arXiv when the gateway saw
    # no connection to arxiv.org at all.
    blocked_but_cited = tuple(h for h in unverified if _covered_by(h, ledger.blocked))
    never_attempted = tuple(h for h in unverified if not _covered_by(h, ledger.blocked))
    uncited = tuple(h for h in consulted if not _covered_by(h, set(cited)))
    return Audit(
        cited=cited,
        consulted=consulted,
        unverified=unverified,
        blocked_but_cited=blocked_but_cited,
        never_attempted=never_attempted,
        uncited=uncited,
    )
