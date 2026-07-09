#!/usr/bin/env python3
"""
unbound_client_rpz_exporter.py - Prometheus textfile-collector metrics for
per-client RPZ block activity, parsed from unbound's rpz-log lines in the
macOS unified log (`log show --info`, readable by any admin user - no sudo).

Scope is deliberately RPZ-only. With log-queries:no and log-replies:no (the
config's intended quiet defaults), RPZ hits are the ONLY per-client trace
unbound leaves anywhere - so this exports per-client BLOCK activity, not
per-client query totals or NXDOMAIN counts. Those would require enabling
full query/reply logging, which this stack refuses on principle (noise,
privacy, log volume). A blocked-only view still answers the practical
question: which device on the LAN is the chattiest tracker phone-home.

Roughly half of rpz-log lines carry no client address at all (unbound's own
internal prefetch / serve-expired refresh lookups) - measured 98 of 188 in a
live 5m sample, 2026-07-09. Those are counted separately as "unattributed"
so the per-client numbers never pretend to cover them, and so the sum of
per-client blocks visibly under-runs unbound_rpz_action_count for an honest,
explainable reason.

Metrics are ROLLING 5-MINUTE WINDOW GAUGES recomputed from `log show
--last 5m` on every run (not counters - naming says so honestly):
  unbound_client_rpz_blocks_5m{client="192.168.0.x"}   blocks in last 5m
  unbound_client_last_rpz_block_seconds{client=...}    epoch of newest block
  unbound_client_rpz_unattributed_5m                   internal/no-client lines
  unbound_client_rpz_parse_errors_5m                   lines we failed to parse
  unbound_job_last_success_timestamp_seconds{task="clientstats"}
The last one follows the stack-wide breadcrumb convention (services.json,
BlocklistUpdateStale-style alerting) rather than a bespoke exporter name.
"last_rpz_block", not "last_seen": a client only appears here when it hits
the blocklist, so this must not be read as general LAN presence.

Only private-LAN clients (192.168.0.0/24) plus 127.0.0.1 are emitted, capped
at the top MAX_CLIENTS by block count. Labels are regex-validated IPv4 - no
hostnames (no live reverse DNS from an exporter), no domains (cardinality).

Usage:
  unbound_client_rpz_exporter.py --stdout
  unbound_client_rpz_exporter.py --output /path/to/unbound-clientstats.prom
  unbound_client_rpz_exporter.py --test
"""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

LOG_BIN = "/usr/bin/log"
WINDOW = "5m"
PREDICATE = 'sender == "unbound" AND eventMessage CONTAINS "rpz: applied"'
MAX_CLIENTS = 25
ALLOWED_NETS = [ipaddress.ip_network("192.168.0.0/24"), ipaddress.ip_network("127.0.0.1/32")]

# eventMessage looks like (client field EMPTY on unbound-internal lookups):
#   [21671:2] info: rpz: applied [hagezi-normal] google-analytics.com. rpz-nxdomain 192.168.0.100@50062 google-analytics.com. A IN
#   [21671:1] info: rpz: applied [hagezi-normal] b.gslb.aaplimg.com. rpz-nxdomain  b.gslb.aaplimg.com. A IN
_RPZ_RE = re.compile(r"rpz: applied \[[^\]]+\] \S+ \S+ (?:(\d{1,3}(?:\.\d{1,3}){3})@\d+)? ")


def client_allowed(ip_str):
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in ALLOWED_NETS)


def parse_log_timestamp(ts):
    """'2026-07-09 16:43:37.172390-0400' -> unix epoch, or None."""
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
    except (ValueError, TypeError):
        return None


def parse_ndjson(lines):
    """Returns (blocks_per_client Counter, last_block_epoch dict,
    unattributed count, parse_errors count). A bad line only bumps
    parse_errors - it never aborts the run."""
    blocks = Counter()
    last_block = {}
    unattributed = 0
    parse_errors = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            parse_errors += 1
            continue
        msg = event.get("eventMessage", "")
        if "rpz: applied" not in msg:
            continue  # log show header/summary objects, not an error
        m = _RPZ_RE.search(msg)
        if not m:
            parse_errors += 1
            continue
        client = m.group(1)
        if client is None:
            unattributed += 1
            continue
        if not client_allowed(client):
            continue
        blocks[client] += 1
        epoch = parse_log_timestamp(event.get("timestamp"))
        if epoch is not None and epoch > last_block.get(client, 0):
            last_block[client] = epoch

    return blocks, last_block, unattributed, parse_errors


