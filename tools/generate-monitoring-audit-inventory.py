#!/usr/bin/env python3
"""Generate the dated inherited-dashboard panel inventory from the audited Git baseline."""

from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BASELINE = "b91a7d02e24c1df02cd6d213dc4dc2736d280bbf"
OUTPUT = ROOT / "docs" / "monitoring-dashboard-panel-inventory-2026-07-14.csv"

FILES = (
    "apple-content-cache.json",
    "network-tailscale.json",
    "prometheus-monitoring.json",
    "services.json",
    "storage-backups.json",
    "system-stats.json",
    "unbound-dns.json",
)

# id: correctness, usefulness, disposition, evidence, operational question
ASSESSMENTS = {
    "apple-content-cache.json": {
        1: ("misleading", "useful", "replace", "Range lastNotNull can retain old ACTIVE after source loss.", "Is the cache currently active?"),
        2: ("technically correct but poorly presented", "limited", "move to drill-down", "Configuration state is not health and has the same stale reducer.", "Is serving activated?"),
        3: ("misleading", "useful", "merge", "Correct boolean source, stale range reducer, duplicated with Active.", "Does AssetCacheManagerUtil report OK?"),
        4: ("semantically incorrect", "low-value", "remove", "Raw registration enumeration has no mapping or documented decision.", "What registration state is reported?"),
        5: ("redundant", "limited", "remove", "Duplicates actual-used/capacity panels and colors ordinary bytes green.", "How many bytes are reported used?"),
        6: ("technically correct but poorly presented", "useful", "merge", "Useful gauge duplicated by multiple capacity panels.", "How many bytes are actually used?"),
        7: ("misleading", "limited", "remove", "The 20 GB threshold has no alert, limit, SLO, or baseline.", "How much configured cache space is free?"),
        8: ("technically correct but poorly presented", "useful", "redesign visualization", "Ratio math is sound; 80/95% severity thresholds are unsupported.", "What fraction of configured cache is used?"),
        9: ("correct", "useful", "retain", "Capacity gauges are valid; duplicate headline stats are unnecessary.", "How are used, free, and limit changing?"),
        10: ("correct", "limited", "remove", "Valid personal-cache detail, but it does not change routine incident decisions.", "How is personal-cache capacity changing?"),
        11: ("technically correct but poorly presented", "useful", "redesign visualization", "Ranking is a range reduction, so removed categories can persist.", "Which cache categories occupy space now?"),
        12: ("technically correct but poorly presented", "limited", "remove", "Metric already represents a 1h maximum and thresholds are unsupported.", "What was peak cache pressure in the source window?"),
        17: ("correct", "limited", "move to drill-down", "Cumulative meaning is explicit but the green total is not health.", "How many bytes have been returned since reset?"),
        18: ("correct", "limited", "remove", "Cumulative meaning is explicit but does not drive a current decision.", "How many origin bytes were stored since reset?"),
        19: ("semantically incorrect", "useful", "rewrite query", "Denominator omits parent/peer, zero-fills idle, and ignores availability/completeness.", "What share of all client bytes came from the local cache?"),
        20: ("correct", "limited", "remove", "Age calculation is correct context, not green health.", "How old are the cumulative counters?"),
        21: ("technically correct but poorly presented", "useful", "replace", "Counter increase is valid but differs from Metrics.db's all-source population.", "How much traffic was returned in 24h?"),
        22: ("correct", "limited", "remove", "Measures fetched-and-stored origin bytes, not all origin-to-client traffic.", "How much was fetched and stored in 24h?"),
        23: ("semantically incorrect", "useful", "rewrite query", "Excludes parent/peer and shows 0% at zero traffic.", "What is the complete local-cache share over 24h?"),
        24: ("misleading", "limited", "remove", "Metric is local-cache bytes, not a proven WAN-savings total under every topology.", "How many local-cache bytes were served since reset?"),
        25: ("misleading", "useful", "rewrite query", "Population is correct but clamp_min renders idle as measured 0%.", "What fraction of client bytes came from origin?"),
    },
    "network-tailscale.json": {
        1: ("correct", "useful", "move to drill-down", "en0 byte counters are correctly rated over 5m.", "What is current Ethernet throughput?"),
        2: ("semantically incorrect", "low-value", "remove", "utun matches five generic tunnels and cannot identify Tailscale or health.", "What is Tailscale throughput?"),
        3: ("technically correct but poorly presented", "limited", "remove", "Heuristic physical-device ranking is diagnostic rather than operational state.", "Which devices receive traffic?"),
        4: ("technically correct but poorly presented", "limited", "remove", "Heuristic physical-device ranking is diagnostic rather than operational state.", "Which devices transmit traffic?"),
        5: ("technically correct but poorly presented", "limited", "move to drill-down", "Counter rate is valid; unit omits per-second semantics.", "Are Ethernet errors increasing?"),
        6: ("technically correct but poorly presented", "limited", "move to drill-down", "Counter rate is valid; unit omits per-second semantics.", "Are receive drops increasing?"),
    },
    "prometheus-monitoring.json": {
        1: ("technically correct but poorly presented", "useful", "retain", "Age is correct; range lastNotNull can retain a stopped process value.", "How long has Prometheus run?"),
        2: ("technically correct but poorly presented", "limited", "remove", "Correct internal count but ordinary information is implicitly green.", "How many TSDB head series exist?"),
        3: ("misleading", "useful", "rewrite query", "Blocks bytes are not total TSDB footprint.", "How much persisted TSDB block storage exists?"),
        4: ("misleading", "critical", "rewrite query", "sum(1-up) cannot detect a removed expected target.", "Are any expected scrape targets down or missing?"),
        5: ("correct", "useful", "retain", "Valid scrape-duration diagnostic.", "Which target scrapes are slow?"),
        6: ("correct", "useful", "retain", "Valid post-relabel sample count by target.", "Which targets drive ingestion?"),
        7: ("technically correct but poorly presented", "useful", "redesign visualization", "Shows named components, not complete disk use.", "How are persisted blocks and head chunks changing?"),
        8: ("redundant", "limited", "remove", "Overlaps head-series and storage diagnostics without a distinct decision.", "How are TSDB head series and chunks changing?"),
        9: ("technically correct but poorly presented", "useful", "retain", "Valid diagnostic hidden behind the only opaque rename transformation.", "Which rule groups evaluate slowly?"),
    },
    "services.json": {
        1: ("redundant", "critical", "merge", "Exact duplicate of Prometheus target-down stat and still lacks inventory.", "Are scrape targets down?"),
        2: ("misleading", "critical", "rewrite query", "A removed job lowers count while remaining targets appear green.", "Are all eight expected targets present?"),
        3: ("misleading", "useful", "rewrite query", "Universal 100k threshold is invalid for supported oisd-small.", "Is the blocklist above its selected source floor?"),
        4: ("decorative", "low-value", "remove", "Warm-list counts are ordinary totals with no decision or alert.", "How is the warm list split?"),
        5: ("technically correct but poorly presented", "useful", "replace", "Boolean target health is shown as continuous lines and missing inventory is invisible.", "Which represented targets were up?"),
        6: ("misleading", "useful", "redesign visualization", "Age threshold aligns with alert but lastNotNull hides current absence.", "Is the blocklist producer fresh?"),
        7: ("misleading", "useful", "redesign visualization", "Age threshold aligns with alert but lastNotNull hides current absence.", "Is the warm-list producer fresh?"),
        8: ("misleading", "useful", "redesign visualization", "Age threshold aligns with alert but lastNotNull hides current absence.", "Is the root-hints producer fresh?"),
        9: ("misleading", "useful", "redesign visualization", "Sign semantics align with alert; absence and unknown are not distinguished.", "Did warmboot complete after this boot?"),
    },
    "storage-backups.json": {
        1: ("correct", "useful", "merge", "Root used math and 90% alert threshold align.", "How full is root now?"),
        2: ("technically correct but poorly presented", "limited", "remove", "Value is valid; 20 GB green threshold is unsupported.", "How many root bytes are free?"),
        3: ("misleading", "harmful", "remove", "A 7d range and lastNotNull show old mounted backup space after unmount.", "How much backup-volume space is free now?"),
        4: ("correct", "limited", "remove", "Volume utilization is valid but transient mounts create noisy history.", "How full are mounted volumes?"),
        5: ("correct", "limited", "remove", "Volume free bytes are valid but do not prove backup health.", "How much space is free by mounted volume?"),
        6: ("misleading", "harmful", "replace", "Mount activity cannot prove a Time Machine backup completed or succeeded.", "Are backups healthy?"),
        7: ("redundant", "limited", "remove", "Duplicates Prometheus storage diagnostics.", "How large are TSDB components?"),
        8: ("mathematically incorrect", "harmful", "remove", ">1000 samples at 15s is about 4h10m, not the stated 15m; reducer can retain an old forecast.", "What is root free predicted in 24h?"),
        9: ("mathematically incorrect", "harmful", "remove", ">12000 samples at 15s is about 50h, not the stated 3d; reducer can retain an old forecast.", "What is root free predicted in 7d?"),
        10: ("technically correct but poorly presented", "limited", "remove", "Regression is plausible but warning thresholds are unsupported.", "What is recent root disk burn rate?"),
        11: ("misleading", "limited", "remove", "No data conflates stable disk, insufficient samples, and missing monitoring.", "How many days until root fills?"),
    },
    "system-stats.json": {
        1: ("technically correct but poorly presented", "useful", "retain", "Correct age; stale range reducer and green uptime are misleading presentation.", "How long has the Mac run?"),
        2: ("correct", "useful", "retain", "Five-minute complement of idle counter is valid.", "How busy is CPU now?"),
        3: ("semantically incorrect", "harmful", "remove", "(active+wired)/total is not macOS memory pressure.", "Is macOS under memory pressure?"),
        4: ("technically correct but poorly presented", "limited", "remove", "Swap gauge is valid; thresholds lack evidence.", "How much swap is used?"),
        5: ("correct", "useful", "retain", "CPU counter rate is valid.", "How is CPU busy changing?"),
        6: ("correct", "limited", "remove", "Valid load gauges but no CPU-count context or decision threshold.", "How is load average changing?"),
        7: ("correct", "useful", "retain", "Useful raw memory categories, not a clean additive pressure model.", "How are memory categories changing?"),
        8: ("redundant", "limited", "remove", "Overlaps memory breakdown without a new decision.", "What fractions are active+wired/compressed?"),
        9: ("correct", "limited", "remove", "Swap counter rates are valid but low-priority for this interface.", "Is the host swapping?"),
        10: ("redundant", "useful", "merge", "Duplicates root storage trend.", "How is root use changing?"),
    },
    "unbound-dns.json": {
        1: ("misleading", "critical", "replace", "unbound_up proves control access, not complete resolver health; stale reducer.", "Can the exporter read Unbound control?"),
        2: ("misleading", "critical", "replace", "Correct represented scrape state, but range lastNotNull and removed-job absence are unsafe.", "Can Prometheus scrape unbound_exporter now?"),
        3: ("technically correct but poorly presented", "critical", "rewrite query", "Valid public-recursive witness mislabeled as Unbound upstream recursion and stale reducer.", "Is at least one public UDP/53 path reliable?"),
        4: ("technically correct but poorly presented", "critical", "retain", "Valid canary; needs correlation with resolver and blackbox paths.", "Is RPZ blocking the canary now?"),
        5: ("correct", "useful", "move to drill-down", "24h counter increase is valid but not first-screen health.", "How many DNS queries occurred in 24h?"),
        6: ("misleading", "useful", "rename", "Global RPZ actions include internal refresh/serve-expired activity.", "How many global RPZ actions occurred in 24h?"),
        7: ("technically correct but poorly presented", "useful", "redesign visualization", "Count is valid; any SERVFAIL red has no documented target.", "How many SERVFAIL answers occurred in 24h?"),
        8: ("technically correct but poorly presented", "limited", "remove", "Cumulative uptime is valid but green range reduction is not a decision.", "How long has Unbound run?"),
        9: ("technically correct but poorly presented", "useful", "retain", "Counter rates are valid; RPZ population needs disclosure.", "How are query and RPZ-action rates changing?"),
        10: ("semantically incorrect", "useful", "rewrite query", "Cache ratio is valid/gated; blocked fraction mixes global internal actions with client query denominator.", "How is cache effectiveness changing under load?"),
        11: ("correct", "useful", "retain", "Rcode counter rates are structurally and semantically valid.", "Which response codes drive answers?"),
        12: ("correct", "limited", "remove", "Record-type rates are valid but lower-priority than incident signals.", "Which query types drive load?"),
        13: ("technically correct but poorly presented", "critical", "retain", "State timeline is suitable; mappings and hot-cache caveat are missing.", "Which DNS synthetic paths were successful?"),
        14: ("correct", "useful", "remove", "Successful-only probe latency filter is valid but endpoint state is more actionable here.", "How long did successful probes take?"),
        15: ("correct", "useful", "retain", "Singleton histogram quantiles correctly aggregate by le.", "How is answer latency distributed?"),
        16: ("unverifiable", "limited", "remove", "Exporter avg/median are cumulative statistics, not a documented windowed distribution.", "What recursion-time statistics does exporter expose?"),
        17: ("correct", "limited", "remove", "Cache entry gauges are valid but not incident-first.", "How many cache entries exist?"),
        18: ("correct", "limited", "remove", "Cache memory gauges are valid but not incident-first.", "How much memory do caches use?"),
        19: ("correct", "useful", "merge", "Prefetch/expired counter rates are valid edge diagnostics.", "Are serve-expired or prefetch edge events changing?"),
        20: ("technically correct but poorly presented", "limited", "remove", "Source families are counter-like but this graph lacks a direct incident decision.", "How are DNSSEC outcomes changing?"),
        21: ("correct", "critical", "retain", "Important gauge tripwire is buried and lacks the alert threshold line.", "Is the requestlist backing up?"),
        22: ("correct", "useful", "merge", "Exceeded/overwritten counter rates are valid requestlist pressure signals.", "Is requestlist pressure increasing?"),
        23: ("technically correct but poorly presented", "limited", "remove", "Cumulative total is explicit but aggregation/thread presentation is weak.", "How many unwanted replies accumulated?"),
        24: ("correct", "limited", "remove", "Valid 24h cache-hit increase duplicates ratio/rate context.", "How many cache hits occurred in 24h?"),
        25: ("correct", "limited", "remove", "Valid 24h cache-miss increase duplicates ratio/rate context.", "How many cache misses occurred in 24h?"),
        26: ("misleading", "limited", "remove", "clamp_min renders zero edge traffic as healthy-looking 0%.", "What share of edge events were served-expired?"),
        27: ("correct", "limited", "merge", "Valid rate but better combined with requestlist/cache edge diagnostics.", "How many cache edge events occur per second?"),
        28: ("technically correct but poorly presented", "useful", "retain", "Instant bounded rolling gauge is valid; empty needs adjacent collector freshness.", "Which sampled clients drove RPZ activity in five minutes?"),
        29: ("technically correct but poorly presented", "useful", "retain", "Timestamp age is valid; clients disappear after five minutes and need freshness context.", "How old is each current sampled client's last RPZ action?"),
    },
}

