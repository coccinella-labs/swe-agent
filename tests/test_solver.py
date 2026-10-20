"""Tests for the solve loop; the driver is scripted and no HTTP happens"""

import json

import pytest

import agent
import driver
from agent import SWEAgent
from conftest import FakeResult


class FakeDriver:
    """Scripted stand-in for driver.Driver."""

    def __init__(self, replies, error=None):
        self._replies = list(replies)
        self._error = error
        self.calls = []

    def chat(self, system, messages):
        self.calls.append((system, messages))
        if self._error is not None:
            raise self._error
        text = self._replies.pop(0)
        return driver.ChatResult(
            text=text,
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=20,
            cost_estimate=30 / 1_000_000,
        )


@pytest.fixture
def solve_repo(no_config, tmp_path):
    """An agent with a real git clone 'fx' holding one buggy file."""
    import subprocess as sp

    clone = tmp_path / "coccinella-labs" / "fx"
    (clone / ".git").mkdir(parents=True)
    (clone / "calc.py").write_text("def add(a, b):\n    return a - b  # known bug\n")
    for cmd in (
        ["git", "-C", str(clone), "init", "-q"],
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "add",
            "-A",
        ],
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
    ):
        sp.run(cmd, check=False, capture_output=True)
    return SWEAgent("t", tmp_base=str(tmp_path)), clone


def task(**overrides):
    base = {"repo": "fx", "operation": "solve", "instruction": "fix add"}
    base.update(overrides)
    return base


def view(path):
    return json.dumps({"command": "view", "path": path})


def edit(path, content):
    return json.dumps({"command": "edit", "path": path, "content": content})


def test_solve_scripts_view_edit_and_submit(solve_repo):
    a, clone = solve_repo
    replies = [
        view("calc.py"),
        edit("calc.py", "def add(a, b):\n    return a + b\n"),
        json.dumps({"command": "submit"}),
    ]
    a.driver_factory = lambda: FakeDriver(replies)
    result = a.execute_task(task())
    assert result["status"] == "ok"
    data = result["data"]
    assert data["turns"] == 3
    assert data["files_changed"] == ["calc.py"]
    assert "-    return a - b  # known bug" in data["diff"]
    assert "+    return a + b" in data["diff"]
    assert "a - b" not in (clone / "calc.py").read_text()


def test_solve_writes_jsonl_transcript(solve_repo, tmp_path):
    a, _ = solve_repo
    replies = [view("calc.py"), json.dumps({"command": "submit"})]
    a.driver_factory = lambda: FakeDriver(replies)
    result = a.execute_task(task())
    transcript_path = tmp_path / "coccinella-labs" / "transcripts" / "fx"
    files = list(transcript_path.glob("*.jsonl"))
    assert files
    lines = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert "run" in lines[0]
    assert lines[0]["run"]["repo"] == "coccinella-labs/fx"
    assert [line["action"]["command"] for line in lines[1:]] == ["view", "submit"]
    assert all("usage" in line for line in lines[1:])
    assert result["data"]["transcript"] == str(files[0])


def test_solve_stops_at_turn_cap(solve_repo, tmp_path):
    a, _ = solve_repo
    searches = [json.dumps({"command": "search", "pattern": "add"})] * 3
    a.driver_factory = lambda: FakeDriver(searches)
    result = a.execute_task(task(max_turns=2))
    assert result["status"] == "error"
    assert "turn cap reached (2)" in result["error"]
    transcript_path = tmp_path / "coccinella-labs" / "transcripts" / "fx"
    files = list(transcript_path.glob("*.jsonl"))
    lines = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(lines) == 1 + 2  # header plus both turns


def test_solve_observes_unparseable_reply_and_continues(solve_repo):
    a, _ = solve_repo
    replies = ["this is not json at all", json.dumps({"command": "submit"})]
    fake = FakeDriver(replies)
    a.driver_factory = lambda: fake
    result = a.execute_task(task())
    assert result["status"] == "ok"
    last_call = fake.calls[-1][1]
    resend = last_call[-1]["content"]
    assert "exactly one JSON object" in resend
    assert last_call[-2]["content"] == "this is not json at all"


