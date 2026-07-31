# What this recipe needs from your sandbox

`tests/test_sandbox_conformance.py` checks thirteen things about NemoClaw,
OpenShell, and OpenClaw — not about this recipe's own code. If one fails, the
recipe is broken even though every unit test still passes.

```console
$ RESEARCH_SANDBOX=nc python3 -m pytest tests/test_sandbox_conformance.py -v
```

Each section below names the failing test, what it was checking, and what to do
about it. **If you are working with a coding agent, point it at this file and
the failing test name** — everything it needs to diagnose and fix the sandbox
is here.

Two failure modes read very differently:

- **Your sandbox drifted.** A preset was removed, the skill was never
  installed, the token expired. Fix the sandbox; the commands are below.
- **The platform changed.** An upgrade altered event formats, flag semantics,
  or enforcement layering. Then the recipe's assumption is wrong and the *code*
  needs updating, not your sandbox. Each section says which conclusion to draw.

---

## `test_openshell_is_not_reachable_from_inside_the_sandbox`

**Checks:** `openshell` is not on `PATH` inside the sandbox.

**Why:** this is the whole security model. The agent must be able to *ask* for
network access by attempting a fetch, and never to *grant* it. If `openshell`
is reachable from inside, the agent can widen its own policy and the approval
boundary is decoration.

**If it fails:** you are running a custom sandbox image that installs
`openshell`, or the host binary has been mounted in. Neither happens with a
default `nemoclaw onboard`.

```console
$ nemoclaw <sandbox> exec -- bash -lc 'command -v openshell'   # should print nothing
```

Rebuild from the stock image, or remove `openshell` from your Dockerfile and
any bind mount that exposes it. Do not work around this test.

---

## `test_the_sandbox_cannot_reach_the_host_gateway_directly`

**Checks:** common host-gateway addresses (`10.200.0.1`, `172.17.0.1`) do not
answer from inside the sandbox.

**Why:** the same asymmetry by another route. A host-side service the sandbox
can reach is a path to the approval machinery.

**If it fails:** something has opened host networking — a policy preset
allowing an RFC1918 destination, a `--network host` container, or a custom
bridge. Review `openshell policy get <sandbox> --full` for `allowed_ips`
entries covering private ranges, and remove what you did not intend.

---

## `test_an_unlisted_host_is_blocked`

**Checks:** a host not in the policy returns `000` from inside the sandbox.

**Why:** deny-by-default is what produces the approval question. Without it
nothing is ever blocked and the recipe has nothing to ask about.

**If it fails:** the sandbox is running a permissive policy, or the host you
are testing was approved earlier and never revoked.

```console
$ ./scripts/reset-approvals.sh <sandbox>          # drop runtime approvals
$ openshell policy get <sandbox> --full           # look for a broad allow
```

If a permissive baseline is in use (`openclaw-sandbox-permissive.yaml` or
similar), this recipe is not meaningful on that sandbox.

---

## `test_a_block_emits_a_parseable_denial`

**Checks:** a blocked connection produces an OCSF `NET:OPEN … DENIED` line
naming host, port, and calling binary, and that `gatekeeper/denials.py` parses
it.

**Why:** that event *is* the escalation. No event, no question, no recipe. The
approval is scoped from exactly those three fields.

**If it fails:** either the events are not being emitted, or their format
changed.

```console
$ openshell logs <sandbox> -n 200 --level debug --since 5m | grep DENIED
```

If you see denials but the test fails, the format moved and `NET_EVENT` in
`gatekeeper/denials.py` needs updating — that is a code change, not a sandbox
fix. If you see nothing, check the log level and that the sandbox is actually
attempting the fetch.

---

## `test_an_allowed_fetch_emits_a_parseable_event`

**Checks:** a successful fetch produces a parseable `ALLOWED` event.

**Why:** the citation audit is built on these. Without them it cannot tell a
source that was read from one that was only named.

**If it fails:** most likely the baseline policy is not usable (see
`test_the_baseline_policy_is_applied_and_usable`), so nothing succeeds to be
logged. The gateway emits two shapes — one naming the binary and matched
policy, one bare `host:port` for inference traffic — and `NET_EVENT` handles
both. A third shape would need adding.

---

## `test_path_rules_are_enforced_separately_from_the_connection`

**Checks:** `en.wikipedia.org/wiki/Main_Page` is reachable while `/` is not.

**Why:** OpenShell enforces at two layers. `NET:OPEN` decides whether a
connection may be opened; `HTTP:GET` decides whether a method and path may be
requested over it. The gatekeeper reads only the first, so a path-level denial
never becomes a spoken question. This test pins that behaviour so a change is
noticed.

**If it fails because `/` is now permitted:** path rules are no longer being
enforced as the baseline expects. Check that
`agents/openclaw/policy.yaml` still scopes `en.wikipedia.org` to `/wiki/**`
and that your applied preset matches the shipped one.

