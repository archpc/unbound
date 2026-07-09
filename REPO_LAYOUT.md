# Repo Layout

This repo is the restore source for a live Mac mini Unbound, Prometheus, Grafana,
and launchd monitoring stack. Repo paths may move later, but runtime paths should
stay stable unless a migration explicitly updates the matching launchd plist,
config reference, docs, and validation.

## Current directories

| Path | Role |
|---|---|
| `unbound-etc/` | Unbound config snapshot, RPZ allowlist, blocklist source files, blocklist tooling |
| `prometheus-etc/` | Prometheus config, alert rules, blackbox modules, node_exporter args |
| `grafana-etc/` | Grafana config, provisioning, dashboard JSON source of truth |
| `launchd/` | LaunchDaemon snapshots; `homebrew.mxcl.unbound.plist` is reference-only |
| `scripts/` | Python textfile exporters, alert notifier, local PDF renderer |
| `warmup/` | Cache warmup scripts and curated domain list |
| `usr-local-sbin/` | Root-owned helper scripts deployed under `/usr/local/sbin` |
| `etc-newsyslog.d/` | newsyslog config snapshot |
| `etc-ssh-sshd_config.d/` | sshd hardening snapshot |
| `fixtures/` | Local test fixture data |

## Source to runtime map

| Repo source | Runtime destination |
|---|---|
| `unbound-etc/unbound.conf` | `/opt/homebrew/etc/unbound/unbound.conf` |
| `unbound-etc/root.hints` | `/opt/homebrew/etc/unbound/root.hints` |
| `unbound-etc/blocklists/hagezi-allow.rpz` | `/opt/homebrew/etc/unbound/blocklists/hagezi-allow.rpz` |
| `unbound-etc/update-blocklist.sh`, `blocklist-tui.sh`, source files | `/opt/homebrew/etc/unbound/` |
| `prometheus-etc/prometheus.yml` | `/opt/homebrew/etc/prometheus.yml` |
| `prometheus-etc/prometheus.rules.yml` | `/opt/homebrew/etc/prometheus.rules.yml` |
| `prometheus-etc/prometheus.args` | `/opt/homebrew/etc/prometheus.args` |
| `prometheus-etc/blackbox.yml` | `/opt/homebrew/etc/blackbox.yml` |
| `prometheus-etc/node_exporter.args` | `/opt/homebrew/etc/node_exporter.args` |
| `grafana-etc/grafana.ini` | `/opt/homebrew/etc/grafana/grafana.ini` |
| `grafana-etc/provisioning/**` | `/opt/homebrew/etc/grafana/provisioning/**` |
| `grafana-etc/dashboards/*.json` | `/opt/homebrew/etc/grafana/dashboards/*.json` |
| `launchd/local.unbound.*.plist` | `/Library/LaunchDaemons/local.unbound.*.plist` |
| `scripts/apple_content_cache_exporter.py` | `/Users/localadmin/monitoring/apple_content_cache_exporter.py` |
| `scripts/unbound_client_rpz_exporter.py` | `/Users/localadmin/monitoring/unbound_client_rpz_exporter.py` |
| `scripts/prometheus_alert_notifier.py` | `/Users/localadmin/monitoring/prometheus_alert_notifier.py` |
| `warmup/*` | `/Users/localadmin/warmup/` |
| `usr-local-sbin/*` | `/usr/local/sbin/` |
| `etc-newsyslog.d/unbound-monitoring.conf` | `/etc/newsyslog.d/unbound-monitoring.conf` |
| `etc-ssh-sshd_config.d/500-keyonly.conf` | `/etc/ssh/sshd_config.d/500-keyonly.conf` |

## File classes

- Config: `unbound-etc/unbound.conf`, `prometheus-etc/*`, `grafana-etc/grafana.ini`,
  `grafana-etc/provisioning/**`, `etc-newsyslog.d/*`, `etc-ssh-sshd_config.d/*`.
- Scripts: `unbound-etc/*.sh`, `scripts/*.py`, `warmup/*.sh`, `usr-local-sbin/*`.
- Generated output: `DISASTER-RECOVERY.pdf`, generated RPZ blocklists, `.prom` textfiles,
  Python bytecode, Grafana/Prometheus runtime databases.
- State/logs: `/opt/homebrew/var/**`, `/opt/homebrew/var/log/**`,
  `/Users/localadmin/monitoring/state/**`, `~/monitoring/ntfy.topic`,
  `/opt/homebrew/var/lib/node_exporter/textfile/*.prom`.

## launchd labels and script paths

