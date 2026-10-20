"""Build the ai-fix issue-comment summary from the run's evidence artifacts.

Usage: ai-fix_summary.py RESULT_JSON OUT_MD

Reads the task result JSON produced by `agent.py task`, re-reads the
JSONL transcript it points to, and writes a short markdown summary of
turns, estimated cost, and the final test and lint observations.
Cost figures are rough estimates for the cap, never billing.
"""

import json
import sys
from pathlib import Path


def _last_observation(lines: list, command: str) -> str:
    for line in reversed(lines):
        action = line.get("action")
        if isinstance(action, dict) and action.get("command") == command:
            return str(line.get("observation", "not observed"))[:300]
    return "not observed"


def build_report(result_path: Path) -> str:
    result = json.loads(result_path.read_text())
    data = result.get("data") or {}
    lines = []
    transcript = data.get("transcript")
    if transcript and Path(transcript).exists():
        lines = [
            json.loads(line)
            for line in Path(transcript).read_text().splitlines()
            if line.strip()
        ]
    usage = data.get("usage") or {}
    cost = usage.get("cost_estimate")
    report = [
        "The sandboxed ai-fix run finished; evidence is on the workflow run.",
        "",
        f"turns used: {data.get('turns')}",
    ]
    if cost is not None:
        report.append(
            f"estimated cost: {cost} USD (rough estimate for the cap, not billing)"
        )
    report += [
        "",
        "final test observation:",
        _last_observation(lines, "test"),
        "",
        "final lint observation:",
        _last_observation(lines, "lint"),
        "",
        (
            "Human review happens on the PR; this automation cannot "
            "approve, merge, or close anything."
        ),
    ]
    return "\n".join(report) + "\n"


def main(argv: list) -> int:
    if len(argv) != 3:
        print("usage: ai-fix_summary.py RESULT_JSON OUT_MD", file=sys.stderr)
        return 2
    Path(argv[2]).write_text(build_report(Path(argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
