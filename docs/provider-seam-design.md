# Provider Seam Design (Multi-Model Driver)

Companion to docs/sandbox-design.md and docs/ci-label-trigger-design.md.
This document settles how a second model provider enters the solve loop
without touching the loop, the executor, the ceilings, or the offline
suite. Points marked DECISION are meant to be settled here before any
implementation starts.

## 1. Model liveness, verified against the official docs

The Gemini API models page (ai.google.dev/gemini-api/docs/models, page
updated 2026-10-19 UTC) changes the originally proposed targets:

- `gemini-2.5-pro` and `gemini-2.5-flash` still exist and are NOT
  deprecated, but access is restricted: "limiting access to the 2.5
  models to users who have actively used them in the past," and the
  page directs new projects to other models. A fresh API key may
  therefore be unable to ride the 2.5 endpoints at all.
- Already shut down (do not target): `gemini-2.0-flash`,
  `gemini-2.0-flash-lite`, `gemini-3-pro-preview`,
  `gemini-3.1-flash-lite-preview`, `gemini-2.5-computer-use`.
- Live, stable, and text-appropriate: `gemini-3.8-flash` (described on
  the same page as engineered for long-horizon software engineering
  and autonomous agents), `gemini-3.6-flash`, `gemini-3.5-flash-lite`,
  `gemini-3.1-flash-lite`.
- Live but preview (2-week-minimum shutdown notice): `gemini-3.1-pro
  -preview`, `gemini-3-flash-preview`. The Gemini 4 line exists as of
  yesterday's release notes (`gemini-4-argon` stable); pricing and
  behavior at this writing are unverified, so it stays out of the
  first cut.

DECISION (proposed, replaces the earlier 2.5 pairing): the Gemini side
targets `gemini-3.8-flash` as the default and `gemini-3.6-flash` as the
benchmark alternate, both stable. `gemini-3.1-pro-preview` may be added
to PRICES as the pro-analog with its preview caveat made explicit. The
2.5 pair is documented here but not enabled by default; a `model` the
caller names explicitly can still select it and it will fail closed at
the cap table when no price entry exists.

## 1a. The incumbent is not exempt, verified the same way

platform.claude.com model deprecations (fetched 2026-10-20): the
repository's default `claude-sonnet-4-5` resolves to
`claude-sonnet-4-5-20250929`, which was deprecated on 2026-09-30 with
a tentative retirement on 2026-11-30 and recommended replacement
`claude-sonnet-5-5` (current lineup, $2/$10 per MTok). Sonnet 4-5
remains callable today but sits inside a six-week retirement window,
and Anthropic's own guidance states deprecated models may be less
reliable. The live run on issue #2 therefore carries a deadline
consideration: either redefault before the first live run or accept
the deprecated endpoint knowingly. That redefault (PRICES entries for
`claude-sonnet-5-5` plus the `SWE_AGENT_MODEL` default change) is
approved to redefault; the targeted lane is PR #10 (fix: redefault driver model to claude-sonnet-5-5), which lands the PRICES row and README line alongside.

## 2. Non-goals

- No native Gemini function-calling in the loop: the solve loop
  communicates through text commands the loop parses itself, exactly
  as the Anthropic path does. A second provider therefore needs no
  tool-schema translation, only message and usage translation.
- No change to the CI ai-fix workflow: it stays Anthropic-secret-only
  per the label-trigger design's scope decision (swe-agent v1).
- No second-engine review bot (deferred by decision).
- The sandbox contract is untouched: keys stay host-side and out of
  container argv, whatever the provider.

## 3. Driver contract and the seam

The existing contract holds for both providers:

    chat(system, messages) -> ChatResult(
        text, stop_reason, input_tokens, output_tokens, cost_estimate
    )

with `CapReached` and `DriverError` keeping their exact semantics
(cost-cap exhaustion is enforced above the transport, so it transfers
by construction). `SWEAgent._make_driver` gains a provider step:

- `SWE_AGENT_PROVIDER` unset or `anthropic`: today's behavior, byte
  for byte.
- `gemini`: a stdlib REST client per section 4.
- anything else: a `DriverError`-style task error at task time, naming
  the unknown value. Fail-closed, no silent fallback, mirroring
  `SWE_AGENT_SANDBOX` selection semantics.

