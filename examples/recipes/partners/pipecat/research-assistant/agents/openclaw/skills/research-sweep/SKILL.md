---
name: research-sweep
description: Run a long, multi-source research sweep, stopping at the sandbox policy boundary rather than routing around it.
---

# Research sweep

## When to use this

Any question that cannot be answered from what you already know, and that
deserves more than one source. A sweep is a long job. Twenty minutes of
careful work beats two minutes of guessing.

## Procedure

1. **Frame the question.** Write `<placeholder_question>` at the top of
   `workspace/reports/NOTES.md`, then list the lines of inquiry that would
   have to be true for an answer to hold up. Aim for three to six.

2. **Plan the sources.** For each line of inquiry, name the kind of source
   that would settle it: primary documentation, a peer-reviewed paper, a
   standards body, a vendor's own numbers, a reputable secondary summary.
   Write the list down before fetching anything.

3. **Work the lines of inquiry.** Take them one at a time. For each source you
   consult, record in `NOTES.md`: what you were checking, what the source
   said, and whether it settled the question.

4. **When a fetch is blocked**, stop that line of inquiry and move to the next
   one. Record the blocked host in `NOTES.md` under "Waiting on approval".
   Read "The policy boundary" below. Do not attempt a workaround.

5. **Reconcile.** When sources disagree, say so and say which you find more
   credible and why. Do not average them into a bland statement.

6. **Write the report** to `workspace/reports/<placeholder_slug>.md`: the
   answer first, then the evidence, then what you could not verify and why.
   Anything you could not check because a source stayed closed goes in that
   last section by name.

## The policy boundary

Your sandbox denies egress by default. A blocked fetch is not a bug and not a
transient failure. It means the destination is outside what your operator has
approved so far.

When you hit one:

- **Do** note it and continue with other work.
- **Do** state plainly, if asked, which source you are waiting on and what you
  wanted from it.
- **Do not** retry the same host repeatedly. The first denial is the answer.
- **Do not** look for the same content on another host, a cache, an archive,
  a mirror, a proxy, or a search-engine snapshot.
- **Do not** ask the operator to disable the policy, and do not attempt to
  edit any policy file.

Your operator is asked out loud whether to open each blocked source. That
decision happens outside this sandbox, on their machine. You cannot make it,
influence it by insisting, or work around it.

## Output

- `workspace/reports/NOTES.md` — the running log, updated as you go.
- `workspace/reports/<placeholder_slug>.md` — the final report.

Spoken summaries are heard, not read: plain sentences, no markdown, no URLs
spelled out. Name the publication instead of reciting the link.
