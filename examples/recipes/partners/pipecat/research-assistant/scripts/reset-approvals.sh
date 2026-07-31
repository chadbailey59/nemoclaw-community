#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Revoke every source approved at runtime, returning the sandbox to its
# baseline policy.
#
#   ./scripts/reset-approvals.sh [sandbox-name]
#   ./scripts/reset-approvals.sh nc --dry-run
#
# Runtime approvals persist for the life of the sandbox, so a second demo on
# the same sandbox is quieter than the first: the agent no longer has to ask
# about anything it was already granted. Run this between demos.
#
# This only removes rules this recipe created, which are the ones OpenShell
# names `allow_<host>_<port>`. Preset-provided access (the baseline research
# sources, npm, pypi, and so on) is left alone; use teardown.sh for those.

set -euo pipefail

SANDBOX="${1:-${NEMOCLAW_SANDBOX:-nc}}"
DRY_RUN=""
[ "${2:-}" = "--dry-run" ] && DRY_RUN="yes"

info() { printf '  %s\n' "$*"; }
fail() { printf '  ERROR: %s\n' "$*" >&2; exit 1; }

command -v openshell >/dev/null 2>&1 || fail "openshell is not on PATH."

# Rule names encode the endpoint: allow_www_w3_org_443 -> www.w3.org:443.
# Read the endpoint back out of the policy rather than reconstructing it from
# the rule name, because a host with a hyphen is not recoverable from the
# underscored form.
mapfile -t RULES < <(
  openshell policy get "${SANDBOX}" --full 2>/dev/null \
    | awk '
        /^  allow_[a-z0-9_]+:/ { rule = $1; sub(":$", "", rule); next }
        rule && /^    - host:/ { host = $3; next }
        rule && host && /^      port:/ { print host ":" $2; rule = ""; host = "" }
      '
)

if [ "${#RULES[@]}" -eq 0 ]; then
  info "No runtime approvals on '${SANDBOX}'. Already at baseline."
  exit 0
fi

info "Runtime approvals on '${SANDBOX}':"
printf '    %s\n' "${RULES[@]}"

if [ -n "${DRY_RUN}" ]; then
  info "--dry-run: nothing removed."
  exit 0
fi

for endpoint in "${RULES[@]}"; do
  info "Revoking ${endpoint}..."
  openshell policy update "${SANDBOX}" --remove-endpoint "${endpoint}" --wait >/dev/null \
    || info "  (could not revoke ${endpoint}; continuing)"
done

REMAINING="$(openshell policy get "${SANDBOX}" --full 2>/dev/null \
  | grep -cE '^  allow_[a-z0-9_]+:' || true)"

if [ "${REMAINING}" -eq 0 ]; then
  info "Done. Back to baseline: the agent must ask again for every source."
else
  info "Warning: ${REMAINING} runtime approval(s) still present."
  info "Inspect with: openshell policy get ${SANDBOX} --full"
fi
