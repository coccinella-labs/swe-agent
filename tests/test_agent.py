"""Tests for the SWE agent's repo and skill handling"""

import json
import subprocess
from pathlib import Path

import pytest

import agent
from agent import FALLBACK_REPOS, SWEAgent
from conftest import FakeResult, runner

# --- repo loading ---------------------------------------------------------


def test_fallback_when_config_is_unreachable(no_config):
    assert SWEAgent("t").repos == FALLBACK_REPOS


def test_uses_payload_when_config_replies(usable_payload):
    assert SWEAgent("t").repos == ["skills", "swe-agent"]


# --- fetch behaviour ------------------------------------------------------


def test_fetch_accepts_well_shaped_payload(monkeypatch):
    runner(0, json.dumps({"repos": ["skills", "swe-agent"]}), monkeypatch)
    assert SWEAgent._fetch_repo_list() == ["skills", "swe-agent"]


def test_fetch_rejects_non_json(monkeypatch):
    runner(0, "<!doctype html>", monkeypatch)
    assert SWEAgent._fetch_repo_list() is None


def test_fetch_rejects_http_error(monkeypatch):
    runner(22, "", monkeypatch)
    assert SWEAgent._fetch_repo_list() is None


def test_fetch_rejects_payload_without_repos(monkeypatch):
    runner(0, json.dumps({"org": "coccinella-labs"}), monkeypatch)
    assert SWEAgent._fetch_repo_list() is None


def test_fetch_rejects_non_string_repo_names(monkeypatch):
    runner(0, json.dumps({"repos": ["skills", 1]}), monkeypatch)
    assert SWEAgent._fetch_repo_list() is None


def test_fetch_survives_missing_curl(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    assert SWEAgent._fetch_repo_list() is None


def test_fetch_survives_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd[0], timeout=10)

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    assert SWEAgent._fetch_repo_list() is None


# --- paths and cloning ----------------------------------------------------


def test_repo_paths_and_listing(no_config, tmp_path):
    a = SWEAgent("t", tmp_base=str(tmp_path))
    (tmp_path / "coccinella-labs" / "skills").mkdir(parents=True)
    assert a.get_repo_path("skills") == tmp_path / "coccinella-labs" / "skills"
    assert a.get_repo_path("missing") is None
    assert a.is_repo_cloned("skills") is True
    assert a.is_repo_cloned("missing") is False
    assert a.list_cloned_repos() == ["skills"]


def test_clone_skips_existing_repo_without_force(no_config, tmp_path, monkeypatch):
    (tmp_path / "coccinella-labs" / "swe-agent").mkdir(parents=True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult(0)

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    a = SWEAgent("t", tmp_base=str(tmp_path))
    assert a.clone_repo("swe-agent") is True
    assert calls == []


def test_clone_runs_when_forced(no_config, tmp_path, monkeypatch):
    (tmp_path / "coccinella-labs" / "swe-agent").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        Path(cmd[2]).mkdir(parents=True, exist_ok=True)
        return FakeResult(0)

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    a = SWEAgent("t", tmp_base=str(tmp_path))
    assert a.clone_repo("swe-agent", force=True) is True
    assert a.last_error is None


def test_clone_reports_git_failure(no_config, tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        return FakeResult(128, stderr=b"fatal: repository 'nope' not found\n")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    a = SWEAgent("t", tmp_base=str(tmp_path))
    assert a.clone_repo("nope") is False
    assert a.last_error is not None
    assert a.last_error.startswith("git clone failed")
    assert "fatal: repository 'nope' not found" in a.last_error


def test_clone_reports_missing_git(no_config, tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    a = SWEAgent("t", tmp_base=str(tmp_path))
    assert a.clone_repo("skills") is False
    assert "could not run" in a.last_error


# --- skills ---------------------------------------------------------------


def test_skills_loaded_from_cloned_skills_repo(no_config, tmp_path):
    base = tmp_path / "coccinella-labs" / "skills" / "agent"
    base.mkdir(parents=True)
    (base / "developer_tools.md").write_text("dev skill text")
    (base / "personas.md").write_text("persona text")
    a = SWEAgent("t", tmp_base=str(tmp_path))
    assert a.skills["developer_tools"] == ["dev skill text"]
    assert a.skills["personas"] == ["persona text"]


def test_skills_empty_without_cloned_skills_repo(no_config, tmp_path):
    a = SWEAgent("t", tmp_base=str(tmp_path))
    assert a.skills == {"developer_tools": [], "personas": []}


# --- task surface ---------------------------------------------------------


@pytest.fixture
def cloned_repo(no_config, tmp_path):
    """An agent whose tmp_base holds a clone named 'fx'."""
    clone = tmp_path / "coccinella-labs" / "fx"
    clone.mkdir(parents=True)
    return SWEAgent("t", tmp_base=str(tmp_path))


def test_execute_task_rejects_non_dict(no_config):
    result = SWEAgent("t").execute_task("do a thing")
    assert result["status"] == "error"
    assert "dict" in result["error"]


def test_execute_task_rejects_missing_repo(cloned_repo):
    result = cloned_repo.execute_task({"operation": "read"})
    assert result["status"] == "error"
    assert "'repo'" in result["error"]


def test_execute_task_rejects_unknown_operation(cloned_repo):
    result = cloned_repo.execute_task({"repo": "fx", "operation": "deploy"})
    assert result["status"] == "error"
    assert "unknown operation" in result["error"]
    assert "read, write, test, pr" in result["error"]


def test_execute_task_rejects_uncloned_repo(usable_payload, tmp_path):
    a = SWEAgent("t", tmp_base=str(tmp_path))
    result = a.execute_task(
        {"repo": "swe-agent", "operation": "read", "path": "README.md"}
    )
    assert result["status"] == "error"
    assert "not cloned" in result["error"]


def test_read_returns_file_text(cloned_repo):
    clone = cloned_repo.get_repo_path("fx")
    (clone / "notes.md").write_text("hello")
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "read", "path": "notes.md"}
    )
    assert result["status"] == "ok"
    assert result["data"] == {"path": "notes.md", "text": "hello"}


def test_read_rejects_missing_file(cloned_repo):
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "read", "path": "absent.md"}
    )
    assert result["status"] == "error"
    assert "does not exist" in result["error"]


