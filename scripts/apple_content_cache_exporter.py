#!/usr/bin/env python3
"""
apple_content_cache_exporter.py - Prometheus textfile-collector metrics for
macOS Content Caching, parsed from `AssetCacheManagerUtil status` (no sudo
required - the status/settings/isActivated/canActivate subcommands are
readable by any local user).

Only fields explicitly needed for monitoring are exported; the tool reports
many more (ServerGUID, PublicAddress, RestrictedMedia, ...) that are left out
on purpose - either not useful for alerting/graphing, or (public/private IP)
not something that belongs baked into Prometheus labels.

TotalBytesReturnedToClients / TotalBytesStoredFromOrigin are exported as
counters (_total): the tool itself reports `TotalBytesAreSince: <timestamp>`,
which only makes sense as the reset epoch of a cumulative counter, not a
point-in-time gauge. apple_content_cache_totals_since_seconds is exported
alongside them so a Grafana panel can tell a real reset (service restart)
apart from a rate dip.

Usage:
  apple_content_cache_exporter.py --stdout
  apple_content_cache_exporter.py --output /path/to/apple-content-cache.prom
  apple_content_cache_exporter.py --test
"""
import argparse
import os
import re
import subprocess
import sys
import time

ASSET_CACHE_MANAGER_UTIL = "/usr/bin/AssetCacheManagerUtil"

