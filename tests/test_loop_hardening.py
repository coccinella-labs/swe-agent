"""Loop-hardening tests: search cwd under sandbox and the op-crash guard."""

import json

from test_provider_seam import solve_repo, task  # noqa: F401

import driver


def test_op_search_passes_cwd_to_the_executor(solve_repo):
    a, clone = solve_repo
    seen = {}

    class Recorder:
        def run(self, cmd, cwd=None, timeout=300):
            seen["cwd"] = cwd
            seen["cmd"] = cmd
            import conftest

            return conftest.FakeResult(returncode=0, stdout="")

    a.executor = Recorder()
    a._op_search(clone, dict(task(), pattern="half"))
    assert seen["cwd"] == clone
    assert "git" in seen["cmd"]


def test_op_crash_becomes_an_observation_not_a_run_death(solve_repo):
    a, _ = solve_repo

    def boom(*args, **kwargs):
        raise ValueError("docker executor needs a cwd to mount at /work")

    a._op_search = boom
    replies = [
        json.dumps({"command": "search", "pattern": "half"}),
        json.dumps({"command": "submit"}),
    ]
    a.driver_factory = lambda: FakeDriverCrashGuard(replies)
    result = a.execute_task(task())
    assert result["status"] == "ok"
    lines = [
        json.loads(line)
        for line in open(result["data"]["transcript"]).read().splitlines()
    ]
    search_line = next(l for l in lines if isinstance(l.get("action"), dict))
    assert "internal failure" in search_line["observation"]
    assert "needs a cwd" in search_line["observation"]


class FakeDriverCrashGuard:
    def __init__(self, replies):
        self._replies = replies

    def chat(self, system, messages):
        text = self._replies.pop(0)
        return driver.ChatResult(
            text=text,
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=20,
            cost_estimate=1 / 1_000_000,
        )

    def usage_summary(self):
        return {
            "provider": "anthropic",
            "model": "fake",
            "calls": 2,
            "input_tokens": 10,
            "output_tokens": 20,
            "cost_estimate": 0.000002,
        }
