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
- CI: landed meanwhile. The label-trigger workflow shipped in PR #8,
  the interim selection `SWE_AGENT_PROVIDER=gemini` with
  `gemini-3.8-flash` landed in PR #14 (maintainer decision while the
  Anthropic console account had no API credit balance), and the
  solve-step `GH_TOKEN` export exists to keep `gh issue view` working
  on the runner (runs 39356118027, 39356775752, 39357533789 on
  issue #2 recorded the death and the regression and the fix in
  PR #13 and PR #15). Both provider secrets now sit in the repo
  settings, scanned by the artifact leak scan.
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
   (Executed: shipped in PR #11 as v1.12.0.)
2. Whether `SWE_AGENT_PROVIDER` should also be readable from the task
   JSON (`"provider": "gemini"`), or environment-only in v1;
   environment-only is the recommendation, keeping the task schema
   untouched. (Executed: environment-only shipped in PR #11.)
3. Exact `PRICES` numeric entries for the selected models come from
   ai.google.dev/gemini-api/docs/pricing at implementation time, with
   the page state recorded in the PR body as evidence, since pricing
   pages drift faster than this doc. (Executed: 0.75/3.75 landed in
   PR #11, fetched 2026-10-20.)
4. The Queued driver lane off the same evidence: add
   `claude-sonnet-5-5` to PRICES and redefault `SWE_AGENT_MODEL`
   before the first live run on issue #2. (Executed: PR #10, v1.11.1.)

## 9. Third provider candidate: openrouter/auto (evidence 2026-10-20)

Maintainer direction arrived with a live `sk-or-v1` key: evaluate
OpenRouter's auto router as a third provider behind the seam.

### 9.1 Doc and live evidence, recorded here

- Endpoint (openrouter.ai/openrouter/auto/llms.txt, fetched
  2026-10-20): `POST https://openrouter.ai/api/v1/chat/completions`,
  Bearer auth. Model id `openrouter/auto`. Not available through EU
  in-region routing.
- Schema: OpenAI Chat-compatible. `messages` take native roles
  `system` / `user` / `assistant` (no role mapping needed, unlike the
  Gemini seam's assistant-to-model map); output cap is `max_tokens`.
- Response: `choices[0].message.content`, normalized
  `finish_reason` (`stop`, `length`, `tool_calls`, `error`),
  `model` echoing the actually routed model, and `usage` carrying
  `prompt_tokens` / `completion_tokens` / `total_tokens`,
  `completion_tokens_details.reasoning_tokens`, and `cost`: the
  request's cost in credits, directly comparable to USD.
- Live probe with the handed-over key (recorded here, GET-met
  first via `/api/v1/auth/key`: HTTP 200, label
  `sk-or-v1-443...e76`, free tier, `usage` 0, no hard limit):
  one call routed to `deepseek/deepseek-v4.1-flash-1017`,
  HTTP 200, `usage.cost` 2.01e-05, `finish_reason` length, and
  `content` None after 8 max_tokens, because the routed reasoning
  model consumed the entire output budget thinking before emitting
  any text. Two consequences for design below.
- Error mapping: 401 invalid key, 402 insufficient credits,
  403 spend limit, 429 rate limit, 502 upstream failure (failed
  generations are not billed).

### 9.2 Proposed decisions, awaiting review

1. Provider name `openrouter`, default model `openrouter/auto`,
   selected by `SWE_AGENT_PROVIDER=openrouter`, environment-only like
   the other two; key from `OPENROUTER_API_KEY` then
   `/tmp/coccinella-labs/openrouter_key` (0600), placeholder refused
   at construction.
2. Cap accounting for this provider is **response-reported**:
   `usage.cost` is the authoritative estimate and accumulates into
   the same ledger and cap. No static `PRICES` row is possible for a
   router whose per-call routing (and pricing) changes; if a response
   comes back without `usage.cost`, the driver fails closed with the
   standard no-price error rather than guessing.
3. `finish_reason: length` with empty or missing content is a clean
   `DriverError` (fail fast), not a loop turn: the probe showed a
   reasoning-hungry routed model can burn the whole output budget on
   thinking; burning solve turns on that is waste. Recommendation on
   record: `max_output_tokens` should default higher than 2048 for
   this provider (say 4096) unless config says otherwise.
4. The routed model flows into `usage_summary` as
   `routed_model`, per-call, so evidence files show what actually
   answered.

### 9.3 New open questions from this addition

1. Implement now, in what order relative to the pending CI retest of
   PR #15 on issue #2? Author's recommendation: land PR #15 and its
   retest first (they are one merge away from a fully proven surface),
   then the openrouter seam lane as `feat:` (v1.13.0).
2. After the Anthropic credits land: does the CI interim posture stay
   gemini, or does the label test move to openrouter/auto's free-tier
   credits? Author's recommendation: keep gemini for CI determinism
   (a router that changes models mid-benchmark makes evidence harder
   to read), use openrouter/auto on-host for cheap exploratory solves.
3. The automatic fallback lane proposed earlier (anthropic attempt,
   credential-class failure, then gemini) remains unadopted by
   decision; re-evaluate it once three providers exist, since a
   try/except over provider selection becomes cheap then.

