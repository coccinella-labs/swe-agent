# The sandbox image for the agent's containerized ops; design in
# docs/sandbox-design.md, procedure in docs/sandbox-smoke.md.
#
# Deliberately minimal: git for the diff/search ops, the two pinned dev
# dependencies, a non-root nobody-adjacent user, and nothing else. No
# pip install happens at runtime, so a test that needs a new dependency
# fails closed instead of resolving it from a network it does not have.

FROM python:3.12-slim

ARG PYTEST_VERSION=9.1.1
ARG RUFF_VERSION=0.17.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest==${PYTEST_VERSION} ruff==${RUFF_VERSION} \
    && useradd -m -u 1001 runner

WORKDIR /work
USER 1001
CMD ["python", "-m", "pytest"]
