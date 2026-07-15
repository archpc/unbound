#!/usr/bin/env python3
"""Export privacy-preserving Time Machine state for node_exporter's textfile collector.

The collector invokes only read-only ``tmutil`` subcommands.  It deliberately never
copies command output into metrics or diagnostics because that output can contain a
backup destination path, host name, or destination identifier.

Usage:
  time_machine_exporter.py --output /path/to/time-machine.prom
  time_machine_exporter.py --test

Without ``--output`` the rendered metrics are written to standard output.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple, Union


TMUTIL = "/usr/bin/tmutil"
COMMAND_TIMEOUT_SECONDS = 30
LATEST_BACKUP_ATTEMPTS = 3
LATEST_BACKUP_RETRY_DELAY_SECONDS = 2

_NO_DESTINATION_RE = re.compile(
    r"\bno\s+(?:time\s+machine\s+)?destinations?\s+(?:configured|found)\b",
    re.IGNORECASE,
)
_RUNNING_RE = re.compile(r"\bRunning\s*=\s*([01])\s*;?", re.IGNORECASE)
_BACKUP_TIMESTAMP_RE = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[- T]"
    r"(\d{2})(?::?)(\d{2})(?::?)(\d{2})(?!\d)"
)


def run_tmutil(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a read-only tmutil command with stable English-oriented locale settings."""
    environment = os.environ.copy()
    environment.update({"LANG": "C", "LC_ALL": "C"})
    try:
        return subprocess.run(
            [TMUTIL, *arguments],
            check=False,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=environment,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            [TMUTIL, *arguments], 127, stdout="", stderr=""
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            [TMUTIL, *arguments], 124, stdout="", stderr=""
        )


def parse_destination_configured(
    result: subprocess.CompletedProcess[str],
) -> Optional[bool]:
    """Return True/False when destination state is known, otherwise None.

    ``tmutil destinationinfo`` can use a non-zero status for the ordinary
    no-destination state, so its documented text is checked before the exit status.
    The potentially sensitive command output is never returned to a caller.
    """
    command_output = f"{result.stdout or ''}\n{result.stderr or ''}"
    if _NO_DESTINATION_RE.search(command_output):
        return False
    if result.returncode == 0 and bool((result.stdout or "").strip()):
        return True
    return None


def parse_backup_running(text: str) -> Optional[int]:
    """Parse tmutil's property-list-like ``Running = 0|1`` status field."""
    match = _RUNNING_RE.search(text)
    if match is None:
        return None
    return int(match.group(1))


def parse_latest_backup_timestamp(text: str) -> Optional[int]:
    """Extract a local-time backup timestamp without returning its containing path.

    Current and older macOS releases expose the time in either
    ``YYYY-MM-DD-HHMMSS`` path components or ``YYYY-MM-DD HH:MM:SS`` text.  If more
    than one timestamp occurs, the final one is used because ``latestbackup`` may
    include parent path components before the selected snapshot.
    """
    matches = list(_BACKUP_TIMESTAMP_RE.finditer(text))
    if not matches:
        return None
    try:
        year, month, day, hour, minute, second = (
            int(value) for value in matches[-1].groups()
        )
        parsed = datetime(year, month, day, hour, minute, second)
        return int(time.mktime(parsed.timetuple()))
    except (OverflowError, ValueError):
        return None


def collect_latest_backup_timestamp() -> Optional[int]:
    """Retry the privacy-safe latest-backup read across transient tmutil gaps."""
    for attempt in range(LATEST_BACKUP_ATTEMPTS):
        result = run_tmutil("latestbackup")
        if result.returncode == 0:
            timestamp = parse_latest_backup_timestamp(result.stdout or "")
            if timestamp is not None:
                return timestamp
        if attempt + 1 < LATEST_BACKUP_ATTEMPTS:
            time.sleep(LATEST_BACKUP_RETRY_DELAY_SECONDS)
    return None


