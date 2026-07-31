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

## What you get

- An OpenClaw agent in a NemoClaw sandbox with a `research-sweep` skill and a
  written rule that a blocked source is a boundary, not an obstacle to route
  around.
- A host-side **gatekeeper** that reads OpenShell's OCSF denial stream, asks
  about each blocked source once, and applies approvals with the narrowest
  scope the denial allows.
- A **Pipecat voice loop** that interrupts you with the question and relays
  your answer — and that stays responsive while the agent works.
- A terminal path for the same boundary, for CI or when you are already at a
  keyboard.

## Prerequisites

- NemoClaw with a sandbox running the OpenClaw agent (`nemoclaw onboard`).
- `openshell` on the host `PATH`. **Do not install it in the sandbox.**
- Python 3.12+.
- For voice: credentials for a speech profile (see `.env.example`). The
  terminal path needs none.

## Setup

```console
$ cd examples/recipes/partners/pipecat/research-assistant
$ ./scripts/setup.sh nc
$ cp .env.example .env
$ echo "OPENCLAW_TOKEN=$(nemoclaw nc gateway-token --quiet)" >> .env
```

The Gateway token is required, not optional. OpenClaw auto-pairs only the
control UI and webchat, and this bot is neither, so without a token every run
fails with `device identity required`. Check the Gateway port with
`nemoclaw list` — a sandbox publishes its own (18790 here), which is not
OpenClaw's default 18789.

`setup.sh` applies a deliberately small baseline policy and installs the skill.
The baseline is small on purpose: a generous one makes the demo quiet and the
boundary meaningless.

## Run it

Approve by voice:

```console
$ python -m voice.bot -t webrtc --port 7860
```

Then say: *"research how NVFP4 quantization compares to FP8 for inference."*

Approve from a terminal instead:

```console
$ python -m gatekeeper.console --sandbox nc
```

Add `--dry-run` to see what an approval would run without changing policy.

## Verify

```console
$ python -m pytest tests -q        # no sandbox needed
$ ./scripts/verify.sh nc www.w3.org
```

`verify.sh` proves the whole boundary against a live sandbox: a source is
blocked, the block surfaces for approval, approving changes policy, and the
sandbox can then reach it. Full detail in
[docs/verify-functionality.md](docs/verify-functionality.md).

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

## Teardown

```console
$ ./scripts/teardown.sh nc
```

Removes the baseline preset and lists any runtime approvals with the command to
drop them. Recreating the sandbox resets policy to the baseline.

## Layout

```text
gatekeeper/     host-side only. Watches the gateway, asks, applies approvals,
                and audits answers against what was actually fetched.
voice/          the Pipecat voice loop and its workers.
agents/openclaw/  SOUL.md, the research-sweep skill, baseline policy.
scripts/        setup, verify, teardown.
docs/           security model and verification.
tests/          unit tests; no sandbox or credentials required.
```

`gatekeeper/` never imports from `voice/`. The two halves meet in exactly one
file, `voice/gatekeeper_worker.py`, which keeps both independently testable.

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
  early, use `openshell policy update --remove-endpoint`.
- The spoken-answer backstop reads the leading word, so a trailing qualifier
  ("sure, but check the date") is lost. The voice model resolves phrasing
  before it reaches that backstop.
- The gatekeeper tails `openshell logs`, which replays recent history on start,
  so denials from just before startup may be raised once.
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
