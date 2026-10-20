# CI Label-Trigger Design

Companion to docs/sandbox-design.md. That document settles where workload
code runs and where the key lives; this one settles how a maintainer's
single label add starts a paid, sandboxed solve run on GitHub CI and what
can never happen as a result. Every point marked DECISION is meant to be
settled in PR review before implementation starts.

## 1. Threat model

What the trigger adds over a maintainer typing `solve` locally:

- The model receives issue text, which is attacker-controllable content.
  In that context issue text is data being summarized, never a rule to
  change the loop's own contract. The solve-command allowlist already
  enforces this; the duplicate enforcement here is fewer verbs, not
  different ones.
- Any repo code is executed only inside the zero-egress container, with
  the runner as host-side controller, exactly as the sandbox design
  specifies. The key stays on the runner. Nothing about the trigger
  widens the container contract.
- The automation's write capability is exactly: create a branch, apply
  a diff to it, push, open a draft PR, and comment on the issue. It
  cannot approve, cannot merge, cannot close, cannot label. The planned
  stopping point is a human reviewing a diff.

The residual risks worth stating plainly:

- Cost abuse: repeated label add/remove could stack paid runs. Answered
  by the event seam section below.
- Prompt injection driving wasted turns: partially mitigated by hard
  turn and cost ceilings that issue text cannot raise.
- Key exposure: answered by the key-handling section; the container
  argv never carries it, and CI adds no new carrier.

## 2. Event seam

- Trigger: `on: issues: types: [labeled]`, with a step that refuses to
  proceed unless `github.event.label.name` is exactly the ai-fix label.
  Labeling is the maintainer's explicit act; the run therefore has a
  human authorization step built in before any spend.
- Race check at job start: re-read the issue's current labels via the
  API and abort if ai-fix was removed by then, so a queued run dies the
  moment a maintainer revokes the request.
- Concurrency: `group: ai-fix-${{ github.event.issue.number }}` with
  cancel-in-progress, so one issue never carries two paid loops; a
  second label add restarts rather than stacks.
- The workflow reacts only to `labeled` events. It never self-triggers:
  the automation adds comments and labels on PRs, not on issues, and
  the ai-fix label is only ever added by a human (decided below).

DECISION (recommended): the automation may remove the ai-fix label from
the issue after opening the PR comment, marking the request as consumed;
a maintainer re-adds it to request a re-run. Keeps one label as the
complete protocol.

## 3. Security boundary: same topology, CI-shaped

The runner is the host-side controller from docs/sandbox-design.md
section 2; nothing in this workflow pushes repo code onto a raw runner
shell.

- Job step order: checkout target repo clone, build the sandbox image
  from docker/sandbox.Dockerfile (or pull from a registry if the supply
  decision lands that way), then invoke the solve loop through the same
  driver with SWE_AGENT_SANDBOX=docker. The executor builds the contract
  argv; the workflow does not hand-type docker flags.
- Deliverable: the solve's diff and JSONL transcript are uploaded as
  run artifacts before anything else happens. Evidence lands even if
  later steps fail.
- Push step: a separate gated step applies the diff, pushes the branch
  `ai-fix/issue-<n>`, and opens a draft PR that references the issue
  with "Refs #<n>" (never any closing keyword; acceptance stays with
  the human). Attribution text in the PR body states it was generated
  by the label trigger and carries the measured cost summary.
- Issue comment: one comment with the PR link, test result, lint
  result, turn count, and estimated cost, all from the transcript.
- Token: the workflow's GITHUB_TOKEN with contents:write. No PAT, no
  app identity, no admin token anywhere in the run.

DECISION (recommended): the draft PR is left draft; flipping to ready
is the maintainer's act, mirroring the local rule that a human performs
every publish beyond the evidence artifacts.

## 4. Cost and token guards

Enforced in three independent layers so no single rejection point can
be talked around:

- Workflow layer: the workflow sets SWE_AGENT_MAX_TURNS and a cost cap
  ceiling env before starting the solve; the driver clamps any task
  input above it, so larger values cannot arrive from issue text.
  Proposed ceilings: turns 15, cost 1.50 USD per run.
- Driver layer: unchanged from current code; the cap exhaustion path
  ("cap exhausted", refusal of the next call) already exists and is
  what the clamp feeds into.
- Time layer: the job carries a hard timeout (20 minutes, settled in
  review) so
  a wedged container cannot outlive the runner's patience.

DECISION (recommended): no aggregate spend ledger in the first
implementation. Per-run ceilings plus the human label gate bound spend
to (maintainer actions x 1.50 USD), which is auditable from history.
A monthly cross-issue ledger is a separate future design if needed.

## 5. Scope: which repos does the trigger serve

DECISION (recommended): first implementation lives in swe-agent itself
and solves starters and swe-agent issues only, the dogfood case that
matches how every earlier lane was validated. For other coccinella-labs
repos the choice is copying the workflow into each repo (per-repo
secret management) or a central hub repo driven by repository_dispatch;
that is deliberately deferred to a follow-up design once the in-repo
version has run at least once for real.

## 6. Key handling in CI

- ANTHROPIC_API_KEY enters the job through a repository secret scoped
  to this workflow's environment, never through the container.
- The container contract already refuses to carry the key in argv; the
  existing argv-contract tests prove that at the unit layer. CI adds a
  second, cheap assertion: after the solve, grep the transcript for the
  key's first eight characters and fail if present, closing the loop on
  the artifact rather than the contract alone.
- The key is never echoed, never printed in summary lines, and the
  transcript sanitizer checks both stdout and stderr captures.

## 7. Offline test plan (before any workflow code)

- Clamp tests: task asks 100 turns or 5 USD; driver clamps to the env
  ceilings; the clamp message is deterministic and testable.
- Trigger matrix: table documenting every label lifecycle event and the
  expected effect (proceed, abort, restart), reviewed as prose since
  the workflow YAML itself can only be linted, not unit-tested.
- Refs-not-closes: a test asserting the PR body template contains
  "Refs #" and no closing keyword strings.
- Transcript key scan: the artifact-grep step is locally reproducible
  against the existing dry-run transcript fixture.

## 8. Open questions for review

Settled in review on 2026-10-20; recorded here so the canon carries the
answers, not just the proposals:

1. Exact caps: turns 15 and 1.50 USD stand; job timeout 20 minutes
   (shortened from the proposed 30) per the reviewer's risk call.
2. Image supply: build from the Dockerfile per run; no registry, no
   GHCR publishing in v1.
3. Label naming: exactly ai-fix; the workflow file must match.

The four in-text DECISION items are likewise settled as recommended:
label consumed by automation after use, draft PR until a human flips
it, no aggregate spend ledger in v1, first implementation scoped to
swe-agent itself.