def collect_snapshot() -> Tuple[Dict[str, Optional[int]], List[str]]:
    """Collect a truthful snapshot and generic, privacy-safe diagnostics.

    Unknown values are represented by omitted metric samples rather than invented
    zeroes.  A configured destination with an unreadable latest backup is explicitly
    represented as ``latest_backup_available=0`` and ``collection_success=0``.
    """
    snapshot: Dict[str, Optional[int]] = {
        "destination_configured": None,
        "backup_running": None,
        "latest_backup_available": None,
        "latest_backup_timestamp_seconds": None,
        "collection_success": 1,
    }
    errors: list[str] = []

    destination_result = run_tmutil("destinationinfo")
    configured = parse_destination_configured(destination_result)
    if configured is None:
        snapshot["collection_success"] = 0
        errors.append("tmutil destination state could not be determined")
        return snapshot, errors

    snapshot["destination_configured"] = int(configured)
    if not configured:
        snapshot["backup_running"] = 0
        snapshot["latest_backup_available"] = 0
        return snapshot, errors

    status_result = run_tmutil("status")
    if status_result.returncode == 0:
        running = parse_backup_running(status_result.stdout or "")
        if running is None:
            snapshot["collection_success"] = 0
            errors.append("tmutil status did not contain a recognized running state")
        else:
            snapshot["backup_running"] = running
    else:
        snapshot["collection_success"] = 0
        errors.append("tmutil status failed")

    latest_timestamp = collect_latest_backup_timestamp()
    if latest_timestamp is None:
        snapshot["latest_backup_available"] = 0
        snapshot["collection_success"] = 0
        errors.append("tmutil latest backup timestamp could not be determined")
    else:
        snapshot["latest_backup_available"] = 1
        snapshot["latest_backup_timestamp_seconds"] = latest_timestamp

    return snapshot, errors


_METRICS = (
    (
        "time_machine_destination_configured",
        "destination_configured",
        "Whether Time Machine has a configured backup destination (1=yes, 0=no).",
    ),
    (
        "time_machine_backup_running",
        "backup_running",
        "Whether Time Machine reports a backup currently running (1=yes, 0=no).",
    ),
    (
        "time_machine_latest_backup_available",
        "latest_backup_available",
        "Whether a latest Time Machine backup timestamp was available (1=yes, 0=no).",
    ),
    (
        "time_machine_latest_backup_timestamp_seconds",
        "latest_backup_timestamp_seconds",
        "Unix timestamp of the latest Time Machine backup in the Mac's local timezone.",
    ),
    (
        "time_machine_exporter_collection_success",
        "collection_success",
        "Whether every required Time Machine state value was collected successfully.",
    ),
)


def render_metrics(
    snapshot: Dict[str, Optional[int]], now: Optional[int] = None
) -> List[str]:
    """Render the snapshot without labels or command-derived text."""
    if now is None:
        now = int(time.time())
    lines = [f"# time_machine_exporter generated {now}"]
    for metric_name, snapshot_key, help_text in _METRICS:
        value = snapshot.get(snapshot_key)
        if value is None:
            continue
        lines.extend(
            (
                f"# HELP {metric_name} {help_text}",
                f"# TYPE {metric_name} gauge",
                f"{metric_name} {value}",
            )
        )
    return lines


def write_atomic(
    path: Union[str, os.PathLike[str]], lines: List[str]
) -> None:
    """Replace a metrics file atomically using a temporary file beside it."""
    destination = Path(path)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def self_test() -> bool:
    """Exercise parsing and privacy properties without invoking tmutil."""
    ok = True
    if parse_backup_running("{ Running = 1; }") != 1:
        print("self-test: status parsing failed", file=sys.stderr)
        ok = False
    if parse_backup_running("{ Running = 0; }") != 0:
        print("self-test: idle status parsing failed", file=sys.stderr)
        ok = False
    timestamp = parse_latest_backup_timestamp(
        "/redacted/2026-07-14-175830.backup"
    )
    if timestamp is None:
        print("self-test: latest backup timestamp parsing failed", file=sys.stderr)
        ok = False
    rendered = "\n".join(
        render_metrics(
            {
                "destination_configured": 1,
                "backup_running": 0,
                "latest_backup_available": 1,
                "latest_backup_timestamp_seconds": timestamp,
                "collection_success": 1,
            },
            now=0,
        )
    )
    if "redacted" in rendered or "{" in rendered:
        print("self-test: rendered metrics contained unexpected text", file=sys.stderr)
        ok = False
    if ok:
        print("self-test: OK")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--output",
        metavar="PATH",
        help="write Prometheus textfile output atomically to PATH",
    )
    group.add_argument(
        "--test", action="store_true", help="run offline internal tests and exit"
    )
    arguments = parser.parse_args()

    if arguments.test:
        return 0 if self_test() else 1

    snapshot, errors = collect_snapshot()
    lines = render_metrics(snapshot)
    for message in errors:
        print(f"error: {message}", file=sys.stderr)

    if arguments.output:
        try:
            write_atomic(arguments.output, lines)
        except OSError:
            print("error: failed to write metrics output", file=sys.stderr)
            return 1
    else:
        print("\n".join(lines))

    return 0 if snapshot["collection_success"] == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
