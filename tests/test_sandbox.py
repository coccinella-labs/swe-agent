"""Tests for the sandbox executor seam; everything stays offline."""

import subprocess
from pathlib import Path

import pytest

from agent import SWEAgent
from conftest import FakeResult
from sandbox import SANDBOX_ENV, DockerExecutor, LocalExecutor, executor_from_env


def _docker_present(monkeypatch):
    import sandbox

    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")


def _docker_daemon_up(monkeypatch):
    import sandbox

    _docker_present(monkeypatch)
    monkeypatch.setattr(
        sandbox.subprocess, "run", lambda cmd, **kw: FakeResult(0, stdout="ok")
    )


# --- selection -------------------------------------------------------------


def test_local_is_default():
    assert isinstance(executor_from_env({}), LocalExecutor)


def test_env_variable_selects_docker(monkeypatch):
    _docker_daemon_up(monkeypatch)
    executor = executor_from_env({"SWE_AGENT_SANDBOX": "docker"})
    assert isinstance(executor, DockerExecutor)


def test_unrelated_env_value_falls_back_local():
    assert isinstance(executor_from_env({"SWE_AGENT_SANDBOX": "banana"}), LocalExecutor)


# --- fail-closed construction -----------------------------------------------


def test_docker_construction_requires_binary(monkeypatch):
    import sandbox

    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="not found"):
        DockerExecutor()


def test_docker_construction_requires_daemon(monkeypatch):
    import sandbox

    _docker_present(monkeypatch)
    monkeypatch.setattr(
        sandbox.subprocess, "run", lambda cmd, **kw: FakeResult(1, stderr="boom")
    )
    with pytest.raises(RuntimeError, match="daemon not reachable"):
        DockerExecutor()


def test_agent_construction_fails_loud_when_docker_requested_but_missing(
    no_config, monkeypatch
):
    import sandbox

    monkeypatch.setenv(SANDBOX_ENV, "docker")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="docker"):
        SWEAgent("t")


# --- the container contract --------------------------------------------------


def test_docker_run_carries_the_full_contract(monkeypatch, tmp_path):
    import sandbox

    _docker_daemon_up(monkeypatch)
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        if cmd[:2] == ["docker", "info"]:
            return FakeResult(0, stdout="ok")
        return FakeResult(0, stdout="container output")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    executor = DockerExecutor()
    result = executor.run(["pytest", "-q"], cwd=tmp_path)
    assert result.stdout == "container output"
    argv = seen[-1]
    for flag, value in (
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--tmpfs", "/tmp:rw,size=512m"),
        ("--user", "1001:1001"),
        ("--memory", "4g"),
        ("--memory-swap", "4g"),
        ("--cpus", "2"),
        ("--pids-limit", "256"),
    ):
        i = argv.index(flag)
        assert argv[i + 1] == value, f"{flag} {value}"
    assert argv.index("--read-only") is not None
    mounts = [v for v in argv if v.endswith(":/work:rw")]
    assert mounts == [f"{tmp_path.resolve()}:/work:rw"]
    assert argv.count("-v") == 1
    assert argv.count("-e") == 0
    assert argv.count("-w") == 1 and argv[argv.index("-w") + 1] == "/work"
    joined = " ".join(argv)
    assert "ANTHROPIC_API_KEY" not in joined
    assert "GEMINI_API_KEY" not in joined
    assert "GH_TOKEN" not in joined


def test_docker_run_demands_a_cwd(monkeypatch, tmp_path):
    _docker_daemon_up(monkeypatch)
    executor = DockerExecutor()
    with pytest.raises(ValueError, match="cwd"):
        executor.run(["ls"])


def test_timeout_kills_the_named_container(monkeypatch, tmp_path):
    import sandbox

    _docker_present(monkeypatch)
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        if cmd[:2] == ["docker", "info"]:
            return FakeResult(0, stdout="ok")
        if cmd[:2] == ["docker", "kill"]:
            return FakeResult(0, stdout="killed")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 300))

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    executor = DockerExecutor()
    with pytest.raises(subprocess.TimeoutExpired):
        executor.run(["pytest", "-q"], cwd=tmp_path, timeout=5)
    kill = [c for c in seen if c[:2] == ["docker", "kill"]]
    assert len(kill) == 1
    assert kill[0][2].startswith("swe-agent-")


def test_run_cwd_mounts_a_resolved_path(monkeypatch, tmp_path):
    import sandbox

    _docker_daemon_up(monkeypatch)
    seen = []
    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda cmd, **kw: (
            FakeResult(0, stdout="ok")
            if cmd[:2] == ["docker", "info"]
            else seen.append(cmd) or FakeResult(0, stdout="")
        ),
    )
    executor = DockerExecutor()
    weird = tmp_path / "clone"
    weird.mkdir()
    executor.run(["git", "status"], cwd=Path(weird) / ".." / "clone")
    argv = seen[-1]
    mount = argv[argv.index("-v") + 1]
    assert mount == f"{weird.resolve()}:/work:rw"


# --- agent wiring ----------------------------------------------------------


def test_ops_route_through_executor(no_config, tmp_path):
    ag = SWEAgent("t", tmp_base=str(tmp_path))
    clone = ag.get_repo_path("fx") or tmp_path / "coccinella-labs" / "fx"
    clone.mkdir(parents=True, exist_ok=True)
    (clone / "tests").mkdir(exist_ok=True)
    seen = []

    class FakeExec:
        def run(self, cmd, cwd=None, timeout=300):
            seen.append((cmd, cwd, timeout))
            return FakeResult(0, stdout="1 passed")

    ag.executor = FakeExec()
    result = ag.execute_task({"repo": "fx", "operation": "test"})
    assert result["status"] == "ok"
    cmd, cwd, timeout = seen[0]
    assert cmd[1:3] == ["-m", "pytest"]
    assert cwd == clone
    assert timeout == 600
