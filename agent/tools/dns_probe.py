"""Fixed local-resolver DNS probes with optional validated domain."""

from __future__ import annotations

import re

from agent.tools.common import run_fixed


DOMAIN_RE = re.compile(r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\.?$")
STATUS_RE = re.compile(r"status:\s*([A-Z]+)")
FLAGS_RE = re.compile(r"flags:\s*([^;]+);")

FIXED_PROBES = (
    ("external_resolution", "cloudflare.com", "NOERROR"),
    ("local_zone", "mini.lan", "NOERROR"),
    ("rpz_canary", "google-analytics.com", "NXDOMAIN"),
    ("dnssec_valid", "cloudflare.com", "NOERROR"),
    ("dnssec_broken", "dnssec-failed.org", "SERVFAIL"),
)


def _run_probe(config, name, domain, expected):
    completed = run_fixed(
        [config.dig_path, "@127.0.0.1", "+time=2", "+tries=1", "+noall", "+comments", domain, "A"],
        min(config.per_tool_timeout_seconds, 5), maximum_bytes=16384,
    )
    status_match = STATUS_RE.search(completed["stdout"])
    flags_match = FLAGS_RE.search(completed["stdout"])
    status = status_match.group(1) if status_match else "UNAVAILABLE"
    flags = sorted(flags_match.group(1).split()) if flags_match else []
    return {"name": name, "domain": domain, "status": status, "expected_status": expected,
            "passed": status == expected, "flags": flags[:12]}


def build_tool(config):
    def dns_probe(arguments):
        requested = arguments.get("domain")
        if requested is not None and not DOMAIN_RE.fullmatch(requested):
            raise ValueError("domain failed validation")
        probes = [_run_probe(config, *probe) for probe in FIXED_PROBES]
        if requested is not None:
            probes.append(_run_probe(config, "caller_domain", requested.rstrip("."), "NOERROR"))
        return {"resolver": "127.0.0.1", "probes": probes,
                "limitation": "All queries used the approved local resolver; no arbitrary server was accepted."}
    return dns_probe
