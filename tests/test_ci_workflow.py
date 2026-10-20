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


def test_solve_step_exports_gh_token_for_the_issue_fetch():
    # _fetch_issue_brief shells out to gh issue view; without a token
    # export the runner's gh call fails and the solve dies before any
    # model call (recorded in runs 39356118027 and 39357533789 on
    # issue #2, 2026-10-20: the second after PR #14 regressed the
    # export, caught only because the text contract asserted the
    # string repo-wide while other steps still carried it.
    # Scope the assertion to the solve step so that cannot recur.)
    solve = TEXT.split("- name: Solve inside the sandbox", 1)[1]
    solve = solve.split("- name: Collect the diff", 1)[0]
    assert "GH_TOKEN: ${{ github.token }}" in solve


def test_image_built_per_run():
    assert "docker build -f docker/sandbox.Dockerfile" in TEXT


def test_model_key_comes_only_from_the_secret():
    assert "${{ secrets.ANTHROPIC_API_KEY }}" in TEXT
    found = set(re.findall(r"secrets\.\w+", TEXT))
    assert found == {
        "secrets.ANTHROPIC_API_KEY",
        "secrets.GEMINI_API_KEY",
        "secrets.OPENROUTER_API_KEY",
    }


def test_openrouter_interim_posture_selected_with_its_key():
    # Interim posture, second leg 2026-10-20: gemini-3.8-flash stalled
    # the label runs twice on vendor-side transient errors (run 4 HTTP
    # 503, run 5 read timeout), so the solve moved to openrouter/auto
    # per design section 9.3 question 2; reverts to the anthropic
    # default when console credits land.
    assert "SWE_AGENT_PROVIDER: openrouter" in TEXT
    assert "SWE_AGENT_MODEL: openrouter/auto" in TEXT
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in TEXT
    solve = TEXT[TEXT.index("Solve inside the sandbox") :]
    solve = solve[: solve.index("- name: Collect the diff")]
    assert "GH_TOKEN: ${{ github.token }}" in solve
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        assert key + ": ${{ secrets." + key + " }}" in solve


def test_sandbox_carries_no_key_in_its_arguments():
    # the solve invocation must not interpolate the key anywhere
    for line in TEXT.splitlines():
        if "agent.py task" in line:
            assert "ANTHROPIC_API_KEY" not in line
            assert "secrets." not in line


def test_transcript_key_prefix_scan_present():
    # the scan loops over both provider keys with the same prefix rule
    assert "for name in ANTHROPIC_API_KEY GEMINI_API_KEY OPENROUTER_API_KEY" in TEXT
    assert "${!name:0:8}" in TEXT


def test_refs_template_present_and_no_closing_keywords():
    assert "Refs #" in TEXT
    lowered = TEXT.lower()
    for keyword in ("closes #", "fixes #", "resolves #"):
        assert keyword not in lowered


def test_post_steps_run_even_when_the_job_fails():
    # run 39360267166 (issue #2, 2026-10-20): the job failed inside the
    # solve step, the failure report and label consumption were skipped,
    # and the label stayed attached; their gates must carry always()
    assert "always() && steps.outcome.outputs.status" in TEXT
    assert "always() && steps.gate.outputs.aborted" in TEXT


def test_write_surface_is_branch_draft_pr_and_comments_only():
    assert "--draft" in TEXT
    assert "x-access-token" in TEXT
    assert "gh issue comment" in TEXT
    assert "gh api" in TEXT or "gh issue edit" in TEXT


def test_label_is_consumed_at_the_end():
    assert "--remove-label ai-fix" in TEXT


def test_evidence_artifacts_uploaded():
    assert "actions/upload-artifact@v4" in TEXT
