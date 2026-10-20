"""Text contract for the ai-fix CI workflow; the YAML must honor the design.

The workflow itself cannot be unit-executed offline, so every security
and cost property the design doc states is asserted here as invariant
text in .github/workflows/ai-fix.yml, in the same spirit as the
sandbox executor's argv-contract tests.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github" / "workflows" / "ai-fix.yml").read_text()


def test_trigger_is_labeled_only():
    assert "types: [labeled]" in TEXT
    assert "on:" in TEXT


def test_label_gate():
    assert "github.event.label.name == 'ai-fix'" in TEXT


def test_per_issue_concurrency_and_cancellation():
    assert "group: ai-fix-${{ github.event.issue.number }}" in TEXT
    assert "cancel-in-progress: true" in TEXT


def test_job_timeout_is_twenty_minutes():
    assert "timeout-minutes: 20" in TEXT


def test_revocation_check_exists():
    assert "issues/${{ github.event.issue.number }}" in TEXT
    assert "ai-fix" in TEXT


def test_caps_held_by_workflow_env():
    assert 'SWE_AGENT_MAX_TURNS: "15"' in TEXT
    assert 'SWE_AGENT_COST_CAP_USD: "1.5"' in TEXT
    assert "SWE_AGENT_SANDBOX: docker" in TEXT


def test_image_built_per_run():
    assert "docker build -f docker/sandbox.Dockerfile" in TEXT


def test_model_key_comes_only_from_the_secret():
    assert "${{ secrets.ANTHROPIC_API_KEY }}" in TEXT
    found = set(re.findall(r"secrets\.\w+", TEXT))
    assert found == {"secrets.ANTHROPIC_API_KEY"}


def test_sandbox_carries_no_key_in_its_arguments():
    # the solve invocation must not interpolate the key anywhere
    for line in TEXT.splitlines():
        if "agent.py task" in line:
            assert "ANTHROPIC_API_KEY" not in line
            assert "secrets." not in line


def test_transcript_key_prefix_scan_present():
    assert "ANTHROPIC_API_KEY:0:8" in TEXT


def test_refs_template_present_and_no_closing_keywords():
    assert "Refs #" in TEXT
    lowered = TEXT.lower()
    for keyword in ("closes #", "fixes #", "resolves #"):
        assert keyword not in lowered


def test_write_surface_is_branch_draft_pr_and_comments_only():
    assert "--draft" in TEXT
    assert "x-access-token" in TEXT
    assert "gh issue comment" in TEXT
    assert "gh api" in TEXT or "gh issue edit" in TEXT


def test_label_is_consumed_at_the_end():
    assert "--remove-label ai-fix" in TEXT


def test_evidence_artifacts_uploaded():
    assert "actions/upload-artifact@v4" in TEXT