`SWE_AGENT_MODEL` continues to select the model within the provider;
the Anthropic default is redefaulted to `claude-sonnet-5-5` (see
section 1a), the Gemini default
per DECISION above. Per-provider price entries land in the same
`PRICES` table; model ids are unique across providers today, and the
"no price entry for model" error remains the guardrail for anything
unpriced.

## 4. Gemini REST shape, stdlib only

One module, no SDK, no new dependency, same offline posture:

- Endpoint: `POST /v1beta/models/{model}:generateContent` on
  generativelanguage.googleapis.com; the key travels in the
  `x-goog-api-key` header only.
- `system` becomes `systemInstruction`; the message history maps to
  `contents[]` with roles `user` and `model`; response text comes from
  `candidates[0].content.parts[*].text` (parts joined).
- Usage: `usageMetadata.promptTokenCount` and
  `candidatesTokenCount` map to `input_tokens` and `output_tokens`;
  the cost estimate multiplies through the same PRICES-to-estimate
  path the Anthropic driver uses, and the cap exhausts identically.
- Errors: nonzero HTTP status raises `DriverError` with the status
  code and Google's error message; a missing `GEMINI_API_KEY` raises
  the same fail-closed error class at driver construction, before any
  HTTP happens.

The exact schema facts above require the doc-fetch verification step
(this lane's step 2) to re-check against Google's current API
reference for request/response field names, and the findings go into
the PR body as evidence. Any name that does not match the reference
get fixed in the implementation commit, not silently.

## 5. Key and environment boundary

- Local: `GEMINI_API_KEY` in the environment, or the key file at
  `/tmp/coccinella-labs/gemini_key` (mode 0600), read-only to the
  driver; the file is deleted after any live run per the standing
  protocol. Placeholder strings are refused the same way the
  Anthropic dry-run gate refuses them.
- CI: a future lane only; the label-trigger workflow is out of scope
  here.
- The executor contract test that asserts no key ever enters container
  argv extends to cover the Gemini key name.

## 6. Offline test plan (before any HTTP-capable code)

Following the conftest FakeResult pattern:

- Provider selection: unset, `anthropic`, `gemini`, an unknown value,
  and an impossible value each produce the expected construction or
  task error; no env means today's exact behavior.
- Payload mapping: a captured request builder test pins system to
  systemInstruction, role mapping, and the parts-join on response.
- Usage mapping and cost estimate: fake usageMetadata flows through
  to ChatResult and the cap ledger identically to the Anthropic shape.
- Key handling: missing key, empty key, and a placeholder value all
  fail closed at construction with the operator-facing message.
- The executor argv contract grows its assertion list by the Gemini
  key name; the whole 115-test suite keeps passing untouched.

## 7. Benchmark posture, for when a key exists

Runs cost real money and stay under the same local ceilings (default
2 USD cap, turn clamp), so benching `gemini-3.8-flash` against
`gemini-3.6-flash` on the same task is two ordinary local `solve`
invocations with `SWE_AGENT_PROVIDER=gemini`, each producing its own
transcript, cost figures estimated by the shared `PRICES` entries.
The comparison is evidence in the transcript only; no automated
leaderboard lands in CI in this lane.

## 8. Open questions for review

1. The DECISION in section 1: default `gemini-3.8-flash`, alternate
   `gemini-3.6-flash`, pro-analog optional. Confirm or reweight.
2. Whether `SWE_AGENT_PROVIDER` should also be readable from the task
   JSON (`"provider": "gemini"`), or environment-only in v1;
   environment-only is the recommendation, keeping the task schema
   untouched.
3. Exact `PRICES` numeric entries for the selected models come from
   ai.google.dev/gemini-api/docs/pricing at implementation time, with
   the page state recorded in the PR body as evidence, since pricing
   pages drift faster than this doc.
4. The Queued driver lane off the same evidence: add
   `claude-sonnet-5-5` to PRICES and redefault `SWE_AGENT_MODEL`
   before the first live run on issue #2, or knowingly run on the
   deprecated 4-5 endpoint through its 2026-11-30 retirement. The
   fallback-free posture favors redefaulting; the exact lane shape
   (inside this seam implementation versus a separate small fix PR)
   is the author's recommendation only after your go.
