# Research Assistant

*This example was built by the Pipecat team at Daily and is presented as-is.
See [Credits](#credits) for contact information.*

A research agent that runs for as long as the question deserves, and asks you
out loud before it reads anything it was not already allowed to read.

NemoClaw already has a human release boundary for network egress: the sandbox
denies by default, and an operator approves blocked requests one at a time.
Today that approval happens in a terminal, in front of `openshell term`. This
recipe puts the same boundary on a voice channel, so it still works when you
are not at the keyboard — which is exactly when a twenty-minute research sweep
is running.

```text
  you ──voice──┐                                    ┌── NemoClaw sandbox
               │                                    │
        ┌──────▼──────┐   blocked source     ┌──────▼──────┐
        │  voice loop │◄─────────────────────│  OpenShell  │  research agent
        │  (Pipecat)  │                      │   gateway   │  (OpenClaw)
        └──────┬──────┘   OCSF denial        └──────▲──────┘
               │                                    │
               └──"yes"──► gatekeeper ──openshell───┘
                           (host-side only)   policy update
```

The agent can *ask* for more reach by trying to fetch something. It can never
*grant* it: `openshell` lives on the host, is not installed in the sandbox, and
is not reachable from it. No text the model produces reaches the approval path.

Work through the three sections below in order. The first is the one people
skip and then spend an afternoon debugging.

---

## 1. Check your agent sandbox

This recipe is only as good as the sandbox underneath it. Most of what it
depends on — deny-by-default egress, the shape of OCSF events, per-binary rule
resolution — belongs to NemoClaw, OpenShell, and OpenClaw rather than to this
code. It can stop working completely without a single unit test failing.

**You need:**

- NemoClaw with a sandbox running the OpenClaw agent (`nemoclaw onboard`).
- `openshell` on the host `PATH`. **Do not install it in the sandbox** — that
  asymmetry is the whole security model.
- Python 3.12+. Sections 1 and 2 need nothing beyond `pytest`; only the voice
  loop in section 3 needs Pipecat and speech credentials (see `.env.example`).

**Set it up:**

```console
$ cd examples/recipes/partners/pipecat/research-assistant
$ ./scripts/setup.sh nc
$ cp .env.example .env
$ echo "OPENCLAW_TOKEN=$(nemoclaw nc gateway-token --quiet)" >> .env
```

The Gateway token is required, not optional. OpenClaw auto-pairs only the
control UI and webchat, and this bot is neither, so without a token every run
fails with `device identity required`. Check the Gateway port with
`nemoclaw list` — a sandbox publishes its own (18790 here), not OpenClaw's
default 18789.

`setup.sh` applies a deliberately small baseline policy and installs the skill.
Small on purpose: a generous baseline makes the demo quiet and the boundary
meaningless. It also warns if presets are applied that would let the agent
route around the boundary entirely — see [Known limitations](#known-limitations).

**Then confirm the sandbox actually behaves as assumed:**

```console
$ RESEARCH_SANDBOX=nc python3 -m pytest tests/test_sandbox_conformance.py -v
13 passed
```

Thirteen checks, each corresponding to an assumption that was wrong at least
once while building this:

| Assumption | Why it is checked |
| --- | --- |
| `openshell` absent from the sandbox; host unreachable from it | The whole security story. If this fails, the agent can grant itself access. |
| An unlisted host is refused | Deny-by-default is what produces the question. |
| A block emits a parseable OCSF denial naming host, port, and binary | An approval is scoped from exactly those fields. |
| An allowed fetch emits a parseable event | The citation audit is built on these. |
| Path rules enforce separately from the connection | Two layers; the approval loop reads only one. |
| An endpoint without `--binary` grants nothing | Merges into policy and still fails at connect time. |
| An endpoint with `--binary` grants access; `--wait` says "loaded" | The approver reports success on exactly that word. |
| `openshell` reports progress on stderr | The approver merges the streams because of this. |
| The baseline preset is applied and reachable | A preset can apply cleanly and still grant nothing. |
| The research skill is installed | Otherwise the agent has no procedure. |
| A gateway token exists | Without one, every run fails to connect. |

A failure here is a platform change, not a regression in this recipe. Several
assertions say what to conclude if the platform has moved on. **Run this first
after any NemoClaw or OpenClaw upgrade.**

---

## 2. Run all the tests

```console
$ python3 -m pytest tests -q                          # our code, anywhere
$ RESEARCH_SANDBOX=nc python3 -m pytest tests -q      # everything, live
$ ./scripts/verify.sh nc www.w3.org                   # the whole loop
```

| Layer | Needs | What breaks it |
| --- | --- | --- |
| `tests/test_gatekeeper.py`, `tests/test_audit.py` | nothing | A change to this recipe's own logic. |
| `tests/test_sandbox_conformance.py` | a sandbox | A platform change under us. |
| `tests/test_agent_behavior.py` | a sandbox, minutes | The agent no longer researching, or no longer honouring the boundary. |
| `scripts/verify.sh` | a sandbox | The approval path end to end. |

`pytest` is the only requirement for the first line — no `pytest-asyncio`
(coroutine tests run through a small `run()` helper) and no Pipecat (nothing
under `tests/` imports the voice loop). Without `RESEARCH_SANDBOX` the live
suites skip rather than fail.

Two tests worth knowing by name, each written after the live run that exposed
the bug it now guards:

- `test_an_answer_for_the_wrong_host_opens_nothing` — the bot once asked about
  one host and opened another.
- `test_claiming_to_have_tried_a_source_never_contacted_is_flagged` — an answer
  said it had attempted arXiv when the gateway saw no connection to it.

`verify.sh` proves the boundary end to end: a source is blocked, the block
surfaces for approval, approving changes policy, and the sandbox can then reach
it. The last step is the one that matters — an approval that reports success
while the next fetch still fails is precisely the failure it exists to catch.

Full detail, including how to run one area at a time, is in
[docs/verify-functionality.md](docs/verify-functionality.md).

---

## 3. Use it live

The voice loop needs Pipecat and a speech profile, so it runs in its own
environment. Everything up to this point runs on a bare `python3`:

```console
$ uv venv && uv pip install "pipecat-ai[cartesia,deepgram,openai,runner,webrtc]"
$ source .venv/bin/activate
```

**By voice:**

```console
$ python -m voice.bot -t webrtc --port 7860
```

Open the printed URL and ask for something that needs sources it does not have:

> *"Research how NVFP4 compares to FP8 for inference throughput and accuracy
> loss, and tell me which workloads each one suits. Check primary sources."*

You get a short acknowledgement, then the agent works. When it reaches a source
outside its policy the bot interrupts with one question at a time. Say **yes**
and that host opens, read-only, for the binary that asked — and the agent is
sent back to it. Say **no** and it records the source as unavailable and
carries on.

What to listen for:

- The question names a host, not a URL spelled out character by character.
- An ambiguous answer opens nothing and the question is asked again.
- "No" is reported as left blocked, not as an error.
- The voice loop stays responsive while the agent is working.

**From a terminal instead** — no Pipecat, no speech credentials, same
boundary:

```console
$ python3 -m gatekeeper.console --sandbox nc
$ python3 -m gatekeeper.console --sandbox nc --dry-run   # shows commands only
```

**Between runs**, put the sandbox back to a clean baseline, or the second demo
is quieter than the first because the agent no longer has to ask:

```console
$ ./scripts/reset-approvals.sh nc
```

---

## Why voice

The voice loop is not a skin on a terminal. It is load-bearing for two reasons:

- **The agent is stalled and you are not watching.** A blocked source pauses
  one line of inquiry. Notification that waits for you to look at a screen is
  notification that arrives after the sweep has finished doing what it could.
- **The decision is small and the context is large.** "Should I read arxiv.org"
  is a one-word answer wrapped in enough context that a notification badge
  cannot carry it. Speech carries both in about four seconds.

Meanwhile the sweep keeps working on the lines of inquiry that do not depend on
the blocked source, so asking is cheap.

## What an approval grants

One host and port, **read-only**, for **one binary**, for the life of the
sandbox. All three are taken from the denial event rather than from anything
the agent said. Loopback, link-local, private, and `.internal` addresses are
refused no matter who approves them — which covers `169.254.169.254`, the
standard path from "read a web page" to credential theft.

The binary scope is not decorative. An endpoint added without `--binary` merges
into the policy and **still fails at connect time**, because OpenShell resolves
rules per calling binary. This was found by running the recipe against a live
sandbox, not by reading the docs, and it is the single easiest way to build
something that reports success while nothing works.

Read [docs/security-model.md](docs/security-model.md) before changing anything
in `gatekeeper/`. It also states plainly what this recipe does *not* defend
against.

## Checking the answer against what was actually read

An agent's answer is not evidence of what it consulted. Two observed cases:
one run cited arXiv after making no network request at all, and another said
it had *attempted* arXiv when the gateway saw no connection to it.

Neither is visible in the transcript. Both are plainly visible at the gateway,
which the gatekeeper is already watching. So it also records every allowed
fetch, and audits each final answer against that record:

```text
cited      : ('arxiv.org',)
consulted  : ()
unverified : ('arxiv.org',)
warning    : Heads up: the answer refers to arxiv.org, but the agent never
             actually tried to reach it during this run.
```

The warning is deliberately narrow. Naming a source you genuinely tried and
were denied is the honest reporting this recipe asks for, and stays silent.
Only a source the agent never contacted at all is worth interrupting a
listener over.

It is a reporting aid, not a security control: it sees which hosts were
contacted, never what came back.

## Layout

```text
gatekeeper/     host-side only. Watches the gateway, asks, applies approvals,
                and audits answers against what was actually fetched.
voice/          the Pipecat voice loop and its workers.
agents/openclaw/  SOUL.md, the research-sweep skill, baseline policy.
scripts/        setup, verify, reset-approvals, teardown.
docs/           security model and verification.
tests/          unit tests, plus opt-in sandbox and agent conformance suites.
```

`gatekeeper/` never imports from `voice/`. The two halves meet in exactly one
file, `voice/gatekeeper_worker.py`, which keeps both independently testable.

## Teardown

```console
$ ./scripts/teardown.sh nc
```

Removes the baseline preset and lists any runtime approvals with the command to
drop them. Recreating the sandbox resets policy to the baseline.

## Known limitations

- **A broad allowlisted host defeats the boundary.** Observed, not theorized:
  denied `arxiv.org`, the agent fetched the same papers through
  `huggingface.co` — allowed by the `huggingface` preset — and said so in its
  own answer. The operator was never asked. `SOUL.md` forbids exactly this and
  the model did it anyway. Removing that one preset and changing nothing else
  closed the route: the agent found no way around and said plainly which
  sources it could not reach. Prompt instructions are not a security control;
  policy is, and that is measured rather than argued. Run a research sandbox
  without general-purpose presets; `setup.sh` warns when it finds them. Both
  runs are in [docs/security-model.md](docs/security-model.md).
- **Path denials never reach you.** OpenShell enforces at two layers, and the
  gatekeeper reads only the connection layer. A request refused by a path rule
  is invisible to the approval loop, and approving a host grants the whole host
  rather than one page.
- **No web search tool is configured.** The agent can fetch URLs it can name
  but cannot search, so it leans on hosts it already knows. Its own words:
  "the available web search tool failed because SearXNG is not configured."
  Research quality suffers, and a search backend would need its own endpoint
  approved.
- **Reusing a session key hides whether research happened.** A repeat question
  in the same session can be answered from what that session already found,
  with no network request, while still reading like fresh research. Use a
  distinct session per evaluation run, or check the OCSF log.
- Approvals persist for the life of the sandbox, not just the sweep. To revoke
  early, use `./scripts/reset-approvals.sh` or
  `openshell policy update --remove-endpoint`.
- The spoken-answer backstop reads the leading word, so a trailing qualifier
  ("sure, but check the date") is lost. The voice model resolves phrasing
  before it reaches that backstop.
- The gatekeeper tails `openshell logs`, which replays recent history on start.
  Denials older than the session are ignored, but the log window is the reason
  that filter exists.
- Speaker identity is not authentication. See the security model.

## Credits

Built by the Pipecat team at [Daily](https://www.daily.co). Pipecat is an
open-source framework for voice and multimodal agents:
[pipecat-ai/pipecat](https://github.com/pipecat-ai/pipecat).

The voice loop under `voice/` is vendored from
[chadbailey59/agent-voice-bot](https://github.com/chadbailey59/agent-voice-bot)
at commit `de9d6de`, with the gatekeeper wiring added here.

Questions or issues with this example: open an issue on the upstream repository
above, or contact chadbailey@gmail.com.