FUNCTIONS = {
    "sum", "avg", "min", "max", "count", "rate", "irate", "increase",
    "clamp_min", "predict_linear", "count_over_time", "avg_over_time",
    "histogram_quantile", "sort", "topk", "time", "scalar", "by", "without",
    "on", "ignoring", "group_left", "group_right", "and", "or", "unless",
    "bool", "vector",
}
LABELS = {
    "job", "instance", "mode", "device", "mountpoint", "fstype", "client",
    "rcode", "type", "cache", "category", "kind", "file", "task", "le",
}


def source_metrics(expression):
    tokens = re.findall(r"(?<![\"'])\b[a-zA-Z_:][a-zA-Z0-9_:]*\b", expression)
    metrics = []
    for token in tokens:
        if token in FUNCTIONS or token in LABELS or token in {"All", "SERVFAIL"}:
            continue
        if token == "up" or "_" in token or ":" in token:
            if token not in metrics:
                metrics.append(token)
    return metrics


def metric_type(name):
    if name == "up":
        return "Prometheus scrape-state boolean gauge"
    if name == "probe_success":
        return "synthetic-probe boolean gauge"
    if name == "unbound_up":
        return "exporter-to-service boolean gauge"
    if name in {
        "apple_content_cache_active",
        "apple_content_cache_activated",
        "apple_content_cache_status_ok",
        "apple_content_cache_traffic_metrics_available",
        "apple_content_cache_since_reset_traffic_complete",
    }:
        return "boolean state gauge"
    if name == "apple_content_cache_registration_status":
        return "enumerated state gauge"
    if "_last_24h_" in name:
        return "rolling 24-hour gauge"
    if name == "apple_content_cache_max_pressure_last_1h_ratio":
        return "windowed one-hour maximum gauge"
    if name.startswith("apple_content_cache_") and name.endswith("_bytes_total"):
        return "source-reconstructed cumulative counter"
    if name.endswith("_timestamp_seconds") or name in {
        "node_boot_time_seconds", "process_start_time_seconds",
        "apple_content_cache_totals_since_seconds",
        "unbound_client_last_rpz_block_seconds",
        "node_textfile_mtime_seconds",
    }:
        return "timestamp gauge"
    if name.endswith("_bucket"):
        return "histogram bucket counter"
    if name.endswith("_total") or name in {
        "unbound_rpz_action_count", "unbound_answers_bogus",
    }:
        return "counter"
    if name in {"unbound_client_rpz_blocks_5m"}:
        return "sampled rolling-window gauge"
    return "gauge"


