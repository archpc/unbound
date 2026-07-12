"""Structured adapter for the canonical read-only deployment verifier."""

from __future__ import annotations

import re

from agent.tools.common import run_fixed


SUMMARY_RE = re.compile(
    r"Summary: ok=(\d+) drift=(\d+) missing_required=(\d+) optional_missing=(\d+) unknown=(\d+)"
)


def _concise_mapping(line):
    body = line.split(None, 1)[1] if " " in line else ""
    mapping = body.split(" | ", 1)[0].strip()
    return mapping[:500]


def build_tool(config):
    def deployment_drift(_arguments):
        completed = run_fixed(
            [config.python_path, config.deployment_verifier_path, "--connect-timeout", "5"],
            min(config.per_tool_timeout_seconds, 30), maximum_bytes=524288,
            cwd=config.repository_root,
        )
        summary = SUMMARY_RE.search(completed["stdout"])
        if summary is None:
            return {"available": False, "ok": 0, "drift": 0, "missing_required": 0,
                    "optional_missing": 0, "unknown": 0, "drifted_paths": [],
                    "result": "unavailable"}
        ok, drift, missing, optional, unknown = [int(value) for value in summary.groups()]
        paths = []
        for line in completed["stdout"].splitlines():
            if line.startswith(("DRIFT", "MISSING")):
                paths.append(_concise_mapping(line))
        result = "drift" if drift or missing else ("incomplete" if unknown else "clean")
        return {"available": True, "ok": ok, "drift": drift, "missing_required": missing,
                "optional_missing": optional, "unknown": unknown, "drifted_paths": paths[:50],
                "result": result}
    return deployment_drift
