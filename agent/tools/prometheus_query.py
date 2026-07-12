"""Named-query-only access to the loopback Prometheus HTTP API."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


QUERIES = {
    "unbound_health": "unbound_up",
    "unbound_exporter_health": 'up{job="unbound"}',
    "upstream_probe_health": 'probe_success{job="blackbox_upstream"}',
    "upstream_probe_duration": 'probe_duration_seconds{job="blackbox_upstream"}',
    "resolver_probe_health": 'probe_success{job="blackbox_resolver"}',
    "resolver_probe_duration": 'probe_duration_seconds{job="blackbox_resolver"}',
    "rpz_probe_health": 'probe_success{job="blackbox_rpz_canary"}',
    "cache_hit_rate_15m": "sum(rate(unbound_cache_hits_total[15m])) / (sum(rate(unbound_cache_hits_total[15m])) + sum(rate(unbound_cache_misses_total[15m])))",
    "cache_query_rate_15m": "sum(rate(unbound_cache_hits_total[15m])) + sum(rate(unbound_cache_misses_total[15m]))",
    "recursive_reply_rate_5m": "sum(rate(unbound_recursive_replies_total[5m]))",
    "request_list_current": "sum(unbound_request_list_current_all)",
    "unbound_uptime": "max(unbound_time_up_seconds_total)",
    "node_load": "node_load1",
    "node_memory_available": "node_memory_MemAvailable_bytes",
    "prometheus_uptime_start": 'process_start_time_seconds{job="prometheus"}',
}


def _normalize_result(body):
    if body.get("status") != "success" or not isinstance(body.get("data"), dict):
        raise ValueError("Prometheus response was not successful")
    data = body["data"]
    result_type = data.get("resultType")
    result = data.get("result")
    if result_type not in ("vector", "scalar") or not isinstance(result, list):
        raise ValueError("unsupported Prometheus result type")
    samples = []
    if result_type == "scalar":
        result = [{"metric": {}, "value": result}]
    for item in result[:32]:
        if not isinstance(item, dict) or not isinstance(item.get("value"), list) or len(item["value"]) != 2:
            continue
        metric = item.get("metric", {})
        safe_labels = {}
        if isinstance(metric, dict):
            for key in ("job",):
                value = metric.get(key)
                if isinstance(value, str) and len(value) <= 100:
                    safe_labels[key] = value
        timestamp, value = item["value"]
        samples.append({"timestamp": float(timestamp), "value": str(value)[:80], "labels": safe_labels})
    return result_type, samples


def build_tool(config, opener=urlopen):
    def prometheus_query(arguments):
        query_id = arguments["query_id"]
        expression = QUERIES[query_id]
        url = config.prometheus_endpoint + "/api/v1/query?" + urlencode({"query": expression})
        try:
            with opener(url, timeout=min(config.per_tool_timeout_seconds, 10)) as response:
                raw = response.read(524289)
            if len(raw) > 524288:
                raise ValueError("Prometheus response exceeded 512 KiB")
            result_type, samples = _normalize_result(json.loads(raw.decode("utf-8")))
            return {"available": True, "query_id": query_id, "result_type": result_type,
                    "samples": samples, "limitation": "Only the configured instantaneous named query was run."}
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return {"available": False, "query_id": query_id, "result_type": "vector", "samples": [],
                    "limitation": "The loopback Prometheus API was unavailable or returned malformed data."}
    return prometheus_query