def run_log_show():
    """Returns (returncode, stdout_text, stderr_text)."""
    try:
        proc = subprocess.run(
            [LOG_BIN, "show", "--info", "--last", WINDOW, "--style", "ndjson",
             "--predicate", PREDICATE],
            check=False, text=True, capture_output=True, timeout=60,
        )
    except FileNotFoundError:
        return 127, "", f"{LOG_BIN} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"log show timed out after 60s"
    return proc.returncode, proc.stdout, proc.stderr


def escape_label(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def build_metrics(blocks, last_block, unattributed, parse_errors, now=None):
    if now is None:
        now = int(time.time())
    lines = [f"# unbound_client_rpz_exporter generated {now}"]

    lines.append("# HELP unbound_client_rpz_blocks_5m RPZ blocks per LAN client over the last 5 minutes (rolling-window gauge, recomputed each run - NOT a counter).")
    lines.append("# TYPE unbound_client_rpz_blocks_5m gauge")
    top = blocks.most_common(MAX_CLIENTS)
    for client, count in top:
        lines.append(f'unbound_client_rpz_blocks_5m{{client="{escape_label(client)}"}} {count}')

    lines.append("# HELP unbound_client_last_rpz_block_seconds Unix timestamp of the newest RPZ block per LAN client within the 5m window. Blocked-traffic recency only - NOT general last-seen.")
    lines.append("# TYPE unbound_client_last_rpz_block_seconds gauge")
    for client, _ in top:
        if client in last_block:
            lines.append(f'unbound_client_last_rpz_block_seconds{{client="{escape_label(client)}"}} {int(last_block[client])}')

    lines.append("# HELP unbound_client_rpz_unattributed_5m RPZ log lines in the last 5 minutes with no client address (unbound-internal prefetch/serve-expired refresh lookups).")
    lines.append("# TYPE unbound_client_rpz_unattributed_5m gauge")
    lines.append(f"unbound_client_rpz_unattributed_5m {unattributed}")

    lines.append("# HELP unbound_client_rpz_parse_errors_5m Log lines in the last 5 minutes this exporter failed to parse.")
    lines.append("# TYPE unbound_client_rpz_parse_errors_5m gauge")
    lines.append(f"unbound_client_rpz_parse_errors_5m {parse_errors}")

    # Deliberately NO "# HELP" here: the other five breadcrumb producers emit
    # TYPE-only, and node_exporter rewrites a multi-file family's help to
    # "Metric read from <files>" - a sample carrying its own conflicting HELP
    # gets REJECTED from the merged family (error in node_exporter.log on
    # every scrape, while node_textfile_scrape_error stays 0). Found live
    # 2026-07-09 at first daemon start: everything looked healthy except the
    # clientstats breadcrumb silently missing from /metrics.
    lines.append("# TYPE unbound_job_last_success_timestamp_seconds gauge")
    lines.append(f'unbound_job_last_success_timestamp_seconds{{task="clientstats"}} {now}')

    return lines


def write_atomic(path, lines):
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")
    os.replace(tmp, path)


_FIXTURE = r"""
{"timestamp":"2026-07-09 16:43:37.172390-0400","eventMessage":"[21671:2] info: rpz: applied [hagezi-normal] google-analytics.com. rpz-nxdomain 192.168.0.100@50062 google-analytics.com. A IN"}
{"timestamp":"2026-07-09 16:43:40.100000-0400","eventMessage":"[21671:1] info: rpz: applied [secondary] *.conviva.com. rpz-nxdomain 192.168.0.86@56509 x.cws.conviva.com. A IN"}
{"timestamp":"2026-07-09 16:43:41.000000-0400","eventMessage":"[21671:2] info: rpz: applied [hagezi-normal] google-analytics.com. rpz-nxdomain 192.168.0.100@50063 google-analytics.com. AAAA IN"}
{"timestamp":"2026-07-09 16:44:04.101460-0400","eventMessage":"[21671:1] info: rpz: applied [hagezi-normal] b.gslb.aaplimg.com. rpz-nxdomain  b.gslb.aaplimg.com. A IN"}
{"timestamp":"2026-07-09 16:44:05.000000-0400","eventMessage":"[21671:1] info: rpz: applied [hagezi-normal] evil.example.com. rpz-nxdomain 10.9.9.9@1234 evil.example.com. A IN"}
{"timestamp":"2026-07-09 16:44:06.000000-0400","eventMessage":"[21671:0] info: service stopped (unbound 1.25.1)"}
this line is not json at all
"""


def self_test():
    ok = True
    blocks, last_block, unattributed, parse_errors = parse_ndjson(_FIXTURE.strip().splitlines())

    if blocks != Counter({"192.168.0.100": 2, "192.168.0.86": 1}):
        print(f"FAIL blocks = {dict(blocks)}", file=sys.stderr)
        ok = False
    if unattributed != 1:
        print(f"FAIL unattributed = {unattributed}, expected 1", file=sys.stderr)
        ok = False
    if parse_errors != 1:  # only the non-JSON line; the non-rpz message is skipped silently
        print(f"FAIL parse_errors = {parse_errors}, expected 1", file=sys.stderr)
        ok = False
    if "10.9.9.9" in blocks:
        print("FAIL non-LAN client 10.9.9.9 must be filtered out", file=sys.stderr)
        ok = False

    epoch = last_block.get("192.168.0.100")
    # the AAAA line at :43:41 is newer than the A line at :43:37
    if epoch is None or abs(epoch - parse_log_timestamp("2026-07-09 16:43:41.000000-0400")) > 0.001:
        print(f"FAIL last_block[192.168.0.100] = {epoch}", file=sys.stderr)
        ok = False

    rendered = "\n".join(build_metrics(blocks, last_block, unattributed, parse_errors, now=1751000000))
    for expect in (
        'unbound_client_rpz_blocks_5m{client="192.168.0.100"} 2',
        'unbound_client_rpz_blocks_5m{client="192.168.0.86"} 1',
        'unbound_client_last_rpz_block_seconds{client="192.168.0.100"}',
        "unbound_client_rpz_unattributed_5m 1",
        "unbound_client_rpz_parse_errors_5m 1",
        'unbound_job_last_success_timestamp_seconds{task="clientstats"} 1751000000',
        "# TYPE unbound_client_rpz_blocks_5m gauge",
        "# HELP unbound_client_rpz_blocks_5m",
    ):
        if expect not in rendered:
            print(f"FAIL expected substring not in rendered output: {expect!r}", file=sys.stderr)
            ok = False
    if "10.9.9.9" in rendered or "evil.example.com" in rendered or "conviva" in rendered:
        print("FAIL rendered output must contain no non-LAN IPs and no domains", file=sys.stderr)
        ok = False

    if ok:
        print("self-test: OK")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stdout", action="store_true", help="print metrics to stdout instead of writing a file")
    group.add_argument("--output", metavar="PATH", help="write Prometheus textfile output atomically to PATH")
    group.add_argument("--test", action="store_true", help="run internal self-tests and exit")
    args = parser.parse_args()

    if args.test:
        sys.exit(0 if self_test() else 1)

    returncode, stdout_text, stderr_text = run_log_show()
    if returncode != 0:
        # Exit without writing: the previous .prom stays in place and the
        # clientstats breadcrumb goes stale - same semantics as the blocklist.
        print(f"log show failed (exit {returncode}): {stderr_text.strip()}", file=sys.stderr)
        sys.exit(1)

    blocks, last_block, unattributed, parse_errors = parse_ndjson(stdout_text.splitlines())
    lines = build_metrics(blocks, last_block, unattributed, parse_errors)

    if args.output:
        try:
            write_atomic(args.output, lines)
        except OSError as exc:
            print(f"failed to write {args.output}: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
