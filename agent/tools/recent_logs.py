"""Bounded, redacted reads from configured log allowlist."""

from __future__ import annotations

from agent.tools.common import read_regular_file_tail_no_follow, redact_text


def build_tool(config):
    def recent_logs(arguments):
        source = arguments["source"]
        maximum_lines = arguments.get("lines", 50)
        path = config.approved_log_sources[source]
        try:
            text = read_regular_file_tail_no_follow(path, 262144)
            lines = text.splitlines()[-maximum_lines:]
            return {"source": source, "available": True,
                    "untrusted_lines": [redact_text(line)[:1000] for line in lines],
                    "redacted": True}
        except (OSError, ValueError):
            return {"source": source, "available": False, "untrusted_lines": [], "redacted": True}
    return recent_logs
