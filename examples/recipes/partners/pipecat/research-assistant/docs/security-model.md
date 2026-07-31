# Security model

The claim this recipe makes is narrow and worth stating precisely:

> A long-running agent can ask for more network reach than it was given, and a
> human can grant it by voice, without the agent ever being able to grant it
> to itself.

Everything below is what makes that true.

## The asymmetry

`openshell` is installed on the host and is **not** installed in the sandbox,
and the sandbox has no network route to it. That is the entire security
posture in one sentence.

| | Sandbox | Host |
| --- | --- | --- |
| Runs the research agent | yes | no |
| Can attempt a network fetch | yes | n/a |
| Can see that a fetch was denied | no | yes, via OCSF events |
| Can widen the network policy | **no** | yes, via `openshell` |

The agent produces a *request* by trying to fetch something. It never produces
a *decision*. The decision is made by a person and executed by a host-side CLI
the agent cannot invoke.

## What the model can and cannot influence

Model output never reaches the approval path. Concretely:

- The escalation is not something the agent writes. It is an OCSF `NET:OPEN`
  `DENIED` event emitted by the OpenShell gateway, and the host and port in it
  come from the intercepted connection, not from anything the model said.
- The agent cannot cause an approval by claiming one happened, by asking
  nicely, or by emitting text shaped like a policy command. There is no
  parsing of agent output anywhere in `gatekeeper/`.
- The agent cannot suppress a question either. It cannot see the denial stream.

This is the failure mode reviewers have flagged on other examples in this
repository: passing user or agent identity *through* the model and trusting it
on the other side. Here the model is not in the trust path at all.

## What an approval actually grants

An approved source is opened as narrowly as the denial allows:

- **One host and port**, taken from the denial event.
- **Read-only** (`GET`, `HEAD`, `OPTIONS`). This agent reads sources; it never
  posts to them. Widening a method is a policy-file decision, not something a
  spoken "yes" can do.
- **One binary**, also taken from the denial event. If `/usr/bin/curl` was
  blocked, only `/usr/bin/curl` is opened. A different binary reaching the same
  host raises a new question.
- **For the life of the sandbox.** Runtime approvals are not written to the
  baseline policy. Destroying and recreating the sandbox resets them.

The binary scope is load-bearing rather than decorative. An endpoint added
without `--binary` is merged into the policy and *still refused at connect
time*, because OpenShell resolves rules per calling binary. An implementation
that omits it will report success while every subsequent fetch fails.

## Hosts that are never opened

`gatekeeper/approver.py` refuses some endpoints regardless of what anyone says:

- `localhost`, and any `.internal`, `.local`, or `.localdomain` name.
- Any literal loopback, link-local, or private address. This covers
  `169.254.169.254`, the cloud instance-metadata endpoint, which is the
  standard target for turning "read a web page" into credential theft.
- Anything that is not a syntactically valid hostname. Values are refused
  rather than escaped, because they become subprocess arguments.

A refusal is spoken aloud. The operator said yes and is entitled to know it did
not happen.

## Reading a spoken answer

Only an unambiguous affirmative approves. Silence, a question, a hedge, or an
unrecognized phrase all leave the policy unchanged and leave the question
outstanding. The bias is deliberate: the cost of failing to open a source is
that research is slower, and the cost of wrongly opening one is a permanent
egress path for the life of the sandbox.

Two layers read the answer. The voice model resolves conversational phrasing
into a clear yes or no and calls `answer_policy_question`; the keyword pass in
`gatekeeper/service.py` is a conservative backstop underneath it, not the
primary reader. The backstop reads the leading word, so a trailing qualifier
("sure, but check the date") is lost - a known and tested behavior.

## What this recipe does not defend against

Stated plainly, because a security model that only lists its strengths is not
one:

- **A person who approves without listening.** Nothing here prevents that, and
  repeated questions make it likelier, which is why identical denials are
  collapsed and asked once.
- **A legitimately approved source serving hostile content.** Approving a host
  means the agent may read it. Prompt injection from a page the operator
  approved is out of scope.
- **Voice as identity.** Speaker identity is *not* an authentication factor
  here and must not be treated as one. This recipe assumes the person on the
  microphone is already the operator of the machine - they are speaking into a
  process running on the same host as `openshell`. If you expose the voice
  transport beyond the local machine, put real authentication in front of it.
  A telephone number is not authentication.
