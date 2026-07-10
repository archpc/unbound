# Mac mini DNS and monitoring

This repository is the configuration and recovery source for a recursive Unbound DNS
resolver and its monitoring stack on a Mac mini. It is an operations repository, not an
application: there is no build artifact, and editing a tracked file does not change the
live host until that file is deployed and the owning service is reloaded.

The target host is `mini.lan` at `192.168.0.100`. Unbound serves the LAN on port 53,
validates DNSSEC, provides the static `.lan` zone, and applies an RPZ allowlist followed
by two independently managed blocklist slots. Prometheus, Grafana, exporters, scheduled
jobs, and an ntfy notifier observe the resolver and the host.

## Documentation map

| Document | Purpose | Primary audience |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Component relationships, request and monitoring flows, ownership, trust boundaries, failure domains | Operators and contributors |
| [RUNBOOK.md](RUNBOOK.md) | Routine health checks, blocklist work, service operations, backups, and maintenance | Operators |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Safe file deployment, validation, reload, and full installation ordering | Operators and contributors |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Symptom-driven diagnosis and smallest safe fixes | Operators |
| [REFERENCE.md](REFERENCE.md) | Ports, addresses, DNS records, services, schedules, jobs, alerts, metrics, paths, and dependencies | Everyone |
| [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md) | Rebuild, cutover, and rollback after host loss | Recovery operator |
| [REPO_LAYOUT.md](REPO_LAYOUT.md) | Repository classes and the source-to-runtime mapping model | Contributors |
| [DASHBOARDS.md](DASHBOARDS.md) | Grafana dashboard conventions | Dashboard contributors |
| [AGENTS.md](AGENTS.md) | Repository-specific automation and contribution contract | Automated contributors |

## Authority and live state

Repository implementation is authoritative in this order:

1. configuration;
2. source code;
3. scripts;
4. deployment tooling, including `deployment-manifest.json`;
5. LaunchDaemon definitions;
6. tests;
7. documentation;
8. historical notes.

The repository is authoritative for intended configuration. It is not evidence that the
live mini currently matches that configuration. `deployment-manifest.json` is the
machine-readable source-to-runtime map, and `tools/verify-deployment.py` performs a
read-only comparison.

```sh
python3 tools/verify-deployment.py
```

An `INCOMPLETE` result means the remote host was not fully checked. It MUST NOT be
interpreted as a clean deployment. See [DEPLOYMENT.md](DEPLOYMENT.md#read-only-drift-verification).

## Safety contract

The capitalized requirement words in this documentation are used as described by
[RFC 8174](https://www.rfc-editor.org/rfc/rfc8174).

- Unbound configuration MUST pass `unbound-checkconf` before reload or restart.
- Prometheus configuration and rules MUST pass `promtool` on the target host before reload.
- LaunchDaemon plists MUST pass `plutil -lint` before bootstrap or replacement.
- Grafana dashboard JSON MUST parse before deployment.
- `brew services` MUST be invoked with `sudo` for Unbound.
- Prometheus, Grafana, and node_exporter MUST NOT be started with `brew services`; their
  repository definitions are system LaunchDaemons.
- Generated blocklist zonefiles MUST NOT be edited by hand.
- Secrets, private keys, tokens, and the ntfy topic MUST NOT be committed or printed.
- A repository edit MUST NOT be described as live until deployment and runtime validation
  have both succeeded.

## Quick health check

Run on the mini unless noted otherwise:

```sh
dig @127.0.0.1 cloudflare.com A | grep -E 'status:|flags:'
dig @127.0.0.1 google-analytics.com A | grep -E 'status:|flags:'
dig @127.0.0.1 dnssec-failed.org A | grep 'status:'
curl -s http://127.0.0.1:9090/api/v1/alerts
curl -s http://127.0.0.1:9100/metrics | grep '^node_textfile_scrape_error'
```

Expected results are `NOERROR` with the `ad` flag for `cloudflare.com`, `NXDOMAIN` with
the `aa` flag for `google-analytics.com`, `SERVFAIL` for `dnssec-failed.org`, no firing
alerts, and `node_textfile_scrape_error 0`. See [RUNBOOK.md](RUNBOOK.md#health-check) for
the procedure metadata and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) when a result differs.

## Repository-local validation

Run from the repository root:

```sh
python3 tools/check-docs.py
PYTHONPYCACHEPREFIX=/private/tmp/pycache-unbound python3 -m unittest \
  tests.test_monitoring_stack tests.test_deployment_verifier
git diff --check
```

Component-specific validators are listed in [DEPLOYMENT.md](DEPLOYMENT.md#validation-matrix).
