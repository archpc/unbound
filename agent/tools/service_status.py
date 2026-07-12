"""Normalized launchd status for fixed, approved labels."""

from __future__ import annotations

import re

from agent.tools.common import run_fixed


PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)
STATE_RE = re.compile(r"^\s*state\s*=\s*([A-Za-z0-9_-]+)\s*$", re.MULTILINE)
EXIT_RE = re.compile(r"^\s*last exit code\s*=\s*(-?\d+)\s*$", re.MULTILINE)


def build_tool(config):
    def service_status(arguments):
        service = arguments["service"]
        label = config.approved_services[service]
        completed = run_fixed([config.launchctl_path, "print", "system/{}".format(label)],
                              min(config.per_tool_timeout_seconds, 10), maximum_bytes=131072)
        if completed["status"] != "ok":
            return {"service": service, "label": label, "loaded": False, "state": "unavailable",
                    "pid": None, "last_exit_code": None}
        state = STATE_RE.search(completed["stdout"])
        pid = PID_RE.search(completed["stdout"])
        exit_code = EXIT_RE.search(completed["stdout"])
        return {"service": service, "label": label, "loaded": True,
                "state": state.group(1).lower() if state else "unknown",
                "pid": int(pid.group(1)) if pid else None,
                "last_exit_code": int(exit_code.group(1)) if exit_code else None}
    return service_status
