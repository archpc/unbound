#!/usr/bin/env python3
"""Exercise provisioned dashboard and rule PromQL against a Prometheus API.

This is an explicitly live, read-only validator and is therefore NOT called by
``tools/validate-repository.sh``. Run it on the monitored host against loopback, or
provide another trusted API URL. It prints only query identities and result counts;
series labels and values (including private client labels) are never printed.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


RANGES = (
    ("15m", 15 * 60, 15),
    ("1h", 60 * 60, 30),
    ("6h", 6 * 60 * 60, 60),
    ("24h", 24 * 60 * 60, 300),
    ("7d", 7 * 24 * 60 * 60, 1800),
    ("30d", 30 * 24 * 60 * 60, 7200),
)

SCRAPE_INTERVAL_SECONDS = 15

REQUIRED_DEPLOYED_QUERIES = (
    ("target inventory record", "monitoring:target_inventory_ok"),
    ("target-down record", "monitoring:targets_down"),
    ("active-alert record", "monitoring:active_alerts"),
    ("collection-path record", "monitoring:collection_path_healthy"),
    ("textfile-path record", "monitoring:textfile_path_healthy"),
    ("notifier-path record", "monitoring:notifier_local_path_healthy"),
    ("upstream-path record", "dns:upstream_best_success_ratio_10m"),
    ("Grafana probe result", 'probe_success{job="blackbox_grafana"}'),
    ("Time Machine configuration", "time_machine_destination_configured"),
    ("Time Machine running state", "time_machine_backup_running"),
    ("Time Machine backup availability", "time_machine_latest_backup_available"),
    ("Time Machine collection state", "time_machine_exporter_collection_success"),
    ("blocklist source floor", "unbound_blocklist_minimum_lines"),
)


def expand_grafana_macros(expression, step=None):
    """Expand the scrape-aware Grafana interval used by these dashboards."""
    if step is None:
        rate_interval = 4 * SCRAPE_INTERVAL_SECONDS
    else:
        # Grafana's definition is max(query interval + scrape interval,
        # four scrape intervals). This validator's range step stands in for
        # Grafana's query interval at each representative time range.
        rate_interval = max(
            int(step) + SCRAPE_INTERVAL_SECONDS,
            4 * SCRAPE_INTERVAL_SECONDS,
        )
    return expression.replace("$__rate_interval", f"{rate_interval}s")


def api_request(base_url, endpoint, parameters):
    body = urlencode(parameters).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{endpoint}",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=45) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(
            f"{payload.get('errorType', 'api error')}: {payload.get('error', '')}"
        )
    data = payload.get("data", {})
    result = data.get("result", []) if isinstance(data, dict) else []
    return len(result) if isinstance(result, list) else 1


def dashboard_queries(directory):
    queries = []
    for path in sorted(Path(directory).glob("*.json")):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        uid = dashboard["uid"]
        for panel in dashboard.get("panels", []):
            for item in panel.get("targets", []):
                expression = item.get("expr")
                if not expression:
                    continue
                # All-value expansion. One/multiple/no-match client cases are
                # exercised separately without printing any client value.
                expression = expression.replace("${client:regex}", ".*")
                queries.append(
                    {
                        "identity": f"dashboard:{uid}:panel-{panel['id']}:{item.get('refId', '?')}",
                        "expression": expression,
                    }
                )
    return queries


def rule_queries(path):
    """Extract expr scalars/blocks from this repository's Prometheus rule YAML.

    promtool remains the syntax authority. This narrow reader only supplies the
    already-validated expressions to the live API; it is not a YAML validator.
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    queries = []
    current_kind = None
    current_name = None
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^(\s*)-\s+(record|alert):\s*(\S+)\s*$", line)
        if match:
            current_kind, current_name = match.group(2), match.group(3)
            index += 1
            continue
        match = re.match(r"^(\s*)expr:\s*(.*)$", line)
        if match and current_kind and current_name:
            indent = len(match.group(1))
            tail = match.group(2).strip()
            if tail in {"|", ">"}:
                parts = []
                index += 1
                while index < len(lines):
                    candidate = lines[index]
                    candidate_indent = len(candidate) - len(candidate.lstrip())
                    if candidate.strip() and candidate_indent <= indent:
                        break
                    if candidate.strip():
                        parts.append(candidate[indent + 2 :])
                    index += 1
                expression = "\n".join(parts)
            else:
                expression = tail
                index += 1
            queries.append(
                {
                    "identity": f"rule:{current_kind}:{current_name}",
                    "expression": expression,
                }
            )
            continue
        index += 1
    return queries


