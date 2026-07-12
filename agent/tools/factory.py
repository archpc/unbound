"""Build the complete, explicit read-only tool registry."""

from __future__ import annotations

from agent.gateway.tool_registry import ToolRegistry, ToolSpec
from agent.tools import deployment_drift, dns_probe, prometheus_query, recent_logs
from agent.tools import repo_docs, service_status, unbound_stats


EMPTY_ARGUMENTS = {"type": "object", "properties": {}, "additionalProperties": False}


def _object_result(required, properties):
    return {"type": "object", "required": required, "properties": properties,
            "additionalProperties": False}


def build_registry(config, metrics, handler_overrides=None):
    """Build the registry, optionally replacing handlers for tests/evaluation only."""
    overrides = dict(handler_overrides or {})

    def handler(name, default):
        return overrides.get(name, default)

    specs = [
        ToolSpec(
            "unbound_stats", "Return only approved aggregate Unbound stats; never raw control output.",
            EMPTY_ARGUMENTS,
            _object_result(
                ["available", "fields", "response_codes", "rpz_actions", "limitation"],
                {"available": {"type": "boolean"}, "fields": {"type": "object"},
                 "response_codes": {"type": "object"}, "rpz_actions": {"type": "object"},
                 "limitation": {"type": "string"}},
            ), min(config.per_tool_timeout_seconds, 10), config.maximum_tool_output_bytes,
            ("approved_fields_only",), handler("unbound_stats", unbound_stats.build_tool(config)),
        ),
        ToolSpec(
            "prometheus_query", "Run one named, precompiled safe Prometheus query; raw PromQL is not accepted.",
            {"type": "object", "required": ["query_id"], "additionalProperties": False,
             "properties": {"query_id": {"type": "string", "enum": sorted(prometheus_query.QUERIES)}}},
            _object_result(
                ["available", "query_id", "result_type", "samples", "limitation"],
                {"available": {"type": "boolean"}, "query_id": {"type": "string"},
                 "result_type": {"type": "string"}, "samples": {"type": "array", "maxItems": 32},
                 "limitation": {"type": "string"}},
            ), min(config.per_tool_timeout_seconds, 10), config.maximum_tool_output_bytes,
            ("drop_all_labels_except_job",), handler("prometheus_query", prometheus_query.build_tool(config)),
        ),
        ToolSpec(
            "service_status", "Return normalized launchd state for one configured service label.",
            {"type": "object", "required": ["service"], "additionalProperties": False,
             "properties": {"service": {"type": "string", "enum": sorted(config.approved_services)}}},
            _object_result(
                ["service", "label", "loaded", "state", "pid", "last_exit_code"],
                {"service": {"type": "string"}, "label": {"type": "string"},
                 "loaded": {"type": "boolean"}, "state": {"type": "string"},
                 "pid": {}, "last_exit_code": {}},
            ), min(config.per_tool_timeout_seconds, 10), config.maximum_tool_output_bytes,
            ("normalized_fields_only",), handler("service_status", service_status.build_tool(config)),
        ),
        ToolSpec(
            "deployment_drift", "Run the repository's canonical read-only deployment verifier and return structured counts.",
            EMPTY_ARGUMENTS,
            _object_result(
                ["available", "ok", "drift", "missing_required", "optional_missing", "unknown", "drifted_paths", "result"],
                {"available": {"type": "boolean"}, "ok": {"type": "integer"},
                 "drift": {"type": "integer"}, "missing_required": {"type": "integer"},
                 "optional_missing": {"type": "integer"}, "unknown": {"type": "integer"},
                 "drifted_paths": {"type": "array", "maxItems": 50}, "result": {"type": "string"}},
            ), min(config.per_tool_timeout_seconds, 30), config.maximum_tool_output_bytes,
            ("omit_hashes", "omit_raw_report"), handler("deployment_drift", deployment_drift.build_tool(config)),
        ),
        ToolSpec(
            "dns_probe", "Run fixed DNS canaries against 127.0.0.1; optional domains are syntax-validated.",
            {"type": "object", "additionalProperties": False,
             "properties": {"domain": {"type": "string", "minLength": 4, "maxLength": 253,
                                      "pattern": dns_probe.DOMAIN_RE.pattern}}},
            _object_result(
                ["resolver", "probes", "limitation"],
                {"resolver": {"type": "string"}, "probes": {"type": "array", "maxItems": 6},
                 "limitation": {"type": "string"}},
            ), min(config.per_tool_timeout_seconds, 30), config.maximum_tool_output_bytes,
            ("fixed_resolver", "fixed_probe_set"), handler("dns_probe", dns_probe.build_tool(config)),
        ),
        ToolSpec(
            "recent_logs", "Read a bounded tail from one configured log source with sensitive values redacted.",
            {"type": "object", "required": ["source"], "additionalProperties": False,
             "properties": {"source": {"type": "string", "enum": sorted(config.approved_log_sources)},
                            "lines": {"type": "integer", "minimum": 1, "maximum": 200}}},
            _object_result(
                ["source", "available", "untrusted_lines", "redacted"],
                {"source": {"type": "string"}, "available": {"type": "boolean"},
                 "untrusted_lines": {"type": "array", "maxItems": 200},
                 "redacted": {"type": "boolean"}},
            ), min(config.per_tool_timeout_seconds, 10), config.maximum_tool_output_bytes,
            ("redact_ipv4", "redact_secret_assignments"), handler("recent_logs", recent_logs.build_tool(config)),
        ),
        ToolSpec(
            "repo_docs", "Deterministically search only canonical repository documentation.",
            {"type": "object", "required": ["query"], "additionalProperties": False,
             "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 200}}},
            _object_result(
                ["query", "matches", "limitation"],
                {"query": {"type": "string"}, "matches": {"type": "array", "maxItems": 12},
                 "limitation": {"type": "string"}},
            ), min(config.per_tool_timeout_seconds, 10), config.maximum_tool_output_bytes,
            ("canonical_docs_only",), handler("repo_docs", repo_docs.build_tool(config)),
        ),
    ]
    return ToolRegistry(specs, metrics)
