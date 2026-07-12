"""Approved-field parser for unbound-control stats_noreset."""

from __future__ import annotations

from agent.tools.common import run_fixed


APPROVED_FIELDS = {
    "total.num.queries": "queries_total",
    "total.num.cachehits": "cache_hits_total",
    "total.num.cachemiss": "cache_misses_total",
    "total.num.recursivereplies": "recursive_replies_total",
    "total.recursion.time.avg": "recursion_time_avg_seconds",
    "total.recursion.time.median": "recursion_time_median_seconds",
    "total.requestlist.avg": "request_list_average",
    "total.requestlist.max": "request_list_maximum",
    "total.requestlist.current.all": "request_list_current",
    "total.requestlist.exceeded": "request_list_exceeded_total",
    "total.requestlist.overwritten": "request_list_overwritten_total",
    "total.unwanted.replies": "unwanted_replies_total",
    "time.up": "uptime_seconds",
}
APPROVED_PREFIXES = {
    "num.answer.rcode.": "response_codes",
    "num.rpz.action.": "rpz_actions",
}


def build_tool(config):
    def unbound_stats(_arguments):
        completed = run_fixed(
            [config.unbound_control_path, "stats_noreset"],
            min(config.per_tool_timeout_seconds, 10), maximum_bytes=262144,
        )
        if completed["status"] != "ok":
            return {"available": False, "fields": {}, "response_codes": {}, "rpz_actions": {},
                    "limitation": "unbound-control was unavailable to the unprivileged gateway"}
        fields = {}
        grouped = {"response_codes": {}, "rpz_actions": {}}
        for line in completed["stdout"].splitlines():
            if "=" not in line:
                continue
            raw_name, raw_value = line.split("=", 1)
            name = raw_name.strip()
            value_text = raw_value.strip()
            try:
                value = float(value_text)
            except ValueError:
                continue
            if name in APPROVED_FIELDS:
                fields[APPROVED_FIELDS[name]] = value
                continue
            for prefix, group in APPROVED_PREFIXES.items():
                if name.startswith(prefix):
                    suffix = name[len(prefix):]
                    if suffix.replace("_", "").isalnum() and len(suffix) <= 40:
                        grouped[group][suffix] = value
        return {"available": True, "fields": fields, "response_codes": grouped["response_codes"],
                "rpz_actions": grouped["rpz_actions"], "limitation": "Only approved aggregate fields are returned."}
    return unbound_stats