def baseline_dashboard(filename):
    path = f"config/grafana/dashboards/{filename}"
    completed = subprocess.run(
        ["git", "show", f"{BASELINE}:{path}"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def rows():
    output = []
    for filename in FILES:
        dashboard = baseline_dashboard(filename)
        assessments = ASSESSMENTS[filename]
        for panel in dashboard["panels"]:
            correctness, usefulness, disposition, evidence, question = assessments[panel["id"]]
            expressions = [item.get("expr", "") for item in panel.get("targets", []) if item.get("expr")]
            metrics = []
            for expression in expressions:
                for name in source_metrics(expression):
                    if name not in metrics:
                        metrics.append(name)
            defaults = panel.get("fieldConfig", {}).get("defaults", {})
            reducer = panel.get("options", {}).get("reduceOptions", {}).get("calcs", [])
            legends = [item.get("legendFormat", "") for item in panel.get("targets", [])]
            output.append({
                "dashboard_file": filename,
                "dashboard_name": dashboard["title"],
                "dashboard_uid": dashboard["uid"],
                "panel_id": panel["id"],
                "panel_title": panel["title"],
                "panel_type": panel["type"],
                "claimed_purpose": panel.get("description") or panel["title"],
                "promql": " ; ".join(" ".join(item.split()) for item in expressions),
                "transformations": compact(panel.get("transformations", [])),
                "reducer": compact(reducer) if reducer else "none",
                "source_metric_families": "; ".join(metrics),
                "metric_types": "; ".join(f"{name}:{metric_type(name)}" for name in metrics),
                "datasource": panel.get("datasource", {}).get("uid", "prometheus"),
                "unit": defaults.get("unit", "none"),
                "decimals": defaults.get("decimals", "auto"),
                "minimum": defaults.get("min", "auto"),
                "maximum": defaults.get("max", "auto"),
                "thresholds": compact(defaults.get("thresholds", {})),
                "value_mappings": compact(defaults.get("mappings", [])),
                "legend": compact(legends),
                "time_override": compact({key: panel[key] for key in ("timeFrom", "timeShift") if key in panel}) or "none",
                "refresh_behavior": dashboard.get("refresh", "off"),
                "variables_used": "client" if any("$client" in item or "${client" in item for item in expressions) else "none",
                "links": compact(panel.get("links", [])),
                "operational_question": question,
                "correctness_status": correctness,
                "usefulness_status": usefulness,
                "recommended_action": disposition,
                "supporting_evidence": evidence,
            })
    return output


def render_csv(inventory):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=list(inventory[0]), lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(inventory)
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    inventory = rows()
    if len(inventory) != 95:
        print(f"expected 95 inherited panels, found {len(inventory)}", file=sys.stderr)
        return 1
    rendered = render_csv(inventory)
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(rendered, encoding="utf-8", newline="")
        print(f"wrote {OUTPUT.relative_to(ROOT)} ({len(inventory)} panels)")
        return 0
    if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
        print(f"inventory differs: {OUTPUT.relative_to(ROOT)}", file=sys.stderr)
        return 1
    print(f"monitoring inventory check: OK ({len(inventory)} panels)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
