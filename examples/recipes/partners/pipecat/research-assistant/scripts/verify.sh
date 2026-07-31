#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Prove the approval boundary end to end, without audio.
#
#   ./scripts/verify.sh [sandbox-name] [host-to-test]
#
# Confirms, against a live sandbox, that:
#   1. an un-allowlisted source is blocked,
#   2. the block surfaces as an approval request,
#   3. approving it actually changes policy, and
#   4. the sandbox can then reach the source.
#
# Step 4 is the one that matters. An approval that reports success but leaves
# the next fetch failing is the failure mode this script exists to catch.

set -euo pipefail

SANDBOX="${1:-${NEMOCLAW_SANDBOX:-nc}}"
HOST="${2:-www.gutenberg.org}"
RECIPE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
LOG="$(mktemp)"

info() { printf '  %s\n' "$*"; }
fail() { printf '  FAIL: %s\n' "$*" >&2; exit 1; }
trap 'rm -f "${LOG}"' EXIT

probe() {
  nemoclaw "${SANDBOX}" exec -- bash -lc \
    "curl -s -m 15 -o /dev/null -w '%{http_code}' https://${HOST}/" 2>/dev/null | tail -1
}

info "Sandbox: ${SANDBOX}   Source under test: ${HOST}"

info "1/4  Confirming ${HOST} is blocked to begin with..."
BEFORE="$(probe || true)"
[ "${BEFORE}" = "000" ] || fail \
  "expected ${HOST} to be blocked, got HTTP ${BEFORE}. Already approved? Try another host."
info "     blocked, as expected"

info "2/4  Starting the gatekeeper (auto-approving this run)..."
cd "${RECIPE_DIR}"
"${PYTHON}" -m gatekeeper.console --sandbox "${SANDBOX}" \
  --auto-allow "${HOST}" --timeout 60 >"${LOG}" 2>&1 &
KEEPER=$!
sleep 6

info "3/4  Triggering the blocked fetch so the gatekeeper sees it..."
probe >/dev/null 2>&1 || true
sleep 20
kill "${KEEPER}" 2>/dev/null || true
wait "${KEEPER}" 2>/dev/null || true

grep -q "${HOST}" "${LOG}" || {
  cat "${LOG}" >&2
  fail "the gatekeeper never saw a denial for ${HOST}"
}
info "     the block surfaced for approval"

info "4/4  Re-fetching now that policy has been widened..."
AFTER="$(probe || true)"
[ "${AFTER}" = "200" ] || {
  cat "${LOG}" >&2
  fail "expected HTTP 200 after approval, got ${AFTER}"
}

cat <<DONE

  PASS  ${HOST}: blocked (000) -> approved -> reachable (200)

  The approval was scoped to that host and the binary that asked for it.
  Inspect it with:
      openshell policy get ${SANDBOX} --full
DONE