| Label | Program path |
|---|---|
| `homebrew.mxcl.unbound` | `/opt/homebrew/opt/unbound/sbin/unbound -c /opt/homebrew/etc/unbound/unbound.conf` |
| `local.unbound.alertnotify` | `/Users/localadmin/monitoring/prometheus_alert_notifier.py` |
| `local.unbound.blackbox` | `/usr/local/sbin/blackbox_exporter` |
| `local.unbound.blocklist` | `/opt/homebrew/etc/unbound/update-blocklist.sh` |
| `local.unbound.clientstats` | `/Users/localadmin/monitoring/unbound_client_rpz_exporter.py` |
| `local.unbound.contentcache` | `/Users/localadmin/monitoring/apple_content_cache_exporter.py` |
| `local.unbound.exporter` | `/usr/local/sbin/unbound_exporter` |
| `local.unbound.grafana` | `/opt/homebrew/opt/grafana/bin/grafana` |
| `local.unbound.nodeexporter` | `/opt/homebrew/opt/node_exporter/bin/node_exporter_brew_services` |
| `local.unbound.prometheus` | `/opt/homebrew/opt/prometheus/bin/prometheus_brew_services` |
| `local.unbound.roothints` | `/usr/local/sbin/unbound-refresh-hints` |
| `local.unbound.warmboot` | `/Users/localadmin/warmup/warm-at-boot.sh` |
| `local.unbound.warmlist` | `/Users/localadmin/warmup/build-warm-list.sh` |

## node_exporter textfiles

All textfile outputs land in `/opt/homebrew/var/lib/node_exporter/textfile`.

| File | Producer |
|---|---|
| `unbound-blocklist.prom` | `update-blocklist.sh` |
| `unbound-warmlist.prom` | `build-warm-list.sh` |
| `unbound-warmboot.prom` | `warm-at-boot.sh` |
| `unbound-roothints.prom` | `unbound-refresh-hints` |
| `unbound-alertnotify.prom` | `prometheus_alert_notifier.py` |
| `unbound-clientstats.prom` | `unbound_client_rpz_exporter.py` |
| `apple-content-cache.prom` | `apple_content_cache_exporter.py` |

## Breadcrumb convention

Scheduled jobs share:

```text
unbound_job_last_success_timestamp_seconds{task="<name>"}
```

Emit only `# TYPE unbound_job_last_success_timestamp_seconds gauge` for this shared
family. Do not add per-file `# HELP` text for the shared breadcrumb; conflicting
HELP strings can make node_exporter reject samples from the merged family while
`node_textfile_scrape_error` remains clean.

## Paths that should not move

- Runtime config: `/opt/homebrew/etc/unbound`, `/opt/homebrew/etc/prometheus.yml`,
  `/opt/homebrew/etc/prometheus.rules.yml`, `/opt/homebrew/etc/blackbox.yml`,
  `/opt/homebrew/etc/node_exporter.args`, `/opt/homebrew/etc/grafana`.
- Runtime scripts: `/Users/localadmin/monitoring`, `/Users/localadmin/warmup`,
  `/usr/local/sbin`.
- launchd labels, Prometheus job names, Grafana dashboard UIDs, metric names, and
  node_exporter textfile names.

## Paths that may move later

- Local-only docs and generated PDF output.
- `fixtures/` to `testdata/`, if exporter selftests are updated.
- `scripts/render-disaster-recovery-pdf.py` to `tools/`.
- Repo source directories, only after a deploy manifest/tool can map old source names
  to unchanged runtime destinations.

## Validation commands

| Component | Local validation |
|---|---|
| Unbound config | `unbound-checkconf unbound-etc/unbound.conf` when available |
| Shell scripts | `bash -n unbound-etc/*.sh warmup/*.sh usr-local-sbin/*` |
| Python scripts | `PYTHONPYCACHEPREFIX=/private/tmp/pycache-unbound python3 -m py_compile scripts/*.py` |
| Python selftests | `python3 scripts/apple_content_cache_exporter.py --test`; `python3 scripts/unbound_client_rpz_exporter.py --test`; `python3 scripts/prometheus_alert_notifier.py --selftest` |
| launchd plists | `plutil -lint launchd/*.plist` |
| Prometheus | `promtool check config prometheus-etc/prometheus.yml`; `promtool check rules prometheus-etc/prometheus.rules.yml` |
| Grafana dashboards | `python3 -m json.tool grafana-etc/dashboards/<name>.json >/dev/null` |
| SSH config | `sshd -t -f /path/to/test/sshd_config` on the mini before relying on it |
| Runtime textfiles | `curl -s http://127.0.0.1:9100/metrics | grep '^node_textfile_scrape_error 0$'` |