_SIZE_RE = re.compile(r"^([\d.]+)\s*(KB|MB|GB|TB)$", re.IGNORECASE)
_PERCENT_RE = re.compile(r"^([\d.]+)\s*%$")
# Decimal (1000-based) units: AssetCacheManagerUtil's GB figures line up with
# macOS's decimal-GB convention (e.g. a "199 GB" cache limit on round disk
# sizing), not binary GiB. Flagged here since it's an assumption, not a spec.
_UNIT_MULTIPLIER = {"KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def parse_size_to_bytes(value):
    """'55.79 GB' -> 55790000000.0, 'Zero KB' -> 0.0. Returns None if unparseable."""
    value = value.strip()
    if value.lower() == "zero kb" or value.lower() == "zero":
        return 0.0
    m = _SIZE_RE.match(value)
    if not m:
        return None
    number, unit = m.groups()
    try:
        return float(number) * _UNIT_MULTIPLIER[unit.upper()]
    except ValueError:
        return None


def parse_percent_to_ratio(value):
    """'0%' -> 0.0, '12.5%' -> 0.125. Returns None if unparseable."""
    m = _PERCENT_RE.match(value.strip())
    if not m:
        return None
    try:
        return float(m.group(1)) / 100.0
    except ValueError:
        return None


def parse_bool(value):
    v = value.strip().lower()
    if v == "true":
        return 1
    if v == "false":
        return 0
    return None


def parse_status_text(text):
    """Parse `AssetCacheManagerUtil status` output into a flat dict of top-level
    fields (raw string values) plus a 'CacheDetails' dict of category->raw
    string values. Unknown/extra fields are simply present in the dict and
    ignored by the caller - nothing here raises on an unexpected key.
    """
    lines = text.splitlines()
    top = {}
    cache_details = {}

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # top-level entries are indented exactly 4 spaces: "    Key: value"
        m = re.match(r"^ {4}([A-Za-z0-9_]+):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2).strip()

        if key == "CacheDetails":
            # value looks like "(4)"; children are 8-space-indented "Name: size"
            i += 1
            while i < n and re.match(r"^ {8}(.+?):\s*(.+)$", lines[i]):
                cm = re.match(r"^ {8}(.+?):\s*(.+)$", lines[i])
                cache_details[cm.group(1)] = cm.group(2).strip()
                i += 1
            continue

        # Other parenthesized sections (Parents, Peers, PrivateAddresses) may
        # have 8-space-indented bare-value children we don't currently export;
        # skip over them so they aren't misread as top-level keys.
        if re.match(r"^\(\d+\)$", value) or value.lower() == "(none)":
            i += 1
            while i < n and re.match(r"^ {8}\S", lines[i]):
                i += 1
            continue

        top[key] = value
        i += 1

    return top, cache_details


class Metric:
    def __init__(self, name, mtype, help_text):
        self.name = name
        self.mtype = mtype
        self.help_text = help_text
        self.samples = []  # list of (labels_dict_or_None, value)

    def add(self, value, labels=None):
        self.samples.append((labels, value))

    def render(self):
        if not self.samples:
            return []
        out = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} {self.mtype}"]
        for labels, value in self.samples:
            if labels:
                label_str = ",".join(f'{k}="{escape_label(v)}"' for k, v in labels.items())
                out.append(f"{self.name}{{{label_str}}} {value}")
            else:
                out.append(f"{self.name} {value}")
        return out


def escape_label(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def build_metrics(top, cache_details, now=None):
    """Returns (list_of_rendered_lines, warnings). Missing optional fields are
    silently omitted (not an error) - see module docstring."""
    if now is None:
        now = int(time.time())
    warnings = []
    metrics = []

    def want(key, parser, name, mtype, help_text, source=None):
        raw = (source or top).get(key)
        if raw is None:
            warnings.append(f"field {key!r} not present; skipping {name}")
            return
        parsed = parser(raw)
        if parsed is None:
            warnings.append(f"field {key!r}={raw!r} could not be parsed; skipping {name}")
            return
        m = Metric(name, mtype, help_text)
        m.add(parsed)
        metrics.append(m)

    want("Activated", parse_bool, "apple_content_cache_activated", "gauge",
         "Whether Apple Content Cache is activated (registered to serve).")
    want("Active", parse_bool, "apple_content_cache_active", "gauge",
         "Whether Apple Content Cache is currently active.")

    if "CacheStatus" in top:
        m = Metric("apple_content_cache_status_ok", "gauge",
                    "1 if CacheStatus reported by AssetCacheManagerUtil is OK, else 0.")
        m.add(1 if top["CacheStatus"].strip().upper() == "OK" else 0)
        metrics.append(m)
    else:
        warnings.append("field 'CacheStatus' not present; skipping apple_content_cache_status_ok")

    want("RegistrationStatus", lambda v: _try_float(v), "apple_content_cache_registration_status",
         "gauge", "Raw RegistrationStatus value reported by AssetCacheManagerUtil.")

    for key, name, help_text in [
        ("ActualCacheUsed", "apple_content_cache_actual_used_bytes", "Actual disk bytes used by the content cache."),
        ("CacheUsed", "apple_content_cache_used_bytes", "Reported cache bytes used (may include reserved space)."),
        ("CacheFree", "apple_content_cache_free_bytes", "Cache bytes free."),
        ("CacheLimit", "apple_content_cache_limit_bytes", "Configured cache size limit in bytes."),
        ("PersonalCacheUsed", "apple_content_cache_personal_used_bytes", "Personal (iCloud) cache bytes used."),
        ("PersonalCacheFree", "apple_content_cache_personal_free_bytes", "Personal (iCloud) cache bytes free."),
        ("PersonalCacheLimit", "apple_content_cache_personal_limit_bytes", "Personal (iCloud) cache size limit in bytes."),
    ]:
        want(key, parse_size_to_bytes, name, "gauge", help_text)

    if cache_details:
        m = Metric("apple_content_cache_detail_bytes", "gauge",
                    "Cache bytes used per content category.")
        any_ok = False
        for category, raw in cache_details.items():
            size = parse_size_to_bytes(raw)
            if size is None:
                warnings.append(f"CacheDetails[{category!r}]={raw!r} could not be parsed; skipping")
                continue
            m.add(size, labels={"category": category})
            any_ok = True
        if any_ok:
            metrics.append(m)
    else:
        warnings.append("field 'CacheDetails' not present or empty; skipping apple_content_cache_detail_bytes")

    want("MaxCachePressureLast1Hour", parse_percent_to_ratio,
         "apple_content_cache_max_pressure_last_1h_ratio", "gauge",
         "Max cache pressure over the last hour, as a 0-1 ratio.")

    # Counters: justified by TotalBytesAreSince being a reset-epoch timestamp
    # (see module docstring). If that field is ever absent, fall back to
    # gauge-style naming rather than assert a counter semantic we can't back up.
    since_raw = top.get("TotalBytesAreSince")
    since_epoch = parse_timestamp_to_epoch(since_raw) if since_raw is not None else None
    is_counter = since_epoch is not None
    suffix = "_total" if is_counter else ""
    # Only warn on the UNEXPECTED path (gauge fallback). The counter case is
    # this exporter's documented normal behavior - warning about it on every
    # scheduled run just fills the launchd log with identical noise
    # (~288 lines/day at a 300s interval).
    if not is_counter:
        warnings.append("ReturnedToClients/StoredFromOrigin: TotalBytesAreSince missing or invalid -> "
                        "could not confirm counter semantics; using gauge naming (no _total, "
                        "and Grafana increase() panels will be wrong until this is resolved)")

    want("TotalBytesReturnedToClients", parse_size_to_bytes,
         f"apple_content_cache_returned_to_clients_bytes{suffix}",
         "counter" if is_counter else "gauge",
         "Bytes served to LAN clients from the cache" + (" (cumulative since apple_content_cache_totals_since_seconds)." if is_counter else "."))
    want("TotalBytesStoredFromOrigin", parse_size_to_bytes,
         f"apple_content_cache_stored_from_origin_bytes{suffix}",
         "counter" if is_counter else "gauge",
         "Bytes fetched from the internet origin and stored in the cache" + (" (cumulative since apple_content_cache_totals_since_seconds)." if is_counter else "."))

    if since_raw is not None:
        if since_epoch is not None:
            m = Metric("apple_content_cache_totals_since_seconds", "gauge",
                        "Unix timestamp when the TotalBytes* counters were last reset (service restart).")
            m.add(since_epoch)
            metrics.append(m)
        else:
            warnings.append(f"TotalBytesAreSince={since_raw!r} could not be parsed as a timestamp")

    if "CacheStatus" in top and "Port" in top:
        m = Metric("apple_content_cache_info", "gauge",
                    "Static info: 1 if present. Deliberately excludes public/private IP labels.")
        m.add(1, labels={"cache_status": top["CacheStatus"].strip(), "port": top["Port"].strip()})
        metrics.append(m)

    lines = [f"# apple_content_cache_exporter generated {now}"]
    for m in metrics:
        lines.extend(m.render())
    return lines, warnings


def _try_float(v):
    try:
        return float(v.strip())
    except (ValueError, AttributeError):
        return None


def parse_timestamp_to_epoch(value):
    """'2026-07-04 14:14:49' -> unix epoch (assumes local time, no tz in output)."""
    try:
        return int(time.mktime(time.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        return None


def run_asset_cache_manager_util():
    """Returns (returncode, stdout_text, stderr_text)."""
    try:
        proc = subprocess.run(
            [ASSET_CACHE_MANAGER_UTIL, "status"],
            check=False, text=True, capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        return 127, "", f"{ASSET_CACHE_MANAGER_UTIL} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{ASSET_CACHE_MANAGER_UTIL} status timed out after 30s"
    return proc.returncode, proc.stdout, proc.stderr


def write_atomic(path, lines):
    directory = os.path.dirname(path) or "."
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")
    os.replace(tmp, path)


def self_test():
    """Exercises the size/percent/bool/timestamp parsers plus the full
    fixture-based parse+build pipeline. Returns True on success."""
    ok = True

    cases = [
        ("Zero KB", 0.0), ("4.9 MB", 4.9 * 1000**2), ("55.79 GB", 55.79 * 1000**3),
        ("199 GB", 199 * 1000**3), ("1.82 GB", 1.82 * 1000**3),
    ]
    for raw, expected in cases:
        got = parse_size_to_bytes(raw)
        if got is None or abs(got - expected) > 1:
            print(f"FAIL parse_size_to_bytes({raw!r}) = {got!r}, expected {expected!r}", file=sys.stderr)
            ok = False

    if parse_size_to_bytes("not a size") is not None:
        print("FAIL parse_size_to_bytes should return None for garbage input", file=sys.stderr)
        ok = False

    if parse_percent_to_ratio("0%") != 0.0 or parse_percent_to_ratio("12.5%") != 0.125:
        print("FAIL parse_percent_to_ratio", file=sys.stderr)
        ok = False

    if parse_bool("true") != 1 or parse_bool("false") != 0 or parse_bool("maybe") is not None:
        print("FAIL parse_bool", file=sys.stderr)
        ok = False

    if parse_timestamp_to_epoch("2026-07-04 14:14:49") is None:
        print("FAIL parse_timestamp_to_epoch", file=sys.stderr)
        ok = False

    fixture_path = os.path.join(os.path.dirname(__file__), "..", "fixtures",
                                 "asset-cache-status.sample.txt")
    if os.path.exists(fixture_path):
        with open(fixture_path) as fh:
            text = fh.read()
        top, details = parse_status_text(text)
        expected_top_keys = {
            "Activated", "Active", "ActualCacheUsed", "CacheFree", "CacheLimit",
            "CacheStatus", "CacheUsed", "MaxCachePressureLast1Hour",
            "PersonalCacheFree", "PersonalCacheLimit", "PersonalCacheUsed",
            "Port", "PublicAddress", "RegistrationStatus", "RestrictedMedia",
            "ServerGUID", "StartupStatus", "TetheratorStatus",
            "TotalBytesAreSince", "TotalBytesDropped", "TotalBytesImported",
            "TotalBytesReturnedToChildren", "TotalBytesReturnedToClients",
            "TotalBytesReturnedToPeers", "TotalBytesStoredFromOrigin",
            "TotalBytesStoredFromParents", "TotalBytesStoredFromPeers",
        }
        missing = expected_top_keys - set(top.keys())
        if missing:
            print(f"FAIL fixture parse missing keys: {missing}", file=sys.stderr)
            ok = False
        if set(details.keys()) != {"iCloud", "iOS Software", "Mac Software", "Other"}:
            print(f"FAIL fixture CacheDetails keys: {details.keys()}", file=sys.stderr)
            ok = False

        lines, warnings = build_metrics(top, details, now=1751000000)
        rendered = "\n".join(lines)
        for expect in (
            "apple_content_cache_activated 1",
            "apple_content_cache_active 1",
            "apple_content_cache_status_ok 1",
            "apple_content_cache_registration_status 1.0",
            'apple_content_cache_detail_bytes{category="iCloud"}',
            "apple_content_cache_returned_to_clients_bytes_total",
            "apple_content_cache_stored_from_origin_bytes_total",
            "apple_content_cache_totals_since_seconds",
        ):
            if expect not in rendered:
                print(f"FAIL expected substring not in rendered output: {expect!r}", file=sys.stderr)
                ok = False
        if "PublicAddress" in rendered or "PrivateAddresses" in rendered or "63.143" in rendered:
            print("FAIL rendered output must not contain address fields", file=sys.stderr)
            ok = False
    else:
        print(f"WARN fixture not found at {fixture_path}, skipping fixture-based checks", file=sys.stderr)

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

    returncode, stdout_text, stderr_text = run_asset_cache_manager_util()
    if returncode != 0:
        print(f"AssetCacheManagerUtil status failed (exit {returncode}): {stderr_text.strip()}",
              file=sys.stderr)
        sys.exit(1)

    top, cache_details = parse_status_text(stdout_text)
    if not top:
        print("AssetCacheManagerUtil status produced no parseable fields", file=sys.stderr)
        sys.exit(1)

    lines, warnings = build_metrics(top, cache_details)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

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
