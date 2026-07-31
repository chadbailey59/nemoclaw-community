#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Remove what this recipe added to a sandbox.
#
#   ./scripts/teardown.sh [sandbox-name]
#
# Removes the baseline policy preset. Endpoints approved at runtime are a
# separate matter: they live in the running policy, not in the preset, so this
# script lists them and tells you how to drop them. It does not remove them
# silently, because some of them may be ones you meant to keep.

set -euo pipefail

SANDBOX="${1:-${NEMOCLAW_SANDBOX:-nc}}"

info() { printf '  %s\n' "$*"; }

info "Sandbox: ${SANDBOX}"

info "Removing the baseline research policy preset..."
nemoclaw "${SANDBOX}" policy-remove research-baseline --yes 2>/dev/null \
  || info "(preset was not applied)"

APPROVED="$(openshell policy get "${SANDBOX}" --full 2>/dev/null \
  | grep -oE '^  allow_[a-z0-9_]+:' | tr -d ' :' || true)"

if [ -n "${APPROVED}" ]; then
  cat <<LIST

  Endpoints approved at runtime are still in the running policy:

$(printf '    %s\n' ${APPROVED})

  Remove one:
      openshell policy update ${SANDBOX} --remove-endpoint HOST:443

  Or clear all of them at once by recreating the sandbox, which resets the
  policy to its baseline:
      nemoclaw ${SANDBOX} destroy
LIST
else
  info "No runtime-approved endpoints remain."
fi

info "Teardown complete. The research-sweep skill is left installed; remove it"
info "by recreating the sandbox if you want a clean image."
