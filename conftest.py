# Present so pytest puts the repository root on sys.path for `import agent`.
"""Shared fixtures and stubs for the test suite; everything stays offline."""

import pytest


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def no_config(monkeypatch):
    """Construction without touching the network; the agent then falls back."""
    import agent

    monkeypatch.setattr(agent.SWEAgent, "_fetch_repo_list", staticmethod(lambda: None))


@pytest.fixture
def usable_payload(monkeypatch):
    """Construction with a small fetched repo list."""
    import agent

    monkeypatch.setattr(
        agent.SWEAgent,
        "_fetch_repo_list",
        staticmethod(lambda: ["skills", "swe-agent"]),
    )


def runner(returncode, payload, monkeypatch):
    """Stub subprocess.run to answer curl-style calls with canned output."""
    import agent

    def fake_run(cmd, **kwargs):
        return FakeResult(returncode, payload)

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
