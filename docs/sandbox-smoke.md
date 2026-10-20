# Sandbox Image Smoke Test

Procedure for building the agent's sandbox image and proving its contract
by execution, per docs/sandbox-design.md. The declaration in CI is the
standing gate (`sandbox-image` job in `.github/workflows/ci.yml`): it
builds the image and runs `ruff check .` inside a container carrying the
full contract. This document is the local variant.

## Prerequisites

- A running docker daemon. On this Mac the choice is colima:

```sh
colima start --cpu 2 --memory 4
```

- The smoke clone must live under `$HOME`: colima's default share set
  covers the user home and not the system temp tree, so a clone under
  `/tmp` mounts into the container as an empty directory (observed in
  the first smoke run; the suite "collected no tests" while the same
  mount under `$HOME` passed 90 tests). Use something like
  `~/.cache/coccinella-labs/sandbox-smoke-repo`.

- The repo pinned at the commit to certify.

## Build

```sh
docker build -f docker/sandbox.Dockerfile -t coccinella-labs/swe-agent-sandbox .
```

The image contains python 3.12, git, pytest 9.1.1, ruff 0.17.1, and a
user 1001. Everything else is absent by design; tests needing new
pip-installed dependencies fail closed inside the container.

## Probes

Three probes, in order of what they prove:

1. **Non-root execution**: the container runs as 1001, not root.

```sh
docker run --rm --user 1001:1001 coccinella-labs/swe-agent-sandbox id -u
# expected: 1001
```

2. **Zero egress**: with `--network none`, outbound connections refuse at
   the name-resolution step, in the container, while the same code
   outside the container succeeds. This is the boundary from the design
   doc proven by execution, not by flag reading.

```sh
docker run --rm --network none coccinella-labs/swe-agent-sandbox \
  python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)"
# expected: nonzero exit; stderr contains the DNS resolution failure
```

3. **The suite runs inside the contract**, through the real executor
   path, no network needed because the offline suite needs none:

```sh
git clone . /tmp/swe-agent-smoke || true
# then, with the executor (DockerExecutor) wrapping the following argv:
#   ["python", "-m", "pytest", "-q"]
# and cwd=/tmp/swe-agent-smoke
# expected: the 90-test suite passes inside the container
```

The scripted form of probe 3 uses `SandboxExecutor` from the repo itself,
so it certifies the exact seam the agent ops call, flags and wiring
included, not a hand-typed docker command.

## Cleanup

```sh
colima stop      # or: colima delete if the VM and image are not needed again
```

## What this proves, and what it does not

Proven by these probes: non-root execution, no outbound network, the
suite green under the full contract flags, and the executor seam working
against the real docker daemon.

Not proven here: hard resource-ceiling behavior (the flags are present in
every invocation and CI guards them textually; a dedicated stress probe
is a future refinement), image digest pinning (the Dockerfile pins the
version tag; a digest pin requires a multi-arch storage decision and is
tracked as an open question from the design doc), and malicious-workload
resistance beyond the namespace defaults.
