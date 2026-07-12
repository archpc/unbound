"""Stable JSON and Markdown report rendering."""

from __future__ import annotations

import json
from pathlib import Path


def render_markdown(report):
    summary = report["summary"]
    lines = [
        "# Read-only agent evaluation",
        "",
        "- Mode: `{}`".format(summary["mode"]),
        "- Model: `{}`".format(summary["model_name"]),
        "- Sampling: temperature={}, top_p={}, max_tokens={}".format(
            summary["sampling"]["temperature"], summary["sampling"]["top_p"],
            summary["sampling"]["max_tokens"],
        ),
        "- Decision: **{}**".format(summary["decision"].upper()),
        "- Scenarios: {} ({} passed)".format(summary["scenario_count"], summary["passed_scenarios"]),
        "- Average score: {:.2f}".format(summary["average_score"]),
        "",
        "## Acceptance gates",
        "",
        "| Gate | Result |",
        "|---|---|",
    ]
    for name, passed in summary["gates"].items():
        lines.append("| `{}` | {} |".format(name, "PASS" if passed else "FAIL"))
    lines.extend((
        "", "## Scenario results", "",
        "| Scenario | Category | Score | Result | Tools |",
        "|---|---|---:|---|---|",
    ))
    for result in report["results"]:
        tools = ", ".join(call["name"] for call in result["tool_calls"]) or "none"
        lines.append("| `{}` | {} | {} | {} | {} |".format(
            result["id"], result["category"], result["score"],
            "PASS" if result["passed"] else "FAIL", tools,
        ))
    lines.extend(("", "## Failures", ""))
    failures = [result for result in report["results"] if not result["passed"]]
    if not failures:
        lines.append("None.")
    else:
        for result in failures:
            failed_checks = [name for name, passed in result["checks"].items() if not passed]
            lines.append("- `{}`: score {}; failed checks: {}".format(
                result["id"], result["score"], ", ".join(failed_checks)
            ))
    lines.extend((
        "", "This report uses synthetic tool evidence. Real-model mode contacts only the",
        "configured loopback model endpoint; it does not invoke live operational tools.", "",
    ))
    return "\n".join(lines)


def write_reports(report, output_dir):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    markdown_path = directory / "report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path
