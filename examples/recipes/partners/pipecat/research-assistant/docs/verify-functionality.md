# Verify functionality

Three levels, cheapest first. The unit tests need no sandbox; the scripted
check needs a sandbox but no audio; the voice check needs both.

## 1. Unit tests (no sandbox, no credentials)

```console
$ cd examples/recipes/partners/pipecat/research-assistant
$ python -m pytest tests -q
```

Expect all tests to pass. These cover the OCSF parser against a denial line
captured verbatim from a live gateway, the never-allow guard, the spoken-answer
reader, and the ask/answer loop against a fake sandbox.

No `pytest-asyncio` is required; coroutine tests run on plain `pytest`.

## 2. The boundary, without audio

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

## 3. Approving interactively

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

## 4. By voice

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
