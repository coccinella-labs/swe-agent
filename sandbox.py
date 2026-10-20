"""Sandbox execution for agent ops; blueprint: docs/sandbox-design.md.

Two executors share one interface identical to subprocess.run so the ops
can hand them command lists unchanged. Selection is by the
SWE_AGENT_SANDBOX env var; the value docker requests the containerized
executor and fails close when docker is unavailable, everything else
runs locally, which is the behavior before any sandbox existed.
"""

import itertools
import os
import shutil
import subprocess
from pathlib import Path

SANDBOX_ENV = "SWE_AGENT_SANDBOX"

SANDBOX_IMAGE = "coccinella-labs/swe-agent-sandbox"
SANDBOX_WORK = "/work"


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


class DockerExecutor:
    """Runs commands in a locked-down container per the sandbox design.

    The container gets no network, no capabilities, no privileges, a
    read-only root filesystem, resource ceilings, exactly one rw mount at
    /work, and nothing else. No environment from the host crosses the
    boundary, so no secret can leak into the container through argv or
    inherited environment.
    """

    def __init__(self, image=SANDBOX_IMAGE):
        self.image = image
        self._seq = itertools.count()
        if shutil.which("docker") is None:
            raise RuntimeError(
                "docker daemon unavailable: binary 'docker' not found; "
                "unsetting SWE_AGENT_SANDBOX restores local execution"
            )
        try:
            result = subprocess.run(
                ["docker", "info"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"docker daemon unavailable: {exc}") from None
        if result.returncode != 0:
            raise RuntimeError(
                "docker daemon not reachable (docker info exited "
                f"{result.returncode}); unset SWE_AGENT_SANDBOX to run locally"
            )

    def run(self, cmd, cwd=None, timeout=300) -> subprocess.CompletedProcess:
        if cwd is None:
            raise ValueError("docker executor needs a cwd to mount at /work")
        cwd = str(Path(cwd).resolve())
        name = f"swe-agent-{os.getpid()}-{next(self._seq)}"
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=512m",
            "--user",
            "1001:1001",
            "--memory",
            "4g",
            "--memory-swap",
            "4g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "-v",
            f"{cwd}:{SANDBOX_WORK}:rw",
            "-w",
            SANDBOX_WORK,
            self.image,
            *cmd,
        ]
        try:
            return subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["docker", "kill", name],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            raise


def executor_from_env(env=None):
    """Build the executor selected by SWE_AGENT_SANDBOX; local is the default."""
    env = os.environ if env is None else env
    mode = str(env.get(SANDBOX_ENV, "local")).strip().lower()
    if mode == "docker":
        return DockerExecutor()
    return LocalExecutor()
