# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Host-side approval boundary for the research assistant.

Nothing in this package runs inside the sandbox. That is the point.
"""

from .approver import ApprovalOutcome, PolicyApprover, UnsafeEndpoint, validate_host
from .audit import Audit, SourceLedger, audit_answer, cited_hosts
from .denials import (
    Deduplicator,
    Denial,
    DenialWatcher,
    NetworkEvent,
    ResearchScope,
    parse_denial,
    parse_network_event,
)
from .service import Gatekeeper, GatekeeperEvent, Question, interpret

__all__ = [
    "ApprovalOutcome",
    "Audit",
    "Deduplicator",
    "Denial",
    "DenialWatcher",
    "Gatekeeper",
    "GatekeeperEvent",
    "NetworkEvent",
    "PolicyApprover",
    "Question",
    "ResearchScope",
    "SourceLedger",
    "UnsafeEndpoint",
    "audit_answer",
    "cited_hosts",
    "interpret",
    "parse_denial",
    "parse_network_event",
    "validate_host",
]