def test_read_rejects_escape(cloned_repo):
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "read", "path": "../outside.txt"}
    )
    assert result["status"] == "error"
    assert "escapes" in result["error"]


def test_write_creates_file_and_parents(cloned_repo):
    result = cloned_repo.execute_task(
        {
            "repo": "fx",
            "operation": "write",
            "path": "src/deep/file.txt",
            "content": "data",
        }
    )
    assert result["status"] == "ok"
    assert result["data"]["bytes"] == 4
    clone = cloned_repo.get_repo_path("fx")
    assert (clone / "src" / "deep" / "file.txt").read_text() == "data"


def test_write_rejects_non_string_content(cloned_repo):
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "write", "path": "x.txt", "content": 7}
    )
    assert result["status"] == "error"
    assert "string 'content'" in result["error"]


def test_write_rejects_directory_target(cloned_repo):
    clone = cloned_repo.get_repo_path("fx")
    (clone / "adir").mkdir()
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "write", "path": "adir", "content": "x"}
    )
    assert result["status"] == "error"
    assert "directory" in result["error"]


def test_test_reports_no_command(cloned_repo):
    result = cloned_repo.execute_task({"repo": "fx", "operation": "test"})
    assert result["status"] == "error"
    assert "no recognisable test command" in result["error"]


def test_test_runs_pytest_when_tests_dir_exists(cloned_repo, monkeypatch):
    clone = cloned_repo.get_repo_path("fx")
    (clone / "tests").mkdir()
    seen = []

    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        seen.append((cmd, cwd))
        return FakeResult(0, stdout="1 passed")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    result = cloned_repo.execute_task({"repo": "fx", "operation": "test"})
    assert result["status"] == "ok"
    assert result["data"]["output_tail"] == "1 passed"
    cmd, cwd = seen[0]
    assert cmd[1:3] == ["-m", "pytest"]
    assert cwd == str(clone)


def test_test_fails_when_tests_fail(cloned_repo, monkeypatch):
    clone = cloned_repo.get_repo_path("fx")
    (clone / "tests").mkdir()

    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        return FakeResult(1, stdout="1 failed")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    result = cloned_repo.execute_task({"repo": "fx", "operation": "test"})
    assert result["status"] == "error"
    assert "tests failed (exit 1)" in result["error"]


def test_test_detects_makefile_target(cloned_repo, monkeypatch):
    clone = cloned_repo.get_repo_path("fx")
    (clone / "Makefile").write_text("all:\n\techo hi\n\ntest:\n\tpytest\n")
    seen = []

    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        seen.append(cmd)
        return FakeResult(0, stdout="ok")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    result = cloned_repo.execute_task({"repo": "fx", "operation": "test"})
    assert result["status"] == "ok"
    assert seen[0] == ["make", "test"]


def _pr_sequence(stdout_by_stage):
    """Build a fake_run that answers the pr steps in order."""

    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        if "status" in cmd:
            return FakeResult(0, stdout=stdout_by_stage.pop("status"))
        if "add" in cmd:
            return FakeResult(0)
        if "commit" in cmd:
            return FakeResult(0)
        if "push" in cmd:
            return FakeResult(0)
        if cmd[0] == "gh":
            return FakeResult(0, stdout=stdout_by_stage.pop("gh"))
        return FakeResult(1, stderr=b"unexpected call")

    return fake_run


