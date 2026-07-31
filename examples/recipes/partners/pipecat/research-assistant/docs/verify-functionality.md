# Verify functionality

Cheapest first, and in three layers that fail for different reasons.

1. **Our code** (§1) — unit tests, no sandbox, no credentials.
2. **The platform** (§2–3) — does the sandbox still behave as assumed, and does
   the agent on top of it. Needs a sandbox; skipped without one.
3. **The whole loop** (§4–6) — a real approval, by script, by terminal, by
   voice.

The distinction in layer 2 is the one worth internalising: this recipe can stop
working entirely without a single unit test failing, because most of what it
depends on belongs to NemoClaw, OpenShell, and OpenClaw rather than to us. Run
§2 first after any upgrade of those.

## 1. Unit tests (no sandbox, no credentials, no virtualenv)

```console
$ cd examples/recipes/partners/pipecat/research-assistant
$ python3 -m pytest tests -q
61 passed
```

`pytest` is the only requirement. Not `pytest-asyncio` — coroutine tests are
driven through a small `run()` helper — and not Pipecat, because nothing under
`tests/` imports the voice loop. If a test ever fails with
`ModuleNotFoundError: No module named 'loguru'`, something has crossed that
boundary and belongs on the `gatekeeper` side of it.

What they cover:

| Area | What is checked |
| --- | --- |
| OCSF parsing | Allowed and denied events, in both shapes the gateway emits, against lines captured verbatim from a live sandbox. |
| Never-allow guard | Loopback, link-local, private, `.internal`, and shell metacharacters are refused however they arrive. |
| Spoken answers | Only an unambiguous affirmative approves; ambiguity opens nothing. |
| Consent integrity | One question outstanding at a time, and an answer for the wrong host opens nothing. |
| Approval mechanics | `--binary` scoping and "loaded, not merely submitted". |
| Citation audit | Sources credited but never contacted are flagged; sources genuinely tried and denied are not. |
| Shipped config | The baseline policy still carries a `binaries` block, without which it grants nothing. |

To run one area:

```console
$ python3 -m pytest tests/test_audit.py -q                       # the citation audit
$ python3 -m pytest tests -q -k "wrong_host or one_question"     # consent integrity
$ python3 -m pytest tests -q -k never_contacted                  # claimed but never fetched
```

The two worth knowing by name, because each was written after the live run
that exposed the bug:

- `test_an_answer_for_the_wrong_host_opens_nothing` — the bot once asked about
  one host and opened another.
- `test_claiming_to_have_tried_a_source_never_contacted_is_flagged` — an answer
  said it had attempted arXiv when the gateway saw no connection to it.

## 2. Is the sandbox still configured the way this recipe assumes?

The unit tests check our code. This checks the ground it stands on, and it is
the first thing to run after a NemoClaw or OpenClaw upgrade — the recipe can
break completely while every unit test still passes.

```console
$ RESEARCH_SANDBOX=nc python3 -m pytest tests/test_sandbox_conformance.py -v
```

Every test here corresponds to an assumption that was wrong at least once
during development:

| Assumption | Why it is checked |
| --- | --- |
| `openshell` is absent from the sandbox and the host is unreachable from it | The entire security story. If this fails, the agent can grant itself access. |
| An unlisted host is refused | Deny-by-default is what produces the question. |
| A block emits a parseable OCSF `NET:OPEN` denial naming host, port, and binary | The approval is scoped from exactly those fields. |
| An allowed fetch emits a parseable event | The citation audit is built on these. |
| Path rules enforce separately from the connection | Two layers; the approval loop reads only one. |
| An endpoint without `--binary` grants nothing | Merges into policy and still 403s. Fails silently if forgotten. |
| An endpoint with `--binary` grants access, and `--wait` says "loaded" | The approver reports success on exactly that word. |
| `openshell` writes progress to stderr | The approver merges the streams because of this. |
| The baseline preset is applied and its hosts are reachable | A preset can apply cleanly and still grant nothing. |
| The research skill is installed | Otherwise the agent has no procedure. |
| A gateway token exists | Without one, every run fails `device identity required`. |

A failure here is a platform change, not a regression in this recipe. Read the
assertion message: several say what to conclude if the platform has moved on.

## 3. Does the agent behave the way the recipe needs?

Slower, model-dependent, and asserted loosely — on whether network traffic
happened at all, not on the wording of an answer.

```console
$ RESEARCH_SANDBOX=nc python3 -m pytest tests/test_agent_behavior.py -v
```

| Behaviour | Why it matters |
| --- | --- |
| A research request produces real fetches | Once failed because the agent-loop instruction asked for "one concise answer", producing a confident reply in two seconds with no fetch. |
| It stops at the boundary rather than routing around | Skipped when a mirror-capable preset is applied, because the answer is already known to be "it routes around". |
| It reports blocked sources instead of guessing | The honest-failure case. |
| The audit agrees with the gateway | Whatever the answer credits, the ledger is the record. |

Each test uses a distinct session key. Reusing one lets the agent answer from
what an earlier run found, which produces no network traffic and reads exactly
like fresh research.

## 4. The boundary, without audio

Confirms against a live sandbox that a blocked source becomes an approval
request, and that approving it actually changes policy.

```console
$ ./scripts/verify.sh nc www.w3.org
```

Expected output:

```text
  1/4  Confirming www.w3.org is blocked to begin with...
       blocked, as expected
  2/4  Starting the gatekeeper (auto-approving this run)...
  3/4  Triggering the blocked fetch so the gatekeeper sees it...
       the block surfaced for approval
  4/4  Re-fetching now that policy has been widened...

  PASS  www.w3.org: blocked (000) -> approved -> reachable (200)
```

Step 4 is the one that matters. An approval that reports success while the next
fetch still fails is the failure this check exists to catch.

Pick a host that has not been approved before. If step 1 reports HTTP 200, that
host is already open; choose another. Inspect what is currently open with:

```console
$ openshell policy get nc --full
```

An approval made by this recipe looks like this. Note that it is scoped to one
binary, not opened for everything in the sandbox:

```yaml
  allow_www_w3_org_443:
    name: allow_www_w3_org_443
    endpoints:
    - host: www.w3.org
      port: 443
      protocol: rest
      enforcement: enforce
      access: read-only
    binaries:
    - path: /usr/bin/curl
```

## 5. Approving interactively

Same boundary, answering by hand:

```console
$ python -m gatekeeper.console --sandbox nc
```

In another terminal, make the sandbox reach for something:

```console
$ nemoclaw nc exec -- curl -s -m 10 -o /dev/null https://arxiv.org/
```

The console prints the question and waits. Answer `yes` or `no`. Answer
something ambiguous and it will tell you nothing was opened, and ask again.

Add `--dry-run` to see exactly which `openshell` command an approval would run,
without changing any policy.

## 6. By voice

```console
$ python -m voice.bot -t webrtc --port 7860
```

Open the printed URL, then start a sweep out loud: *"research how NVFP4
quantization compares to FP8 for inference."* The agent works in the sandbox.
When it reaches a source that is not allowlisted, the bot interrupts and asks.
Say yes and it resumes; say no and it records the source as unavailable and
carries on.

What to listen for:

- The question names a host, not a URL read character by character.
- An ambiguous answer does not open anything.
- "No" is reported as left blocked, not as an error.
- The voice loop stays responsive while the agent is working.

## Teardown

```console
$ ./scripts/teardown.sh nc
```

Removes the baseline preset and lists any endpoints approved at runtime, with
the command to remove them. Recreating the sandbox resets the policy entirely.
