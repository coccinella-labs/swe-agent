# Container Sandbox Design

Status: design only, no container code exists yet. Decision points are marked
DECISION and are meant to be settled in PR review before implementation starts.

## 1. Threat model

The solve loop already constrains which files model actions may touch
(`_safe_path` in agent.py), but every op still runs as plain Python
subprocesses under the host user. What is not constrained today:

| Risk | Today | After this design |
| --- | --- | --- |
| Model-influenced code opening network connections | possible | impossible: container has no network namespace access at all |
| File writes outside the clone | blocked (path guard, code ops only) | blocked harder: process sees only /work |
| Reading host secrets (env, ssh keys, gh tokens) | possible in principle | impossible: secrets never enter the container |
| Runaway memory or CPU from tests | limited only by host | capped by container ceilings |
| Fork bombs / runaway process trees | limited only by host | capped by pids ceiling |
| Docker escape via socket sharing | n/a | never mounted |

Not defended by this design (stated for honesty): host kernel Docker escapes
out of scope; model edits may still wreck the clone itself; supply-chain risk
of the image is handled by pinning, not isolation.

## 2. Topology: the driver never enters the container

DECISION (recommended): zero-egress topology. The Anthropic driver keeps
running on the host exactly as it does today, including the transcript writer.
The container executes code only. Alternative rejected: allowing egress to
`api.anthropic.com` from inside the container. Reasons:

- Smaller surface: an allowlist is a network policy to get right and to
  review; `--network none` is a null policy.
- The driver needs streaming JSON, retries, and cost caps; keeping it host
  side means zero changes to driver.py.
- No credential of any kind, neither the Anthropic key nor gh tokens, ever
  lives inside the container.

Split of responsibilities:

- Host: clone provisioning (git, with credentials), driver, model loop,
  parsing, transcripts, timeouts, cost caps.
- Container: everything `_observe` executes today: view/search are pure
  file reads, edit is a file write, and diff/lint/test are subprocesses
  constrained to /work.

## 3. Container contract

One image, `docker/sandbox.Dockerfile`:

- Base: `python:3.12-slim` (the pinned suite versions from
  requirements-dev.txt are installed at image build time: pytest, ruff).
- git present. No pip at runtime; a test needing a new dependency fails
  closed rather than resolving it from the network.
- Image digest pinned in CI and locally; rebuilds are explicit, reviewed
  changes to docker/sandbox.Dockerfile.

Runtime flags for every containerized exec:

```
--network none
--cap-drop=ALL
--security-opt no-new-privileges
--read-only                  (container root filesystem)
--tmpfs /tmp:rw,size=512m
--user 1001:1001             (non-root, no shell home to write)
--memory 4g --memory-swap 4g (no swap)
--cpus 2
--pids-limit 256
-v <clone>:/work:rw          (the only rw mount; host-side clone path)
```

- Wall-clock ceilings stay host-side at their current values (60s for a
  single git call, 120s lint, 600s test), enforced by the existing `_run`
  timeout; the container adds a hard belt-and-braces via host-side docker
  kill on timeout.
- The docker socket is never mounted. Container results are text on stdout
  plus an exit code, nothing else crosses the boundary.

## 4. Agent integration seam

DECISION (recommended): introduce a `SandboxExecutor` with two
implementations sharing one interface identical to today's `_run`:

- `LocalExecutor`: current behavior, verbatim, the default.
- `DockerExecutor`: invokes `docker run` with the contract above, passing
  the same argv, cwd mapped to /work, timeout mapped to docker kill.

Selection: env var `SWE_AGENT_SANDBOX=docker|local`, default `local`.
Nothing changes for the issue #2 run or the existing suite until opted in.

Fail-closed rule: `SWE_AGENT_SANDBOX=docker` with docker absent or not
running is a loud error at executor construction, never a silent fallback
to local execution.

macOS note: Docker Desktop binds mounts are slower when pytest reads many
small files; sizes stay tiny in this repo, and the ceiling knobs above
absorb the variance. Linux CI runners bring docker preinstalled, so CI can
run the docker lane in a later step.

## 5. What the label trigger inherits

The future CI label trigger is a host-side controller that needs exactly
two things this design already settles: a cost-capped driver on a runner,
and an execution boundary that does not trust its workload. It gets both
for free from this topology, which is why closing the sandbox first is the
right order.

## 6. Offline test plan (before any docker code)

Following the FakeResult pattern in conftest.py:

- DockerExecutor argv contains all contract flags and only /work as an rw
  mount; captured command carries no environment containing
  ANTHROPIC_API_KEY or any secret substring.
- Timeout mapping: host timeout converts to the docker kill behavior.
- Fail-closed: missing docker binary raises at construction with the
  message the operator sees; no partial invocation started.
- LocalExecutor path unchanged: existing 79 tests keep passing untouched.

## 7. Open questions for review

1. tmpfs size for /tmp (512m proposed) versus the biggest repo pytest in
   the org; adjustable per-repo later.
2. uid mapping on Linux: chown the host clone to 1001 at provision time
   versus userns remap; propose chown for v1.
3. Should `lint` and `diff` run inside the container too (yes, proposed:
   all op execution goes through the executor, no per-op exceptions), or
   stay host-side for speed?
4. Image build cadence: manual on Dockerfile change, or a scheduled
   rebuild workflow with digest rotation?