def test_pr_refuses_without_confirm(cloned_repo):
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "pr", "branch": "b", "message": "m"}
    )
    assert result["status"] == "error"
    assert "confirm" in result["error"]


def test_pr_refuses_clean_tree(cloned_repo, monkeypatch):
    monkeypatch.setattr(agent.subprocess, "run", _pr_sequence({"status": ""}))
    result = cloned_repo.execute_task(
        {
            "repo": "fx",
            "operation": "pr",
            "branch": "b",
            "message": "m",
            "confirm": True,
        }
    )
    assert result["status"] == "error"
    assert "working tree is clean" in result["error"]


def test_pr_commits_pushes_and_opens_pr(cloned_repo, monkeypatch):
    monkeypatch.setattr(
        agent.subprocess,
        "run",
        _pr_sequence(
            {
                "status": " M README.md\n",
                "gh": "https://github.com/coccinella-labs/fx/pull/9\n",
            }
        ),
    )
    result = cloned_repo.execute_task(
        {
            "repo": "fx",
            "operation": "pr",
            "branch": "swe-agent/x",
            "message": "fix: thing",
            "confirm": True,
        }
    )
    assert result["status"] == "ok"
    assert result["data"] == {
        "branch": "swe-agent/x",
        "url": "https://github.com/coccinella-labs/fx/pull/9",
    }


def test_pr_reports_step_failure(cloned_repo, monkeypatch):
    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        if "status" in cmd:
            return FakeResult(0, stdout=" M f\n")
        if "add" in cmd:
            return FakeResult(0)
        if "commit" in cmd:
            return FakeResult(1, stderr="nothing added to commit\n")
        return FakeResult(1, stderr=b"unexpected call")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    result = cloned_repo.execute_task(
        {
            "repo": "fx",
            "operation": "pr",
            "branch": "b",
            "message": "m",
            "confirm": True,
        }
    )
    assert result["status"] == "error"
    assert result["error"] == "commit failed: nothing added to commit"


def test_can_handle_accepts_well_shaped_tasks(cloned_repo):
    assert cloned_repo.can_handle({"repo": "fx", "operation": "read"}) is True
    assert cloned_repo.can_handle("do a thing") is False
    assert cloned_repo.can_handle({"operation": "read"}) is False
    assert cloned_repo.can_handle({"repo": "fx", "operation": "deploy"}) is False


# --- search ---------------------------------------------------------------


def test_search_returns_matches(cloned_repo, monkeypatch):
    seen = []

    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        seen.append(cmd)
        return FakeResult(0, stdout="calc.py:1:def add(a, b):\n")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "search", "pattern": "def add"}
    )
    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    assert result["data"]["matches"] == ["calc.py:1:def add(a, b):"]
    assert "grep" in seen[0] and "def add" in seen[0]


def test_search_no_matches_is_ok_not_error(cloned_repo, monkeypatch):
    monkeypatch.setattr(
        agent.subprocess,
        "run",
        lambda cmd, cwd=None, timeout=300, **kwargs: FakeResult(1, stdout=""),
    )
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "search", "pattern": "zzz"}
    )
    assert result["status"] == "ok"
    assert result["data"]["count"] == 0


def test_search_requires_pattern(cloned_repo):
    result = cloned_repo.execute_task({"repo": "fx", "operation": "search"})
    assert result["status"] == "error"
    assert "'pattern'" in result["error"]


def test_search_reports_git_failure(cloned_repo, monkeypatch):
    monkeypatch.setattr(
        agent.subprocess,
        "run",
        lambda cmd, cwd=None, timeout=300, **kwargs: FakeResult(
            128, stderr="fatal: no git index available\n"
        ),
    )
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "search", "pattern": "x"}
    )
    assert result["status"] == "error"
    assert "git grep failed" in result["error"]


def test_capabilities_shape(no_config, tmp_path):
    a = SWEAgent("agent-test", tmp_base=str(tmp_path))
    info = a.get_capabilities()
    assert info["name"] == "agent-test"
    assert info["repos"] == FALLBACK_REPOS
    assert info["cloned_repos"] == []
    assert set(info["skills"]) == {"developer_tools", "personas"}
    assert info["tmp_base"] == str(tmp_path)


# --- diff operation --------------------------------------------------------


def test_diff_reports_working_tree_changes(cloned_repo):
    clone = cloned_repo.get_repo_path("fx")
    subprocess.run(["git", "init", "-q"], cwd=clone, check=False, capture_output=True)
    (clone / "tracked.txt").write_text("one\n")
    subprocess.run(
        ["git", "add", "tracked.txt"], cwd=clone, check=False, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@x",
            "commit",
            "-qm",
            "seed",
        ],
        cwd=clone,
        check=False,
        capture_output=True,
    )
    (clone / "tracked.txt").write_text("two\n")
    result = cloned_repo.execute_task({"repo": "fx", "operation": "diff"})
    assert result["status"] == "ok"
    assert "M tracked.txt" in result["data"]["text"]
    assert result["data"]["truncated"] is False


