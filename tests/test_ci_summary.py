"""Unit tests for the ai-fix run-summary builder (scripts/ai-fix_summary.py)."""

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ai-fix_summary.py"
_SPEC = importlib.util.spec_from_file_location("ai_fix_summary", _SCRIPT)
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)
build_report = _mod.build_report


def _evidence(
    tmp_path, turns_done="2", cost="0.000012", test_obs="pass", lint_obs="clean"
):
    transcript = tmp_path / "transcripts" / "swe-agent" / "run.jsonl"
    transcript.parent.mkdir(parents=True)
    entries = [
        {
            "turn": 1,
            "action": {"command": "view", "path": "calc.py"},
            "observation": "ok",
        },
        {"turn": 2, "action": {"command": "test"}, "observation": test_obs},
        {"turn": 3, "action": {"command": "lint"}, "observation": lint_obs},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    result = tmp_path / "result.json"
    result.write_text(
        json.dumps(
            {
                "status": "ok",
                "data": {
                    "turns": turns_done,
                    "transcript": str(transcript),
                    "usage": {"cost_estimate": cost},
                },
            }
        )
    )
    return result


def test_report_carries_turns_cost_and_observations(tmp_path):
    result = _evidence(tmp_path, test_obs="3 passed in 0.1s", lint_obs="clean")
    report = build_report(result)
    assert "turns used: 2" in report
    assert "estimated cost: 0.000012 USD" in report
    assert "3 passed in 0.1s" in report
    assert "clean" in report


def test_report_without_transcript_says_not_observed(tmp_path):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"status": "ok", "data": {"turns": 1}}))
    report = build_report(result)
    assert "turns used: 1" in report
    assert "not observed" in report


def test_report_stays_plain_and_reference_only(tmp_path):
    report = build_report(_evidence(tmp_path))
    lowered = report.lower()
    for keyword in ("closes #", "fixes #", "resolves #"):
        assert keyword not in lowered


def test_report_truncates_long_observations(tmp_path):
    result = _evidence(tmp_path, test_obs="x" * 1000)
    report = build_report(result)
    test_section = report.split("final test observation:", 1)[1]
    assert "x" * 1000 not in test_section.split("final lint observation:")[0]


@pytest.mark.parametrize("command", ["test", "lint"])
def test_last_matching_wins(tmp_path, command):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(e)
            for e in (
                {"turn": 1, "action": {"command": command}, "observation": "first"},
                {"turn": 2, "action": {"command": command}, "observation": "later"},
            )
        )
        + "\n"
    )
    result = tmp_path / "result.json"
    result.write_text(
        json.dumps({"status": "ok", "data": {"transcript": str(transcript)}})
    )
    report = build_report(result)
    assert "later" in report
