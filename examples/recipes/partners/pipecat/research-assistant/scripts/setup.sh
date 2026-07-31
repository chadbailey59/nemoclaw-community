#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Prepare a NemoClaw sandbox to run the research assistant.
#
#   ./scripts/setup.sh [sandbox-name]
#
# Applies the baseline research policy and installs the research-sweep skill.
# Deliberately does not widen egress beyond the baseline: every other source
# is meant to be approved out loud, at the moment the agent asks for it.

set -euo pipefail

SANDBOX="${1:-${NEMOCLAW_SANDBOX:-nc}}"
RECIPE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

info() { printf '  %s\n' "$*"; }
fail() { printf '  ERROR: %s\n' "$*" >&2; exit 1; }

command -v nemoclaw >/dev/null 2>&1 || fail "nemoclaw is not on PATH."
command -v openshell >/dev/null 2>&1 || fail \
  "openshell is not on PATH. It applies approvals and must stay host-side."

info "Sandbox: ${SANDBOX}"

if ! nemoclaw list 2>/dev/null | grep -qE "^\s+${SANDBOX}\b"; then
  fail "Sandbox '${SANDBOX}' not found. Create one with 'nemoclaw onboard'."
fi

info "Applying the baseline research policy..."
nemoclaw "${SANDBOX}" policy-add \
  --from-file "${RECIPE_DIR}/agents/openclaw/policy.yaml" --yes

info "Installing the research-sweep skill..."
nemoclaw "${SANDBOX}" skill install \
  "${RECIPE_DIR}/agents/openclaw/skills/research-sweep"

cat <<'NEXT'

  Setup complete.

  The agent can currently read only the baseline sources. Anything else it
  reaches for will be blocked and raised for approval.

  Next:
    1. Copy .env.example to .env and fill in your speech credentials.
    2. Approve from a terminal:   python -m gatekeeper.console --sandbox SANDBOX
       ...or by voice:            python -m voice.bot -t webrtc --port 7860
    3. Verify the boundary works: ./scripts/verify.sh SANDBOX

  Teardown: ./scripts/teardown.sh SANDBOX
NEXT