def test_diff_errors_outside_a_git_repo(cloned_repo):
    result = cloned_repo.execute_task({"repo": "fx", "operation": "diff"})
    assert result["status"] == "error"
    assert "exited 128" in result["error"]


# --- lint operation --------------------------------------------------------


def test_lint_reports_success(cloned_repo, monkeypatch):
    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        assert cmd == ["ruff", "check", "."]
        assert cwd is not None
        return FakeResult(0, stdout="All checks passed!")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    result = cloned_repo.execute_task({"repo": "fx", "operation": "lint"})
    assert result["status"] == "ok"
    assert result["data"]["output_tail"] == "All checks passed!"


def test_lint_reports_violations(cloned_repo, monkeypatch):
    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        return FakeResult(1, stdout="x.py:1:1: F401 import unused")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    result = cloned_repo.execute_task({"repo": "fx", "operation": "lint"})
    assert result["status"] == "error"
    assert "lint failed (exit 1)" in result["error"]


def test_lint_survives_missing_ruff(cloned_repo, monkeypatch):
    def boom(cmd, cwd=None, timeout=300, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(agent.subprocess, "run", boom)
    result = cloned_repo.execute_task({"repo": "fx", "operation": "lint"})
    assert result["status"] == "error"
    assert "ruff is not installed" in result["error"]


# --- multi-file view -------------------------------------------------------


def test_read_multi_file_paths(cloned_repo):
    clone = cloned_repo.get_repo_path("fx")
    (clone / "a.txt").write_text("alpha")
    (clone / "b.txt").write_text("beta")
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "read", "paths": ["a.txt", "b.txt"]}
    )
    assert result["status"] == "ok"
    assert result["data"]["files"] == [
        {"path": "a.txt", "text": "alpha"},
        {"path": "b.txt", "text": "beta"},
    ]


def test_read_paths_rejects_more_than_eight(cloned_repo):
    paths = [f"p{i}.txt" for i in range(9)]
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "read", "paths": paths}
    )
    assert result["status"] == "error"
    assert "more than 8" in result["error"]


def test_read_paths_rejects_non_string_entries(cloned_repo):
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "read", "paths": [1, 2]}
    )
    assert result["status"] == "error"


def test_read_paths_rejects_escape(cloned_repo):
    clone = cloned_repo.get_repo_path("fx")
    (clone / "ok.txt").write_text("fine")
    result = cloned_repo.execute_task(
        {"repo": "fx", "operation": "read", "paths": ["ok.txt", "../out"]}
    )
    assert result["status"] == "error"
    assert "escapes" in result["error"]


def test_observe_flattens_multi_file_view(cloned_repo):
    clone = cloned_repo.get_repo_path("fx")
    (clone / "a.txt").write_text("alpha")
    (clone / "b.txt").write_text("beta")
    text = cloned_repo._observe(
        clone, {"repo": "fx"}, {"command": "view", "paths": ["a.txt", "b.txt"]}
    )
    assert "alpha" in text and "beta" in text


# --- solve surface ---------------------------------------------------------


def test_solve_command_table_includes_diff_and_lint():
    assert "diff" in agent.SOLVE_COMMANDS
    assert "lint" in agent.SOLVE_COMMANDS


def test_solve_prompt_documents_new_commands():
    assert "diff" in agent.SOLVE_SYSTEM
    assert "lint" in agent.SOLVE_SYSTEM
    assert "paths" in agent.SOLVE_SYSTEM


def test_parse_action_unknown_command_mentions_new_verbs(no_config, tmp_path):
    agent_t = SWEAgent("t", tmp_base=str(tmp_path))
    action, err = agent_t._parse_action('{"command": "burn"}')
    assert action is None
    assert "diff" in err and "lint" in err


def test_observe_routes_diff_command(cloned_repo, monkeypatch):
    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        if cmd[:2] == ["git", "status"]:
            return FakeResult(0, stdout="M tracked.txt")
        return FakeResult(0, stdout="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    text = cloned_repo._observe(
        cloned_repo.get_repo_path("fx"), {"repo": "fx"}, {"command": "diff"}
    )
    assert "M tracked.txt" in text


def test_observe_routes_lint_command(cloned_repo, monkeypatch):
    def fake_run(cmd, cwd=None, timeout=300, **kwargs):
        return FakeResult(0, stdout="All checks passed!")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    text = cloned_repo._observe(
        cloned_repo.get_repo_path("fx"), {"repo": "fx"}, {"command": "lint"}
    )
    assert text.startswith("lint passed:")
