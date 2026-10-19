"""Sandbox execution for agent ops; blueprint: docs/sandbox-design.md.

Two executors share one interface identical to subprocess.run so the ops
can hand them command lists unchanged. Selection is by the
SWE_AGENT_SANDBOX env var; everything except the value docker runs locally,
which is today's behavior verbatim.
"""

import subprocess


SANDBOX_ENV = "SWE_AGENT_SANDBOX"


class LocalExecutor:
    """Runs commands on the host; the behavior before any sandbox existed."""

    def run(self, cmd, cwd=None, timeout=300) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def executor_from_env(env=None) -> LocalExecutor:
    """Build the executor selected by SWE_AGENT_SANDBOX; local is the default.

    The docker-selected path arrives together with DockerExecutor.
    """
    return LocalExecutor()