def test_solve_blocks_unknown_commands(solve_repo):
    a, _ = solve_repo
    replies = [
        json.dumps({"command": "bash", "cmd": "rm -rf /"}),
        json.dumps({"command": "submit"}),
    ]
    fake = FakeDriver(replies)
    a.driver_factory = lambda: fake
    result = a.execute_task(task())
    assert result["status"] == "ok"
    resend = fake.calls[-1][1][-1]["content"]
    assert "unknown command 'bash'" in resend


def test_solve_reports_clone_failure(no_config, tmp_path, monkeypatch):
    a = SWEAgent("t", tmp_base=str(tmp_path))
    monkeypatch.setattr(a, "clone_repo", lambda *args, **kwargs: False)
    a.driver_factory = lambda: FakeDriver([json.dumps({"command": "submit"})])
    result = a.execute_task(task())
    assert result["status"] == "error"
    assert "clone failed" in result["error"]


def test_solve_requires_instruction_or_issue(solve_repo):
    a, _ = solve_repo
    a.driver_factory = lambda: pytest.fail("driver must never be built")
    result = a.execute_task({"repo": "fx", "operation": "solve"})
    assert result["status"] == "error"
    assert "instruction" in result["error"] and "issue" in result["error"]


def test_solve_surfaces_cost_cap(solve_repo):
    a, _ = solve_repo
    a.driver_factory = lambda: FakeDriver(
        [], error=driver.CapReached("cost cap of 2 USD exceeded")
    )
    result = a.execute_task(task())
    assert result["status"] == "error"
    assert "cost cap" in result["error"]


def test_solve_surfaces_driver_errors(solve_repo):
    a, _ = solve_repo
    a.driver_factory = lambda: FakeDriver(
        [], error=driver.DriverError("model call returned HTTP 500")
    )
    result = a.execute_task(task())
    assert result["status"] == "error"
    assert "HTTP 500" in result["error"]


def test_solve_validates_turn_cap(solve_repo):
    a, _ = solve_repo
    a.driver_factory = lambda: FakeDriver([json.dumps({"command": "submit"})])
    result = a.execute_task(task(max_turns=99))
    assert result["status"] == "error"
    assert "1 to 50" in result["error"]


