# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gatekeeper.audit import (  # noqa: E402
    SourceLedger,
    audit_answer,
    cited_hosts,
)
from gatekeeper.denials import parse_network_event  # noqa: E402

# Both shapes the gateway actually emits, captured verbatim from a live run.
ALLOWED_FETCH = (
    "[1785506371.815] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] ALLOWED "
    "/usr/local/bin/node(364) -> arxiv.org:443 [policy:allow_arxiv_org_443 engine:opa]"
)
ALLOWED_BARE = (
    "[1785509273.361] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] ALLOWED inference.local:443"
)
DENIED_FETCH = (
    "[1785462960.759] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED "
    "/usr/bin/curl(74441) -> arxiv.org:443 [policy:- engine:opa] [reason:not in policy]"
)

# Verbatim from the run that answered from session memory without fetching.
RECALLED_ANSWER = (
    "I checked arXiv and used it as the primary source base. The short answer "
    "is that NVFP4 is meant for cases where you want the biggest inference "
    "throughput and memory reduction from hardware supported floating point."
)
# Verbatim from the run that was blocked and said so.
HONEST_ANSWER = (
    "I could not verify this research from external sources in this sandbox. "
    "Sources I attempted to use but could not access were NVIDIA Blackwell "
    "materials on developer.nvidia.com and nvidia.com."
)


# --- parsing both dispositions ---------------------------------------------

def test_parses_an_allowed_fetch():
    event = parse_network_event(ALLOWED_FETCH)
    assert event is not None
    assert event.allowed
    assert event.host == "arxiv.org"
    assert event.binary == "/usr/local/bin/node"
    assert event.as_denial() is None


def test_parses_the_bare_inference_form():
    event = parse_network_event(ALLOWED_BARE)
    assert event is not None
    assert event.allowed and event.host == "inference.local"
    assert event.binary == "" and event.pid == 0


def test_denied_events_still_convert_to_denials():
    event = parse_network_event(DENIED_FETCH)
    assert event is not None and not event.allowed
    denial = event.as_denial()
    assert denial is not None and denial.host == "arxiv.org"


# --- reading citations out of prose ----------------------------------------

def test_finds_hostnames_and_common_names():
    assert "developer.nvidia.com" in cited_hosts(HONEST_ANSWER)
    # Spoken answers name arXiv without a hostname; the alias catches it.
    assert "arxiv.org" in cited_hosts(RECALLED_ANSWER)


def test_does_not_mistake_technical_terms_for_hosts():
    text = "NVFP4 beats FP8 on GB200 and GB300, roughly 2x to 3x."
    assert cited_hosts(text) == ()


def test_infrastructure_is_never_treated_as_a_source():
    ledger = SourceLedger()
    ledger.record("inference.local", allowed=True)
    ledger.record("registry.npmjs.org", allowed=True)
    assert ledger.consulted == ()


# --- the audit -------------------------------------------------------------

def test_flags_an_answer_that_credits_a_source_it_never_fetched():
    """The observed case: cites arXiv, made no network request."""
    ledger = SourceLedger()  # nothing fetched
    result = audit_answer(RECALLED_ANSWER, ledger)

    assert not result.clean
    assert "arxiv.org" in result.unverified
    assert "arxiv.org" in result.never_attempted
    assert "arxiv.org" in result.spoken_warning()
    assert "never actually tried to reach" in result.spoken_warning()


def test_an_answer_backed_by_real_fetches_is_clean():
    ledger = SourceLedger()
    ledger.record("arxiv.org", allowed=True)
    result = audit_answer(RECALLED_ANSWER, ledger)

    assert result.clean
    assert result.spoken_warning() == ""
    assert result.consulted == ("arxiv.org",)


def test_naming_a_source_you_genuinely_tried_and_were_denied_is_not_a_warning():
    """Saying "I could not reach X", having really tried X, is the goal.

    It appears in `unverified` because nothing was read from it, but it must
    not interrupt the listener: this is exactly the honest reporting the
    recipe asks for.
    """
    ledger = SourceLedger()
    # The answer says "nvidia.com"; the gateway recorded "www.nvidia.com".
    ledger.record("developer.nvidia.com", allowed=False)
    ledger.record("www.nvidia.com", allowed=False)
    result = audit_answer(HONEST_ANSWER, ledger)

    assert "developer.nvidia.com" in result.unverified
    assert "developer.nvidia.com" in result.blocked_but_cited
    assert result.never_attempted == ()
    assert result.clean and result.spoken_warning() == ""


def test_a_bare_domain_matches_the_subdomain_actually_fetched():
    """Prose says "nvidia.com"; the log says "www.nvidia.com"."""
    ledger = SourceLedger()
    ledger.record("www.nvidia.com", allowed=True)
    result = audit_answer("According to nvidia.com, FP8 is well supported.", ledger)
    assert result.clean and result.unverified == ()


def test_claiming_to_have_tried_a_source_never_contacted_is_flagged():
    """Observed live: an answer said it attempted arXiv; arXiv was never hit.

    The gateway saw connections to a search host and three NVIDIA hosts, and
    none to arxiv.org, while the answer listed "arXiv entries" among the
    sources it had attempted.
    """
    ledger = SourceLedger()
    for host in ("html.duckduckgo.com", "developer.nvidia.com", "www.nvidia.com"):
        ledger.record(host, allowed=False)
    answer = (
        "I attempted to fetch the relevant primary sources, including NVIDIA "
        "Blackwell materials and arXiv papers, and every fetch was blocked."
    )
    result = audit_answer(answer, ledger)

    assert "arxiv.org" in result.never_attempted
    assert not result.clean
    assert "never actually tried to reach" in result.spoken_warning()


def test_reports_a_source_used_but_not_credited():
    """The mirror case: content came from a host the answer never names."""
    ledger = SourceLedger()
    ledger.record("huggingface.co", allowed=True)
    result = audit_answer("The arXiv paper says NVFP4 is faster.", ledger)

    assert "huggingface.co" in result.uncited
    assert "arxiv.org" in result.unverified


def test_ledger_separates_reached_from_blocked():
    ledger = SourceLedger()
    ledger.record("arxiv.org", allowed=True)
    ledger.record("developer.nvidia.com", allowed=False)
    assert ledger.fetched == {"arxiv.org"}
    assert ledger.blocked == {"developer.nvidia.com"}