def validate_task(base_url, query, range_name, duration, step, now):
    parameters = {
        "query": expand_grafana_macros(query["expression"], step),
        "start": f"{now - duration:.3f}",
        "end": f"{now:.3f}",
        "step": str(step),
    }
    try:
        count = api_request(base_url, "/api/v1/query_range", parameters)
        return query["identity"], range_name, count, None
    except Exception as exc:  # API/network errors are reportable validation failures.
        return query["identity"], range_name, 0, str(exc)


def validate_instant(base_url, query, now):
    try:
        count = api_request(
            base_url,
            "/api/v1/query",
            {
                "query": expand_grafana_macros(query["expression"]),
                "time": f"{now:.3f}",
            },
        )
        return query["identity"], count, None
    except Exception as exc:
        return query["identity"], 0, str(exc)


def client_variable_checks(base_url, directory, now):
    """Exercise one, multiple, All, and no-match without exposing label values."""
    def promql_regex(value):
        # re.escape targets the regex engine; the result must then survive the
        # PromQL quoted-string parser used by the dashboard label matcher.
        return re.escape(value).replace("\\", "\\\\").replace('"', '\\"')

    try:
        request = Request(
            f"{base_url.rstrip('/')}/api/v1/query?"
            + urlencode({"query": "unbound_client_rpz_blocks_5m"}),
            method="GET",
        )
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
        result = payload.get("data", {}).get("result", [])
        values = [
            item.get("metric", {}).get("client")
            for item in result
            if item.get("metric", {}).get("client")
        ] if payload.get("status") == "success" else []
    except Exception as exc:
        return [("label-values", 0, str(exc))]

    templates = []
    for path in Path(directory).glob("*.json"):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        for panel in dashboard.get("panels", []):
            for item in panel.get("targets", []):
                expression = item.get("expr", "")
                if "${client:regex}" in expression:
                    templates.append(expression)

    scenarios = [("all", ".*"), ("no-match", "__codex_no_such_client__")]
    if values:
        scenarios.append(("one", promql_regex(values[0])))
    if len(values) >= 2:
        scenarios.append(
            ("multiple", f"({promql_regex(values[0])}|{promql_regex(values[1])})")
        )

    results = []
    for scenario, expansion in scenarios:
        total = 0
        error = None
        for template in templates:
            try:
                total += api_request(
                    base_url,
                    "/api/v1/query",
                    {
                        "query": expand_grafana_macros(
                            template.replace("${client:regex}", expansion)
                        ),
                        "time": f"{now:.3f}",
                    },
                )
            except Exception as exc:
                error = str(exc)
                break
        results.append((scenario, total, error))
    if not values:
        results.append(("one", 0, "SKIPPED: no current client label values"))
    if len(values) < 2:
        results.append(("multiple", 0, "SKIPPED: fewer than two client label values"))

    counts = {scenario: count for scenario, count, error in results if not error}
    revised = []
    for scenario, count, error in results:
        if error:
            revised.append((scenario, count, error))
        elif scenario == "no-match" and count != 0:
            revised.append((scenario, count, "expected zero series for no-match"))
        elif values and scenario in {"all", "one"} and count == 0:
            revised.append((scenario, count, "expected current selected client series"))
        elif (
            len(values) >= 2
            and scenario == "multiple"
            and count <= counts.get("one", 0)
        ):
            revised.append((scenario, count, "multiple selection did not expand beyond one"))
        else:
            revised.append((scenario, count, None))
    return revised