**If it fails because `/wiki/Main_Page` is blocked:** see the baseline check
below.

---

## `test_an_endpoint_without_binary_scope_grants_nothing`

**Checks:** `openshell policy update --add-endpoint …` *without* `--binary`
merges into the policy and the host is still refused.

**Why:** OpenShell resolves rules per calling binary. This is the trap that
makes an approval look successful while every subsequent fetch fails — the
endpoint appears in `policy get --full`, and nothing works.

**If it fails because access was granted:** the platform now scopes endpoints
without an explicit binary. That is a *code* opportunity, not a sandbox
problem: `PolicyApprover.allow()` could stop passing `--binary`. Confirm
deliberately before changing it — over-scoping an approval is worse than an
unnecessary flag.

---

## `test_an_endpoint_with_binary_scope_grants_access`

**Checks:** the same call *with* `--binary /usr/bin/curl` and `--wait` grants
access, and the output contains "loaded".

**Why:** this is exactly what an approval does. `--wait` makes the gateway
confirm the revision is live, and `PolicyApprover` reports success on that
word — "submitted" can precede "loaded" by seconds.

**If it fails:** run the command by hand and read both streams.

```console
$ openshell policy update <sandbox> \
    --add-endpoint example.com:443:read-only:rest:enforce \
    --binary /usr/bin/curl --wait
```

If it reports loaded but the fetch still fails, the binary path is wrong for
your image — check what actually issues requests (`/usr/bin/curl`,
`/usr/local/bin/node`) and whether `agents/openclaw/policy.yaml` lists it.

---

## `test_openshell_reports_progress_on_stderr`

**Checks:** `openshell policy update` writes "loaded" to stderr, not stdout.

**Why:** `PolicyApprover` merges the two streams because of this. An approver
reading stdout alone sees an empty string and reports every approval as not
applied.

**If it fails:** `openshell` moved its output to stdout. Harmless — the
approver merges both — but the test is informational and should be updated so
it stops asserting something untrue.

---

## `test_an_approval_is_scoped_to_one_binary_not_the_whole_sandbox`

**Checks:** an approval carries a `binaries:` list rather than opening the host
for everything in the sandbox.

**Why:** approving for `curl` should not quietly also open the host for the
agent's own runtime.

**If it fails:** the approval path is over-granting. Check
`PolicyApprover.endpoint_spec` and that the denial event still names a binary
to scope to.

---

## `test_the_baseline_policy_is_applied_and_usable`

**Checks:** the `research-baseline` preset is applied *and* its hosts are
actually reachable.

**Why:** both halves matter, and the second is the one that bites. A preset can
apply cleanly, appear in `policy-list`, show its hosts in `policy get --full`,
and grant nothing — because it has no `binaries:` block.

**If it fails:**

```console
$ ./scripts/setup.sh <sandbox>                    # applies the preset
$ nemoclaw <sandbox> policy-list                  # research-baseline present?
$ nemoclaw <sandbox> exec -- curl -s -o /dev/null -w '%{http_code}' \
    https://en.wikipedia.org/wiki/Main_Page       # expect 200
```

If the preset is applied but the fetch returns `000`, confirm
`agents/openclaw/policy.yaml` still carries its `binaries:` list. That block is
load-bearing and its absence fails silently in both directions.

---

## `test_the_research_skill_is_installed`

**Checks:** `~/.openclaw/skills/research-sweep/SKILL.md` exists in the sandbox
and mentions the policy boundary.

**Why:** without it the agent has no procedure, and in particular no written
rule that a blocked source is a boundary rather than an obstacle.

**If it fails:**

```console
$ nemoclaw <sandbox> skill install agents/openclaw/skills/research-sweep
```

Frontmatter must be the first line of `SKILL.md`; a leading comment makes the
installer reject it.

---

## `test_the_gateway_needs_and_accepts_a_token`

**Checks:** `nemoclaw <sandbox> gateway-token --quiet` returns a token.

**Why:** the Gateway auto-pairs only recognised clients — the control UI and
webchat. This bot is neither, so without a token every run fails with
`device identity required`.

**If it fails:**

```console
$ echo "OPENCLAW_TOKEN=$(nemoclaw <sandbox> gateway-token --quiet)" >> .env
```

Also confirm the Gateway URL. A sandbox publishes its own port — check
`nemoclaw list` — which is not OpenClaw's default `18789`.

---

## When the platform has moved on

Several tests above distinguish "your sandbox is wrong" from "the recipe's
assumption is wrong". If it is the latter, the fix belongs in the code and the
test should be updated in the same change, with the new behaviour recorded in
[security-model.md](security-model.md).

Every one of these assumptions was wrong at least once during development, and
each was found by running against a live sandbox rather than by reading
documentation. A mock would have agreed with every mistake.
