"""Tests for the sandbox executor seam; everything stays offline."""

from agent import SWEAgent
from conftest import FakeResult
from sandbox import LocalExecutor, executor_from_env


# --- selection -------------------------------------------------------------


def test_local_is_default():
    assert isinstance(executor_from_env({}), LocalExecutor)


def test_unrelated_env_value_falls_back_local():
    assert isinstance(executor_from_env({"SWE_AGENT_SANDBOX": "banana"}), LocalExecutor)


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
