#!/usr/bin/env python3
"""Generate the repository-managed Grafana dashboards deterministically.

The dashboard JSON remains the deployed artifact.  This small builder keeps repeated
Grafana defaults consistent and makes panel queries, reducers, missing-data behavior,
and navigation reviewable without hand-editing thousands of JSON lines.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = ROOT / "config" / "grafana" / "dashboards"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}
SCHEMA_VERSION = 39

DASHBOARDS = (
    ("operations-overview", "Operational Overview"),
    ("unbound-dns", "DNS and Unbound"),
    ("host-services", "Host, Services, and Backups"),
    ("apple-content-cache", "Apple Content Cache"),
    ("prometheus-monitoring", "Prometheus Diagnostics"),
)

RETIRED_DASHBOARD_FILES = {
    "network-tailscale.json",
    "services.json",
    "storage-backups.json",
    "system-stats.json",
}


def target(expr, ref_id="A", legend=None, *, instant=False, fmt=None):
    value = {
        "datasource": DATASOURCE,
        "expr": expr,
        "instant": instant,
        "range": not instant,
        "refId": ref_id,
    }
    if legend is not None:
        value["legendFormat"] = legend
    if fmt is not None:
        value["format"] = fmt
    return value


def thresholds(*steps, base="blue"):
    return {
        "mode": "absolute",
        "steps": [{"color": base, "value": None}]
        + [{"color": color, "value": value} for value, color in steps],
    }


def boolean_mappings(good="HEALTHY", bad="FAILED"):
    return [
        {
            "type": "value",
            "options": {
                "0": {"color": "red", "index": 1, "text": bad},
                "1": {"color": "green", "index": 0, "text": good},
            },
        }
    ]


def panel_link(title, uid):
    return {
        "targetBlank": False,
        "title": title,
        "url": f"/d/{uid}?${{__url_time_range}}",
    }


def grid(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def row(panel_id, title, y):
    return {
        "id": panel_id,
        "title": title,
        "type": "row",
        "collapsed": False,
        "gridPos": grid(0, y, 24, 1),
        "panels": [],
    }


def stat(
    panel_id,
    title,
    expr,
    pos,
    description,
    *,
    unit="none",
    mappings=None,
    threshold_config=None,
    decimals=None,
    minimum=None,
    maximum=None,
    no_value="UNKNOWN",
    color_mode="value",
    links=None,
):
    defaults = {
        "unit": unit,
        "noValue": no_value,
        "color": {"mode": "thresholds"},
        "thresholds": threshold_config or thresholds(),
        "mappings": mappings or [],
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    value = {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "stat",
        "datasource": DATASOURCE,
        "gridPos": pos,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "colorMode": color_mode,
            "graphMode": "none",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {
                "calcs": ["lastNotNull"],
                "fields": "",
                "values": False,
            },
            "textMode": "auto",
            "wideLayout": True,
        },
        "targets": [target(expr, instant=True)],
    }
    if links:
        value["links"] = links
    return value


def timeseries(
    panel_id,
    title,
    queries,
    pos,
    description,
    *,
    unit="short",
    decimals=None,
    minimum=None,
    maximum=None,
    threshold_config=None,
    stacking="none",
    legend="list",
    links=None,
):
    defaults = {
        "unit": unit,
        "color": {"mode": "palette-classic"},
        "custom": {
            "axisCenteredZero": False,
            "axisColorMode": "text",
            "axisLabel": "",
            "axisPlacement": "auto",
            "barAlignment": 0,
            "drawStyle": "line",
            "fillOpacity": 8,
            "gradientMode": "none",
            "hideFrom": {"legend": False, "tooltip": False, "viz": False},
            "lineInterpolation": "linear",
            "lineWidth": 2,
            "pointSize": 5,
            "scaleDistribution": {"type": "linear"},
            "showPoints": "never",
            "spanNulls": False,
            "stacking": {"group": "A", "mode": stacking},
            "thresholdsStyle": {"mode": "line" if threshold_config else "off"},
        },
        "mappings": [],
        "thresholds": threshold_config or thresholds(),
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    built_targets = []
    for index, query in enumerate(queries):
        expr, legend_format = query
        built_targets.append(target(expr, chr(ord("A") + index), legend_format))
    value = {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "timeseries",
        "datasource": DATASOURCE,
        "gridPos": pos,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {
                "calcs": ["lastNotNull"] if legend == "table" else [],
                "displayMode": legend,
                "placement": "bottom",
                "showLegend": True,
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": built_targets,
    }
    if links:
        value["links"] = links
    return value


def state_timeline(panel_id, title, queries, pos, description, *, links=None):
    built_targets = []
    for index, query in enumerate(queries):
        expr, legend_format = query
        built_targets.append(target(expr, chr(ord("A") + index), legend_format))
    value = {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "state-timeline",
        "datasource": DATASOURCE,
        "gridPos": pos,
        "fieldConfig": {
            "defaults": {
                "unit": "none",
                "noValue": "UNKNOWN",
                "color": {"mode": "thresholds"},
                "mappings": boolean_mappings(),
                "thresholds": thresholds((1, "green"), base="red"),
                "custom": {"fillOpacity": 80, "lineWidth": 0, "spanNulls": False},
            },
            "overrides": [],
        },
        "options": {
            "alignValue": "left",
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "mergeValues": True,
            "rowHeight": 0.9,
            "showValue": "auto",
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": built_targets,
    }
    if links:
        value["links"] = links
    return value


def bar_gauge(
    panel_id,
    title,
    queries,
    pos,
    description,
    *,
    unit="short",
    threshold_config=None,
    minimum=None,
    maximum=None,
    no_value="NO DATA",
    links=None,
):
    defaults = {
        "unit": unit,
        "noValue": no_value,
        "color": {"mode": "thresholds"},
        "mappings": [],
        "thresholds": threshold_config or thresholds(),
    }
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    built_targets = []
    for index, query in enumerate(queries):
        expr, legend_format = query
        built_targets.append(
            target(expr, chr(ord("A") + index), legend_format, instant=True)
        )
    value = {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "bargauge",
        "datasource": DATASOURCE,
        "gridPos": pos,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "displayMode": "basic",
            "minVizHeight": 10,
            "minVizWidth": 0,
            "namePlacement": "auto",
            "orientation": "horizontal",
            "reduceOptions": {
                "calcs": ["lastNotNull"],
                "fields": "",
                "values": False,
            },
            "showUnfilled": True,
            "sizing": "auto",
            "valueMode": "color",
        },
        "targets": built_targets,
    }
    if links:
        value["links"] = links
    return value


def table_panel(panel_id, title, expr, pos, description, *, links=None):
    value = {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "table",
        "datasource": DATASOURCE,
        "gridPos": pos,
        "fieldConfig": {
            "defaults": {
                "unit": "none",
                "noValue": "NO ACTIVE ALERTS",
                "color": {"mode": "thresholds"},
                "mappings": [],
                "thresholds": thresholds((1, "red"), base="red"),
            },
            "overrides": [],
        },
        "options": {
            "cellHeight": "sm",
            "footer": {"enablePagination": True, "show": False},
            "showHeader": True,
        },
        "targets": [target(expr, instant=True, fmt="table")],
    }
    if links:
        value["links"] = links
    return value


def text_panel(panel_id, title, content, pos, description=""):
    return {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "text",
        "gridPos": pos,
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"content": content, "mode": "markdown"},
    }


def dashboard_links(current_uid):
    links = []
    for uid, title in DASHBOARDS:
        if uid == current_uid:
            continue
        links.append(
            {
                "asDropdown": False,
                "icon": "external link",
                "includeVars": True,
                "keepTime": True,
                "tags": [],
                "targetBlank": False,
                "title": title,
                "tooltip": "",
                "type": "link",
                "url": f"/d/{uid}?${{__url_time_range}}",
            }
        )
    return links


def make_dashboard(uid, title, description, panels, *, refresh="30s", start="now-6h", tags=(), templating=None, version=1):
    return {
        "annotations": {"list": []},
        "description": description,
        "editable": False,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": dashboard_links(uid),
        "liveNow": False,
        "panels": panels,
        "refresh": refresh,
        "schemaVersion": SCHEMA_VERSION,
        "tags": list(tags) + ["mac-mini", "homelab"],
        "templating": {"list": templating or []},
        "time": {"from": start, "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "title": title,
        "uid": uid,
        "version": version,
        "weekStart": "monday",
    }


APPLE_FRESH = 'time() - node_textfile_mtime_seconds{file=~".*/apple-content-cache[.]prom"} < 900'
CLIENT_FRESH = 'time() - unbound_job_last_success_timestamp_seconds{task="clientstats"} < 900'
TM_FRESH = 'time() - node_textfile_mtime_seconds{file=~".*/time-machine[.]prom"} < 3600'


def gated(metric, freshness):
    return f"({metric}) and on() ({freshness})"


def time_machine_backup_age_state():
    """Return age, -1 for explicitly unconfigured, and empty for unknown/stale."""
    return (
        f'{gated("time() - time_machine_latest_backup_timestamp_seconds", TM_FRESH)} '
        f'or on() (((time_machine_destination_configured == 0) - 1) '
        f'and on() ({TM_FRESH}))'
    )


def time_machine_running_state():
    """Return 0/1 while configured, -1 when disabled, and empty when unknown."""
    return (
        f'({gated("time_machine_backup_running", TM_FRESH)}) '
        'and on() (time_machine_destination_configured == 1) '
        f'or on() (((time_machine_destination_configured == 0) - 1) '
        f'and on() ({TM_FRESH}))'
    )


def apple_enabled_state(metric):
    """Return metric 0/1 while enabled, -1 when disabled, empty when stale."""
    state = (
        f'(({metric}) and on() (apple_content_cache_activated == 1)) '
        'or on() ((apple_content_cache_activated == 0) - 1)'
    )
    return gated(state, APPLE_FRESH)


TM_BACKUP_AGE_MAPPINGS = [
    {
        "type": "value",
        "options": {
            "-1": {"color": "gray", "index": 0, "text": "NOT CONFIGURED"}
        },
    }
]


CACHE_RATIO_STATE = (
    "unbound:cache_hit_ratio_15m or on() "
    "(((((sum(rate(unbound_cache_hits_total[15m])) + "
    "sum(rate(unbound_cache_misses_total[15m]))) <= 1) * 0) - 1) "
    'and on() (up{job="unbound"} == 1))'
)

CACHE_RATIO_MAPPINGS = [
    {
        "type": "value",
        "options": {
            "-1": {"color": "gray", "index": 0, "text": "LOW TRAFFIC"}
        },
    }
]


def overview_dashboard():
    status = boolean_mappings()
    panels = [
        row(1, "Current health", 0),
        stat(2, "Collection", "monitoring:collection_path_healthy", grid(0, 1, 6, 3),
             "HEALTHY requires all eight expected scrape targets and every core collection path to be up. UNKNOWN means Prometheus cannot provide the canonical result.", mappings=status, threshold_config=thresholds((1, "green"), base="red"), links=[panel_link("Prometheus diagnostics", "prometheus-monitoring")]),
        stat(3, "Alerts", "monitoring:active_alerts", grid(6, 1, 6, 3),
             "Current firing Prometheus alerts. Zero is healthy only when collection coverage is also healthy.", threshold_config=thresholds((1, "red"), base="green"), decimals=0, no_value="UNKNOWN", links=[{"title": "Open Prometheus alerts", "url": "http://192.168.0.100:9090/alerts", "targetBlank": True}]),
        stat(4, "Unbound", "unbound_up", grid(12, 1, 6, 3),
             "Whether unbound_exporter can read the Unbound control socket. This is daemon/control health, not proof that clients receive answers.", mappings=boolean_mappings("CONTROL OK", "CONTROL FAILED"), threshold_config=thresholds((1, "green"), base="red"), links=[panel_link("DNS drill-down", "unbound-dns")]),
        stat(5, "DNS probe", 'probe_success{job="blackbox_resolver"}', grid(18, 1, 6, 3),
             "Synthetic query to the LAN resolver address. It proves that Unbound answers a repeated hot name; it does not prove fresh recursion.", mappings=boolean_mappings("ANSWERING", "NO ANSWER"), threshold_config=thresholds((1, "green"), base="red"), links=[panel_link("DNS drill-down", "unbound-dns")]),
        stat(6, "Upstream (10m)", "dns:upstream_best_success_ratio_10m", grid(0, 4, 6, 3),
             "Success ratio of the healthier public DNS probe path. Red matches the alert threshold below 95%; amber means partial loss; green means effectively all probes succeeded.", unit="percentunit", threshold_config=thresholds((0.95, "orange"), (0.999, "green"), base="red"), decimals=1, minimum=0, maximum=1, links=[panel_link("DNS drill-down", "unbound-dns")]),
        stat(7, "RPZ", 'probe_success{job="blackbox_rpz_canary"}', grid(6, 4, 6, 3),
             "The canary is healthy only when the configured blocked domain returns NXDOMAIN through Unbound.", mappings=boolean_mappings("BLOCKING", "NOT BLOCKING"), threshold_config=thresholds((1, "green"), base="red"), links=[panel_link("DNS drill-down", "unbound-dns")]),
        stat(8, "Host metrics", 'up{job="node"}', grid(12, 4, 6, 3),
             "Prometheus can scrape node_exporter. This is a local observation and is not an independent witness that the Mac or LAN is externally reachable.", mappings=boolean_mappings("METRICS UP", "METRICS DOWN"), threshold_config=thresholds((1, "green"), base="red"), links=[panel_link("Host drill-down", "host-services")]),
        stat(9, "Notifier", "monitoring:notifier_local_path_healthy", grid(18, 4, 6, 3),
             "Fresh notifier run, successful Prometheus poll, and fewer than two failed ntfy deliveries in 30 minutes. This cannot prove receipt on the phone.", mappings=boolean_mappings("LOCAL PATH OK", "FAILED / STALE"), threshold_config=thresholds((1, "green"), base="red"), links=[panel_link("Host drill-down", "host-services")]),
        stat(10, "Backup age", time_machine_backup_age_state(), grid(0, 7, 6, 3),
             "Age of the latest completed backup from the privacy-safe Time Machine collector. It uses tmutil when permitted and completed SnapshotDates obtained through the macOS defaults preference service otherwise. NOT CONFIGURED is explicit; missing or stale collection remains UNKNOWN. Warning at six hours and critical at 24 hours are engineering limits based on the observed hourly schedule.", unit="s", mappings=TM_BACKUP_AGE_MAPPINGS, threshold_config=thresholds((21600, "orange"), (86400, "red"), base="green"), decimals=0, no_value="UNKNOWN / STALE", links=[panel_link("Backup drill-down", "host-services")]),
        stat(11, "Grafana", 'probe_success{job="blackbox_grafana"}', grid(6, 7, 6, 3),
             "HTTP /api/health returned 200 and reported database ok through blackbox_exporter.", mappings=boolean_mappings("HTTP + DB OK", "FAILED"), threshold_config=thresholds((1, "green"), base="red"), links=[panel_link("Prometheus diagnostics", "prometheus-monitoring")]),
        text_panel(12, "Independent outage witness", "### NOT DEPLOYED\nNo observer exists outside the Mac mini and residential network. A host, LAN, power, or Prometheus loss can remove DNS, dashboards, rule evaluation, and notifications together. Deploy a small external DNS/dead-man witness before treating green local panels as proof of external reachability.", grid(12, 7, 12, 3), "Static limitation, intentionally prominent rather than represented as a green metric."),
        table_panel(13, "Active or pending alerts", 'ALERTS{alertstate=~"pending|firing"}', grid(0, 10, 15, 7),
                    "Every current alert labelset, retaining affected job, instance, mountpoint, and other rule labels when present. Empty means no alert series; confirm Collection coverage before interpreting that as healthy.", links=[{"title": "Open Prometheus alerts", "url": "http://192.168.0.100:9090/alerts", "targetBlank": True}]),
        text_panel(14, "What to inspect next", "- **Collection coverage failed:** open Prometheus Diagnostics and identify the missing/down job.\n- **Control failed, probe answering:** inspect unbound_exporter and the control socket.\n- **Control OK, probe failed:** inspect Unbound listeners, firewall, and blackbox exporter.\n- **Both upstream paths degraded:** inspect WAN/UDP 53 before cache behavior.\n- **RPZ failed:** inspect the blocklist breadcrumb, source floor, and Unbound RPZ load.\n- **Notification failed:** inspect the notifier log and local Prometheus alerts; phone receipt remains unverified.", grid(15, 10, 9, 7), "Incident routing based on independently observed paths."),
        state_timeline(15, "Health history", [
            ("monitoring:target_inventory_ok", "Expected target inventory"),
            ('up{job="node"}', "node_exporter"),
            ('up{job="unbound"}', "unbound_exporter scrape"),
            ("unbound_up", "Unbound control"),
            ('probe_success{job="blackbox_resolver"}', "LAN resolver probe"),
            ('probe_success{job="blackbox_rpz_canary"}', "RPZ canary"),
            ('probe_success{job="blackbox_upstream"}', "Upstream · {{instance}}"),
            ('probe_success{job="blackbox_grafana"}', "Grafana HTTP + DB"),
            ("monitoring:notifier_local_path_healthy", "Local notifier path"),
        ], grid(0, 17, 24, 9), "Discrete states retain gaps: gray/UNKNOWN is not converted to green or zero. Correlated row changes help locate whether a failure began in the service, exporter, probe path, or monitoring stack."),
    ]
    return make_dashboard(
        "operations-overview",
        "Operational Overview",
        "Incident-first view: current service and monitoring health, active alerts, correlated failure history, and the shortest path to the next diagnostic view. Local green state does not prove external reachability.",
        panels,
        tags=("operations", "incident-response"),
        version=1,
    )


def dns_dashboard():
    client_variable = {
        "name": "client",
        "label": "RPZ-active client",
        "type": "query",
        "datasource": DATASOURCE,
        "definition": "label_values(unbound_client_rpz_blocks_5m, client)",
        "query": {"query": "label_values(unbound_client_rpz_blocks_5m, client)", "refId": "StandardVariableQuery"},
        "refresh": 2,
        "multi": True,
        "includeAll": True,
        "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
        "options": [],
        "sort": 3,
    }
    panels = [
        row(1, "Resolver and path health", 0),
        stat(2, "Exporter", 'up{job="unbound"}', grid(0, 1, 4, 4), "Prometheus can scrape unbound_exporter. A missing result is UNKNOWN, not down or healthy.", mappings=boolean_mappings("SCRAPING", "DOWN"), threshold_config=thresholds((1, "green"), base="red")),
        stat(3, "Unbound", "unbound_up", grid(4, 1, 4, 4), "unbound_exporter can read the Unbound control socket. This does not prove client reachability.", mappings=boolean_mappings("CONTROL OK", "CONTROL FAILED"), threshold_config=thresholds((1, "green"), base="red")),
        stat(4, "LAN probe", 'probe_success{job="blackbox_resolver"}', grid(8, 1, 4, 4), "Unbound answered the repeated LAN synthetic query. Cache, prefetch, and serve-expired mean this is not proof of fresh recursion.", mappings=boolean_mappings("ANSWERING", "NO ANSWER"), threshold_config=thresholds((1, "green"), base="red")),
        stat(5, "Upstream (10m)", "dns:upstream_best_success_ratio_10m", grid(12, 1, 4, 4), "The healthier of the two public-recursive UDP/53 probes. These are reachability witnesses, not Unbound's actual root/authoritative destinations.", unit="percentunit", threshold_config=thresholds((0.95, "orange"), (0.999, "green"), base="red"), decimals=1, minimum=0, maximum=1),
        stat(6, "RPZ", 'probe_success{job="blackbox_rpz_canary"}', grid(16, 1, 4, 4), "Healthy only when the documented canary name returns NXDOMAIN through Unbound.", mappings=boolean_mappings("BLOCKING", "NOT BLOCKING"), threshold_config=thresholds((1, "green"), base="red")),
        stat(7, "Requestlist", "sum(unbound_request_list_current_all)", grid(20, 1, 4, 4), "Current queued requests across worker threads. Red above 100 matches UnboundRequestListJammed; this is a primary upstream-failure tripwire.", unit="short", threshold_config=thresholds((101, "red"), base="blue"), decimals=0),
        state_timeline(8, "DNS path history", [
            ('up{job="unbound"}', "unbound_exporter"),
            ("unbound_up", "Unbound control"),
            ('probe_success{job="blackbox_resolver"}', "LAN resolver"),
            ('probe_success{job="blackbox_rpz_canary"}', "RPZ canary"),
            ('probe_success{job="blackbox_upstream"}', "Upstream · {{instance}}"),
        ], grid(0, 5, 24, 8), "Compare independent paths. A blackbox exporter failure makes every probe row UNKNOWN; use Prometheus Diagnostics to distinguish exporter loss from simultaneous target failures."),
        row(9, "Traffic and latency", 13),
        timeseries(10, "Query and RPZ action rates", [("sum(rate(unbound_queries_total[$__rate_interval]))", "queries/s"), ("sum(rate(unbound_rpz_action_count[$__rate_interval]))", "RPZ actions/s")], grid(0, 14, 12, 8), "Counter rates use Grafana's scrape-aware interval. Global RPZ actions can include internal Unbound activity and are not presented as client block totals.", unit="ops"),
        timeseries(11, "Answers by response code", [('sum by (rcode) (rate(unbound_answer_rcodes_total[$__rate_interval]))', "{{rcode}}")], grid(12, 14, 12, 8), "Per-second answer rate by DNS response code using Grafana's scrape-aware rate interval. SERVFAIL remains a rate/trend, not an arbitrary red count.", unit="ops", legend="table"),
        timeseries(13, "Answer latency distribution", [
            ("histogram_quantile(0.50, sum by (le) (rate(unbound_response_time_seconds_bucket[$__rate_interval])))", "p50"),
            ("histogram_quantile(0.95, sum by (le) (rate(unbound_response_time_seconds_bucket[$__rate_interval])))", "p95"),
        ], grid(0, 22, 24, 8), "Histogram quantiles are valid for this singleton exporter: bucket rates use Grafana's scrape-aware interval and are summed only by le before histogram_quantile.", unit="s"),
        row(14, "Cache behavior and pressure", 30),
        stat(15, "Cache hit ratio (15m, under load)", CACHE_RATIO_STATE, grid(0, 31, 6, 5), "Canonical ratio used by the alert. LOW TRAFFIC is an explicit neutral state at one lookup/s or less while the exporter is healthy; missing input remains UNKNOWN. Red below 50% matches UnboundCacheHitRateLow before its 30-minute hold.", unit="percentunit", mappings=CACHE_RATIO_MAPPINGS, threshold_config=thresholds((0.5, "green"), base="red"), decimals=1, minimum=-1, maximum=1, no_value="UNKNOWN"),
        timeseries(16, "Cache hit and miss rates", [('sum(rate(unbound_cache_hits_total[$__rate_interval]))', "hits/s"), ('sum(rate(unbound_cache_misses_total[$__rate_interval]))', "misses/s")], grid(6, 31, 9, 8), "Counter rates summed across Unbound worker threads before comparison, using Grafana's scrape-aware rate interval.", unit="ops"),
        timeseries(17, "Requestlist and cache edge-event rates", [('sum(rate(unbound_request_list_exceeded_total[$__rate_interval]))', "requestlist exceeded/s"), ('sum(rate(unbound_request_list_overwritten_total[$__rate_interval]))', "requestlist overwritten/s"), ('sum(rate(unbound_expired_total[$__rate_interval]))', "served-expired/s")], grid(15, 31, 9, 8), "Comparable counter event rates using Grafana's scrape-aware interval. Current requestlist depth is shown separately in the health row.", unit="ops"),
        row(20, "RPZ client attribution", 39),
        stat(21, "Client collector freshness", 'time() - unbound_job_last_success_timestamp_seconds{task="clientstats"}', grid(0, 40, 6, 5), "Collector runs every 120 seconds. Amber at 10 minutes; red at 15 minutes matches ClientStatsStale. This freshness panel disambiguates an empty client ranking.", unit="s", threshold_config=thresholds((600, "orange"), (900, "red"), base="green"), decimals=0),
        bar_gauge(22, "Top RPZ-active clients (rolling 5m)", [(gated('topk(10, unbound_client_rpz_blocks_5m{client=~"${client:regex}"})', CLIENT_FRESH), "{{client}}")], grid(6, 40, 9, 8), "Sampled top-25 log-derived gauges over a rolling five-minute window, filtered by the selected client variable. This is not a complete total and clients disappear after inactivity. Use the adjacent freshness state to distinguish no activity from missing collection.", unit="short", minimum=0, no_value="NO CURRENT SERIES — CHECK FRESHNESS"),
        bar_gauge(23, "Age of latest sampled RPZ action", [(gated('sort(time() - unbound_client_last_rpz_block_seconds{client=~"${client:regex}"})', CLIENT_FRESH), "{{client}}")], grid(15, 40, 9, 8), "Seconds since the most recent sampled RPZ action for clients still present in the rolling five-minute collector population. Use the adjacent freshness state to distinguish no activity from missing collection.", unit="s", minimum=0, no_value="NO CURRENT SERIES — CHECK FRESHNESS"),
    ]
    return make_dashboard(
        "unbound-dns",
        "DNS and Unbound",
        "Resolver drill-down: independent health paths, traffic, response behavior, cache pressure, and explicitly sampled client/RPZ attribution.",
        panels,
        tags=("unbound", "dns", "drill-down"),
        templating=[client_variable],
        version=13,
    )


def host_dashboard():
    panels = [
        row(1, "Current host, backup, and automation state", 0),
        stat(2, "Host exporter", 'up{job="node"}', grid(0, 1, 4, 4), "Scrape health for the shared host and textfile-exporter path. If this fails, all host and custom textfile metrics disappear together.", mappings=boolean_mappings("SCRAPING", "DOWN"), threshold_config=thresholds((1, "green"), base="red")),
        stat(3, "Textfiles", "monitoring:textfile_path_healthy", grid(4, 1, 4, 4), "HEALTHY requires node_exporter up and zero textfile parse errors. Missing textfile families can still require their dedicated freshness/absence checks.", mappings=boolean_mappings("PARSE OK", "FAILED"), threshold_config=thresholds((1, "green"), base="red")),
        stat(4, "Uptime", "time() - node_boot_time_seconds", grid(8, 1, 4, 4), "Age of the current macOS boot. Neutral context for correlating an incident with a host reboot; no severity is implied.", unit="s", threshold_config=thresholds(base="blue"), decimals=0),
        stat(5, "TM configured", gated("time_machine_destination_configured", TM_FRESH), grid(12, 1, 4, 4), "Whether tmutil reports a configured destination, shown only while the collector file is fresh. Configuration is neutral context, not verified backup health.", mappings=[{"type": "value", "options": {"0": {"color": "gray", "index": 1, "text": "NOT CONFIGURED"}, "1": {"color": "blue", "index": 0, "text": "CONFIGURED"}}}], threshold_config=thresholds((1, "blue"), base="gray")),
        stat(6, "TM collector", gated("time_machine_exporter_collection_success", TM_FRESH), grid(16, 1, 4, 4), "Whether destination, running state, and latest-completed-backup state were collected without error. The timestamp uses tmutil when permitted and completed SnapshotDates obtained through the macOS defaults preference service otherwise. It does not state that a backup is recent.", mappings=boolean_mappings("COLLECTED", "FAILED"), threshold_config=thresholds((1, "green"), base="red")),
        stat(7, "Backup age", time_machine_backup_age_state(), grid(20, 1, 4, 4), "Age of the latest completed backup from the privacy-safe collector. NOT CONFIGURED is explicit; missing or stale collection remains UNKNOWN. Six-hour warning and 24-hour failure bounds are based on the observed hourly schedule.", unit="s", mappings=TM_BACKUP_AGE_MAPPINGS, threshold_config=thresholds((21600, "orange"), (86400, "red"), base="green"), decimals=0, no_value="UNKNOWN / STALE"),
        row(8, "Resource pressure", 5),
        timeseries(9, "CPU busy", [('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval]))', "busy")], grid(0, 6, 12, 8), "Complement of idle time using Grafana's scrape-aware rate interval. No arbitrary severity threshold is applied.", unit="percentunit", minimum=0, maximum=1),
        timeseries(10, "Memory categories", [("node_memory_active_bytes", "active"), ("node_memory_wired_bytes", "wired"), ("node_memory_compressed_bytes", "compressed"), ("node_memory_free_bytes", "free")], grid(12, 6, 12, 8), "Raw macOS memory gauges. These categories are diagnostic and are not presented as the operating system's memory-pressure signal.", unit="bytes", legend="table"),
        timeseries(11, "Ethernet throughput (en0)", [('rate(node_network_receive_bytes_total{device="en0"}[$__rate_interval])', "receive"), ('rate(node_network_transmit_bytes_total{device="en0"}[$__rate_interval])', "transmit")], grid(0, 14, 8, 8), "Byte rates on the documented Ethernet interface using Grafana's scrape-aware rate interval.", unit="Bps"),
        timeseries(12, "Root filesystem trend", [('1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}', "used fraction")], grid(8, 14, 8, 8), "Root used fraction over time. Red at 90% matches DiskAlmostFull for root; use the current-filesystems ranking when that alert identifies another active mount.", unit="percentunit", minimum=0, maximum=1, threshold_config=thresholds((0.9, "red"), base="blue")),
        bar_gauge(13, "Current APFS/HFS filesystems", [('sort_desc(max by (mountpoint) (1 - node_filesystem_avail_bytes{fstype=~"apfs|hfs"} / node_filesystem_size_bytes{fstype=~"apfs|hfs"}))', "{{mountpoint}}")], grid(16, 14, 8, 8), "Instant current utilization by active APFS/HFS mountpoint, matching DiskAlmostFull's population without retaining transient historical mounts.", unit="percentunit", threshold_config=thresholds((0.9, "red"), base="blue"), minimum=0, maximum=1, no_value="UNKNOWN"),
        row(14, "Backups and scheduled automation", 22),
        timeseries(15, "Latest backup age", [(gated("time() - time_machine_latest_backup_timestamp_seconds", TM_FRESH), "age")], grid(0, 23, 8, 8), "Sawtooth age since the latest completed backup reported by the privacy-safe collector. Gaps remain gaps instead of becoming zero; configuration and collection state are explicit above.", unit="s", threshold_config=thresholds((21600, "orange"), (86400, "red"), base="blue")),
        stat(16, "Backup running", time_machine_running_state(), grid(8, 23, 4, 8), "Current tmutil running state. NOT CONFIGURED is distinct from ordinary IDLE; RUNNING is informational, not an alert.", mappings=[{"type": "value", "options": {"-1": {"color": "gray", "index": 0, "text": "NOT CONFIGURED"}, "0": {"color": "gray", "index": 1, "text": "IDLE"}, "1": {"color": "blue", "index": 2, "text": "RUNNING"}}}], threshold_config=thresholds((1, "blue"), base="gray"), minimum=-1, no_value="UNKNOWN / STALE"),
        stat(17, "Blocklist size / source floor", gated("unbound_blocklist_lines / unbound_blocklist_minimum_lines", 'time() - unbound_job_last_success_timestamp_seconds{task="blocklist"} < 93600'), grid(12, 23, 4, 8), "Ratio of deployed lines to the selected source's own validated minimum. Red below 100% matches BlocklistSuspiciouslySmall; no universal 100k assumption remains.", unit="percentunit", threshold_config=thresholds((1, "green"), base="red"), decimals=1, minimum=0, no_value="STALE / UNKNOWN"),
        state_timeline(18, "Scheduled producer freshness", [
            ('time() - unbound_job_last_success_timestamp_seconds{task="blocklist"} < bool 93600', "Blocklist · daily"),
            ('time() - unbound_job_last_success_timestamp_seconds{task="warmlist"} < bool 93600', "Warm list · daily"),
            ('time() - unbound_job_last_success_timestamp_seconds{task="roothints"} < bool 2937600', "Root hints · monthly"),
            ('unbound_job_last_success_timestamp_seconds{task="warmboot"} >= bool scalar(node_boot_time_seconds)', "Warm cache · boot"),
            ('time() - node_textfile_mtime_seconds{file=~".*/apple-content-cache[.]prom"} < bool 900', "Content Cache · 5m"),
            ('time() - unbound_job_last_success_timestamp_seconds{task="clientstats"} < bool 900', "Client RPZ · 2m"),
            ('time() - unbound_job_last_success_timestamp_seconds{task="alertnotify"} < bool 600', "Notifier · 2m"),
            ('time() - node_textfile_mtime_seconds{file=~".*/time-machine[.]prom"} < bool 3600', "Time Machine · 15m"),
        ], grid(16, 23, 8, 11), "Each row uses its producer cadence and matching alert-age boundary. Missing metrics remain UNKNOWN. A simultaneous loss across rows points first to node_exporter/textfile collection."),
        timeseries(19, "Notifier delivery and state errors", [('increase(unbound_ntfy_delivery_failures_total[30m])', "delivery failures / 30m"), ('increase(unbound_ntfy_state_errors_total[30m])', "state errors / 30m")], grid(0, 31, 16, 8), "Persisted counter increases shown neutrally because the two series have different alert semantics. NtfyDeliveryFailures fires at two delivery failures in 30 minutes; even a healthy local path cannot prove phone receipt.", unit="short"),
    ]
    return make_dashboard(
        "host-services",
        "Host, Services, and Backups",
        "Shared host/resource failure domains, Time Machine recency, network trends, and freshness of every scheduled metric producer.",
        panels,
        tags=("host", "services", "backups", "drill-down"),
        version=1,
    )


def apple_dashboard():
    fresh = APPLE_FRESH
    panels = [
        row(1, "Current service and data trust", 0),
        stat(2, "Cache active", apple_enabled_state("apple_content_cache_active"), grid(0, 1, 5, 4), "Whether AssetCacheManagerUtil reports the enabled service active, shown only while the textfile is fresh. Intentionally disabled is neutral and distinct from enabled-but-inactive.", mappings=[{"type": "value", "options": {"-1": {"color": "gray", "index": 0, "text": "DISABLED"}, "0": {"color": "red", "index": 1, "text": "INACTIVE"}, "1": {"color": "green", "index": 2, "text": "ACTIVE"}}}], threshold_config=thresholds((1, "green"), base="red"), minimum=-1),
        stat(3, "Cache status", apple_enabled_state("apple_content_cache_status_ok"), grid(5, 1, 5, 4), "Whether AssetCacheManagerUtil reported CacheStatus OK while enabled. Disabled is neutral; missing/stale collection remains UNKNOWN.", mappings=[{"type": "value", "options": {"-1": {"color": "gray", "index": 0, "text": "DISABLED"}, "0": {"color": "red", "index": 1, "text": "NOT OK"}, "1": {"color": "green", "index": 2, "text": "OK"}}}], threshold_config=thresholds((1, "green"), base="red"), minimum=-1),
        stat(4, "Traffic data", apple_enabled_state("apple_content_cache_traffic_metrics_available"), grid(10, 1, 5, 4), "Whether Apple's Metrics.db was readable and its traffic schema recognized during the latest fresh collection. It is not applicable while Content Cache is disabled.", mappings=[{"type": "value", "options": {"-1": {"color": "gray", "index": 0, "text": "N/A — DISABLED"}, "0": {"color": "red", "index": 1, "text": "UNAVAILABLE"}, "1": {"color": "green", "index": 2, "text": "AVAILABLE"}}}], threshold_config=thresholds((1, "green"), base="red"), minimum=-1),
        stat(5, "Since-reset data", apple_enabled_state("apple_content_cache_since_reset_traffic_complete"), grid(15, 1, 5, 4), "Complete only while the reset epoch lies inside Apple's documented 30-day Metrics.db retention used by this collector. It is not applicable while disabled; last-24h metrics remain independent.", mappings=[{"type": "value", "options": {"-1": {"color": "gray", "index": 0, "text": "N/A — DISABLED"}, "0": {"color": "orange", "index": 1, "text": "INCOMPLETE"}, "1": {"color": "green", "index": 2, "text": "COMPLETE"}}}], threshold_config=thresholds((1, "green"), base="orange"), minimum=-1),
        stat(6, "Exporter age", 'time() - node_textfile_mtime_seconds{file=~".*/apple-content-cache[.]prom"}', grid(20, 1, 4, 4), "Collector runs every five minutes. Amber at 10 minutes; red at 15 minutes matches ContentCacheExporterStale.", unit="s", threshold_config=thresholds((600, "orange"), (900, "red"), base="green"), decimals=0),
        row(7, "Capacity", 5),
        timeseries(9, "Cache capacity", [(gated("apple_content_cache_actual_used_bytes", fresh), "actual used"), (gated("apple_content_cache_free_bytes", fresh), "free"), (gated("apple_content_cache_limit_bytes", fresh), "configured limit")], grid(0, 6, 14, 8), "Freshness-gated capacity gauges. A collector outage creates a gap instead of a flat, stale line.", unit="bytes", legend="table"),
        bar_gauge(10, "Current cache categories", [(gated("apple_content_cache_detail_bytes", fresh), "{{category}}")], grid(14, 6, 10, 8), "Instant category ranking. Categories missing from the latest fresh source disappear rather than persisting through a range reducer.", unit="bytes", minimum=0, no_value="NO FRESH CATEGORY DATA"),
        row(11, "Traffic composition (rolling 24h)", 14),
        stat(12, "All client traffic", gated("apple_content_cache_total_to_client_last_24h_bytes", fresh), grid(0, 15, 6, 5), "All bytes Apple recorded as served to clients from local cache, origin, parent, or peer in its rolling 24-hour window.", unit="bytes", threshold_config=thresholds(base="blue"), no_value="NO FRESH DATA"),
        stat(13, "Local cache share", gated("apple_content_cache:local_hit_ratio_24h", fresh), grid(6, 15, 6, 5), "Local-cache bytes divided by all client bytes. Parent and peer traffic remain in the denominator. If no ratio is present, the adjacent total and Traffic metrics state distinguish zero traffic from unavailable/stale collection.", unit="percentunit", decimals=1, minimum=0, maximum=1, threshold_config=thresholds(base="blue"), no_value="NO RATIO — CHECK TRAFFIC STATE"),
        stat(14, "Origin share", gated("apple_content_cache:origin_fraction_24h", fresh), grid(12, 15, 6, 5), "Origin-to-client bytes divided by all client bytes. If no ratio is present, the adjacent total and Traffic metrics state distinguish zero traffic from unavailable/stale collection.", unit="percentunit", decimals=1, minimum=0, maximum=1, threshold_config=thresholds(base="blue"), no_value="NO RATIO — CHECK TRAFFIC STATE"),
        stat(15, "Other-source share", gated("apple_content_cache:other_source_fraction_24h", fresh), grid(18, 15, 6, 5), "Parent- and peer-sourced bytes inferred as total minus local-cache minus origin, divided by all client bytes. Use the adjacent traffic state to classify a missing ratio.", unit="percentunit", decimals=1, minimum=0, maximum=1, threshold_config=thresholds(base="blue"), no_value="NO RATIO — CHECK TRAFFIC STATE"),
        timeseries(16, "Traffic bytes by source", [(gated("apple_content_cache_cache_to_client_last_24h_bytes", fresh), "local cache"), (gated("apple_content_cache_origin_to_client_last_24h_bytes", fresh), "origin"), (gated("clamp_min(apple_content_cache_total_to_client_last_24h_bytes - apple_content_cache_cache_to_client_last_24h_bytes - apple_content_cache_origin_to_client_last_24h_bytes, 0)", fresh), "parent + peer")], grid(0, 20, 24, 8), "Additive rolling-24h source components. Stacking is valid because the three series form the displayed total.", unit="bytes", stacking="normal", legend="table"),
    ]
    return make_dashboard(
        "apple-content-cache",
        "Apple Content Cache",
        "Freshness-gated service state, capacity, and source-complete traffic composition. Zero traffic, unavailable Metrics.db data, and incomplete since-reset history remain distinct.",
        panels,
        refresh="5m",
        start="now-24h",
        tags=("apple", "content-cache", "drill-down"),
        version=8,
    )


def prometheus_dashboard():
    panels = [
        row(1, "Monitoring system state", 0),
        stat(2, "Target inventory", "monitoring:target_inventory_ok", grid(0, 1, 4, 4), "Each provisioned job must have its exact expected target count, with eight targets total. This catches missing, duplicate, and substituted jobs that sum(1-up) cannot see.", mappings=boolean_mappings("EXACT", "MISMATCH"), threshold_config=thresholds((1, "green"), base="red")),
        stat(3, "Targets down", "monitoring:targets_down", grid(4, 1, 4, 4), "Count of represented targets with up=0. Interpret together with Expected target inventory.", threshold_config=thresholds((1, "red"), base="green"), decimals=0),
        stat(4, "Alerts", "monitoring:active_alerts", grid(8, 1, 4, 4), "Count of firing Prometheus alerts. Zero is meaningful only while Prometheus and target inventory are observable.", threshold_config=thresholds((1, "red"), base="green"), decimals=0),
        stat(5, "Config reload", "prometheus_config_last_reload_successful", grid(12, 1, 4, 4), "Prometheus reports whether its most recent configuration reload succeeded.", mappings=boolean_mappings("SUCCESS", "FAILED"), threshold_config=thresholds((1, "green"), base="red")),
        stat(6, "Rule failures (5m)", "sum(increase(prometheus_rule_evaluation_failures_total[5m])) <= bool 0", grid(16, 1, 4, 4), "Boolean state aligned with PrometheusRuleEvaluationFailures: any positive counter increase in the last five minutes is a failure, including extrapolated fractional increases.", mappings=boolean_mappings("NO FAILURES", "FAILURES"), threshold_config=thresholds((1, "green"), base="red"), decimals=0),
        stat(7, "Uptime", 'time() - process_start_time_seconds{job="prometheus"}', grid(20, 1, 4, 4), "Age of the current Prometheus process. Neutral context; a restart is not itself healthy or unhealthy.", unit="s", threshold_config=thresholds(base="blue"), decimals=0),
        state_timeline(8, "Scrape target history", [('up', "{{job}} · {{instance}}")], grid(0, 5, 24, 10), "Every represented target. Missing expected targets disappear from this list, so the inventory card above is the required companion. Blackbox jobs reflect exporter scrape health, not probe_success."),
        row(9, "Scrape and ingestion diagnostics", 15),
        timeseries(10, "Scrape duration", [('scrape_duration_seconds', "{{job}} · {{instance}}")], grid(0, 16, 12, 8), "End-to-end scrape duration by target. Compare spikes with target health and sample counts.", unit="s", legend="table"),
        timeseries(11, "Samples ingested per scrape", [('scrape_samples_post_metric_relabeling', "{{job}} · {{instance}}")], grid(12, 16, 12, 8), "Post-relabel samples per scrape. The former direct Grafana /metrics job was replaced by a small /api/health probe because its unused metrics dominated ingestion.", unit="short", legend="table"),
        timeseries(13, "Persisted TSDB components", [("prometheus_tsdb_storage_blocks_bytes", "persisted blocks"), ("prometheus_tsdb_head_chunks_storage_size_bytes", "head chunks directory")], grid(0, 24, 24, 8), "Two named TSDB components, not a claim of complete on-disk footprint. Filesystem usage remains the authoritative disk-capacity signal.", unit="bytes", legend="table"),
        row(14, "Rule and exporter diagnostics", 32),
        timeseries(15, "Rule-group evaluation duration", [('prometheus_rule_group_last_duration_seconds', "{{rule_group}}")], grid(0, 33, 24, 8), "Last evaluation duration per rule group, with direct labels rather than an opaque rename transformation.", unit="s", legend="table"),
    ]
    return make_dashboard(
        "prometheus-monitoring",
        "Prometheus Diagnostics",
        "Monitoring-the-monitoring drill-down: expected inventory, scrape paths, alerts, rule evaluation, ingestion cost, and TSDB internals.",
        panels,
        tags=("prometheus", "monitoring", "diagnostics"),
        version=4,
    )


def build_dashboards():
    values = [
        overview_dashboard(),
        dns_dashboard(),
        host_dashboard(),
        apple_dashboard(),
        prometheus_dashboard(),
    ]
    return {f"{value['uid']}.json": value for value in values}


def render(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write all generated dashboard JSON")
    mode.add_argument("--check", action="store_true", help="fail when generated JSON differs from disk")
    parser.add_argument(
        "--prune-retired",
        action="store_true",
        help="with --write, remove only the four explicitly retired inherited JSON files",
    )
    args = parser.parse_args()

    if args.prune_retired and not args.write:
        parser.error("--prune-retired requires --write")

    generated = build_dashboards()
    expected_names = set(generated)
    existing_names = {path.name for path in DASHBOARD_DIR.glob("*.json")}

    if args.write:
        DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
        stale_names = existing_names - expected_names
        unknown_names = stale_names - RETIRED_DASHBOARD_FILES
        if unknown_names:
            print(
                f"refusing to overwrite unexpected dashboard files: {sorted(unknown_names)}",
                file=sys.stderr,
            )
            return 1
        if stale_names and not args.prune_retired:
            print(
                "retired inherited dashboards are still present; review them, then rerun "
                "with --write --prune-retired",
                file=sys.stderr,
            )
            return 1
        for stale_name in sorted(stale_names):
            (DASHBOARD_DIR / stale_name).unlink()
        for name, value in generated.items():
            (DASHBOARD_DIR / name).write_text(render(value), encoding="utf-8", newline="\n")
        print(f"wrote {len(generated)} dashboards")
        return 0

    failures = []
    if existing_names != expected_names:
        failures.append(
            f"dashboard filenames differ: expected {sorted(expected_names)}, found {sorted(existing_names)}"
        )
    for name, value in generated.items():
        path = DASHBOARD_DIR / name
        if not path.exists() or path.read_text(encoding="utf-8") != render(value):
            failures.append(f"generated dashboard differs: {name}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"dashboard generation check: OK ({len(generated)} dashboards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