def required_deployed_checks(base_url, now):
    results = []
    for identity, expression in REQUIRED_DEPLOYED_QUERIES:
        try:
            count = api_request(
                base_url,
                "/api/v1/query",
                {"query": expression, "time": f"{now:.3f}"},
            )
            error = None if count else "required deployed series is empty"
        except Exception as exc:
            count, error = 0, str(exc)
        results.append((identity, count, error))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-dir", required=True)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--variables-only",
        action="store_true",
        help="run only variable expansion scenarios",
    )
    parser.add_argument(
        "--require-deployed-state",
        action="store_true",
        help="also fail if canonical new records/probes/collector families are not live",
    )
    args = parser.parse_args()

    now = time.time()
    if args.variables_only:
        variable_results = client_variable_checks(
            args.prometheus_url, args.dashboard_dir, now
        )
        for scenario, count, error in variable_results:
            state = f"error={error}" if error else f"returned_series={count}"
            print(f"client variable {scenario}: {state}")
        variable_errors = [
            item
            for item in variable_results
            if item[2] and not item[2].startswith("SKIPPED")
        ]
        return 1 if variable_errors else 0

    queries = dashboard_queries(args.dashboard_dir) + rule_queries(args.rules)
    # Validate each panel target, not merely each unique expression.
    instant_results = [validate_instant(args.prometheus_url, item, now) for item in queries]
    instant_errors = [item for item in instant_results if item[2]]
    instant_nonempty = sum(1 for _, count, error in instant_results if not error and count)
    print(
        f"instant queries: {len(instant_results)} checked, "
        f"{instant_nonempty} nonempty, {len(instant_results) - instant_nonempty - len(instant_errors)} empty (review required), "
        f"{len(instant_errors)} errors"
    )

    tasks = []
    for query in queries:
        for range_name, duration, step in RANGES:
            tasks.append((query, range_name, duration, step))
    range_results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                validate_task,
                args.prometheus_url,
                query,
                range_name,
                duration,
                step,
                now,
            )
            for query, range_name, duration, step in tasks
        ]
        for future in as_completed(futures):
            range_results.append(future.result())

    range_errors = [item for item in range_results if item[3]]
    for range_name, _, _ in RANGES:
        selected = [item for item in range_results if item[1] == range_name]
        nonempty = sum(1 for _, _, count, error in selected if not error and count)
        errors = sum(1 for item in selected if item[3])
        print(
            f"range {range_name}: {len(selected)} checked, {nonempty} nonempty, "
            f"{len(selected) - nonempty - errors} empty (review required), {errors} errors"
        )

    variable_results = client_variable_checks(
        args.prometheus_url, args.dashboard_dir, now
    )
    for scenario, count, error in variable_results:
        state = f"error={error}" if error else f"returned_series={count}"
        print(f"client variable {scenario}: {state}")

    deployed_results = []
    if args.require_deployed_state:
        deployed_results = required_deployed_checks(args.prometheus_url, now)
        deployed_errors = sum(1 for _, _, error in deployed_results if error)
        deployed_nonempty = sum(1 for _, count, error in deployed_results if count and not error)
        print(
            f"required deployed state: {len(deployed_results)} checked, "
            f"{deployed_nonempty} nonempty, {deployed_errors} errors"
        )
        for identity, _, error in deployed_results:
            if error:
                print(f"ERROR required deployed state {identity}: {error}", file=sys.stderr)

    for identity, _, error in instant_errors:
        print(f"ERROR instant {identity}: {error}", file=sys.stderr)
    for identity, range_name, _, error in range_errors:
        print(f"ERROR range {range_name} {identity}: {error}", file=sys.stderr)

    variable_errors = [
        item for item in variable_results if item[2] and not item[2].startswith("SKIPPED")
    ]
    deployed_errors = [item for item in deployed_results if item[2]]
    return 1 if instant_errors or range_errors or variable_errors or deployed_errors else 0


if __name__ == "__main__":
    sys.exit(main())