def test_solve_fetches_issue_via_gh(solve_repo, monkeypatch):
    a, _ = solve_repo
    recorded = []

    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        recorded.append(cmd)
        if cmd[0] == "gh":
            return FakeResult(
                0,
                stdout=json.dumps(
                    {
                        "title": "add() subtracts instead",
                        "body": "please fix",
                        "state": "OPEN",
                        "url": "https://github.com/coccinella-labs/fx/issues/5",
                    }
                ),
            )
        if "ls-files" in cmd:
            return FakeResult(0, stdout="calc.py\n")
        if "diff" in cmd:
            return FakeResult(0, stdout="diff --git a/calc.py\n")
        return FakeResult(0, stdout="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    fake = FakeDriver([json.dumps({"command": "submit"})])
    a.driver_factory = lambda: fake
    result = a.execute_task(
        {"repo": "fx", "operation": "solve", "issue": 5, "instruction": None}
    )
    assert result["status"] == "ok"
    gh_cmd = next(cmd for cmd in recorded if cmd[0] == "gh")
    assert gh_cmd[1:3] == ["issue", "view"]
    assert gh_cmd[3] == "5"
    assert "coccinella-labs/fx" in gh_cmd
    brief = fake.calls[0][1][0]["content"]
    assert "add() subtracts instead" in brief
    assert "please fix" in brief
    assert "[OPEN]" in brief


def test_solve_accepts_issue_url(solve_repo, monkeypatch):
    a, _ = solve_repo

    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        if cmd[0] == "gh":
            return FakeResult(
                0,
                stdout=json.dumps(
                    {"title": "t", "body": "b", "state": "OPEN", "url": "u"}
                ),
            )
        if "ls-files" in cmd:
            return FakeResult(0, stdout="calc.py\n")
        if "diff" in cmd:
            return FakeResult(0, stdout="x")
        return FakeResult(0, stdout="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    fake = FakeDriver([json.dumps({"command": "submit"})])
    a.driver_factory = lambda: fake
    result = a.execute_task(
        {
            "repo": "fx",
            "operation": "solve",
            "issue": "https://github.com/coccinella-labs/fx/issues/7",
        }
    )
    assert result["status"] == "ok"


def test_solve_rejects_malformed_issue(solve_repo, monkeypatch):
    a, _ = solve_repo
    monkeypatch.setattr(
        agent.subprocess,
        "run",
        lambda cmd, cwd=None, timeout=300, **kwargs: FakeResult(0, stdout=""),
    )
    a.driver_factory = lambda: FakeDriver([json.dumps({"command": "submit"})])
    result = a.execute_task(
        {"repo": "fx", "operation": "solve", "issue": "not-a-url-or-number"}
    )
    assert result["status"] == "error"
    assert "issue" in result["error"]


def test_solve_notes_event_callback(solve_repo):
    a, _ = solve_repo
    events = []
    a.on_event = lambda turn, action, observation: events.append(turn)
    a.driver_factory = lambda: FakeDriver(
        [view("calc.py"), json.dumps({"command": "submit"})]
    )
    result = a.execute_task(task())
    assert result["status"] == "ok"
    assert events == [1, 2]


def _transcript_lines(tmp_path):
    files = list((tmp_path / "coccinella-labs" / "transcripts" / "fx").glob("*.jsonl"))
    assert files
    return [json.loads(line) for line in files[0].read_text().splitlines()]


_submit = [json.dumps({"command": "submit"})]


def test_solve_clamps_turns_to_env_ceiling(solve_repo, tmp_path, monkeypatch):
    a, _ = solve_repo
    monkeypatch.setenv("SWE_AGENT_MAX_TURNS", "15")
    a.driver_factory = lambda: FakeDriver(_submit)
    result = a.execute_task(task(max_turns=40))
    assert result["status"] == "ok"
    lines = _transcript_lines(tmp_path)
    assert lines[0]["run"]["max_turns"] == 15
    assert lines[0]["run"]["clamped"] == {"max_turns": 40}


def test_solve_clamps_cost_cap_to_env_ceiling(solve_repo, tmp_path, monkeypatch):
    a, _ = solve_repo
    monkeypatch.setenv("SWE_AGENT_COST_CAP_USD", "1.5")
    a.driver_factory = lambda: FakeDriver(_submit)
    result = a.execute_task(task(cost_cap_usd=5.0))
    assert result["status"] == "ok"
    lines = _transcript_lines(tmp_path)
    assert lines[0]["run"]["clamped"] == {"cost_cap_usd": 5.0}


def test_solve_hard_ceiling_still_errors_above_fifty(solve_repo, monkeypatch):
    a, _ = solve_repo
    monkeypatch.setenv("SWE_AGENT_MAX_TURNS", "15")
    a.driver_factory = lambda: FakeDriver(_submit)
    result = a.execute_task(task(max_turns=99))
    assert result["status"] == "error"
    assert "1 to 50" in result["error"]


def test_solve_invalid_env_ceiling_means_absent(solve_repo, tmp_path, monkeypatch):
    a, _ = solve_repo
    monkeypatch.setenv("SWE_AGENT_MAX_TURNS", "fifteen")
    monkeypatch.setenv("SWE_AGENT_COST_CAP_USD", "bogus")
    a.driver_factory = lambda: FakeDriver(_submit)
    result = a.execute_task(task(max_turns=40, cost_cap_usd=5.0))
    assert result["status"] == "ok"
    lines = _transcript_lines(tmp_path)
    # invalid ceilings fall back to defaults: turns stay 40 (under 50);
    # the default 2 USD ceiling still clamps a 5 USD task input
    assert lines[0]["run"]["max_turns"] == 40
    assert lines[0]["run"]["clamped"] == {"cost_cap_usd": 5.0}


def test_solve_without_env_has_no_clamped_record(solve_repo, tmp_path, monkeypatch):
    a, _ = solve_repo
    monkeypatch.delenv("SWE_AGENT_MAX_TURNS", raising=False)
    monkeypatch.delenv("SWE_AGENT_COST_CAP_USD", raising=False)
    a.driver_factory = lambda: FakeDriver(_submit)
    result = a.execute_task(task(max_turns=40, cost_cap_usd=1.5))
    assert result["status"] == "ok"
    lines = _transcript_lines(tmp_path)
    assert "clamped" not in lines[0]["run"]
