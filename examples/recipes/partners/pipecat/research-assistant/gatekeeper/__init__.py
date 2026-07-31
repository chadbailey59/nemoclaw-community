# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Host-side approval boundary for the research assistant.

Nothing in this package runs inside the sandbox. That is the point.
"""

from .approver import ApprovalOutcome, PolicyApprover, UnsafeEndpoint, validate_host
from .denials import Deduplicator, Denial, DenialWatcher, ResearchScope, parse_denial
from .service import Gatekeeper, GatekeeperEvent, Question, interpret

__all__ = [
    "ApprovalOutcome",
    "Deduplicator",
    "Denial",
    "DenialWatcher",
    "Gatekeeper",
    "GatekeeperEvent",
    "PolicyApprover",
    "Question",
    "ResearchScope",
    "UnsafeEndpoint",
    "interpret",
    "parse_denial",
    "validate_host",
]
