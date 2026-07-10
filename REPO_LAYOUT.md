# Repository layout

This repository stores intended configuration, runtime scripts, service definitions,
dashboard sources, tests, and documentation. Runtime paths are stable interfaces even
when repository organization changes.

## Directory responsibilities

| Path | Responsibility | Deployed? |
|---|---|---|
| `unbound-etc/` | Unbound config, tracked allowlist, generated-list selectors and updater | Selected files |
| `prometheus-etc/` | Prometheus config/rules, blackbox modules, exporter arguments | Yes |
| `grafana-etc/` | Grafana config, provisioning, dashboard JSON | Yes |
| `launchd/` | System LaunchDaemon snapshots and Homebrew Unbound plist reference | All `local.unbound.*`; Unbound plist is reference-only |
| `scripts/` | Runtime Python collectors and ntfy notifier | Yes |
| `warmup/` | Cache warming, personalized-list generation, curated domains | Yes |
| `usr-local-sbin/` | Root-owned helper scripts | Yes |
| `etc-newsyslog.d/` | Scheduled-job log rotation | Yes |
| `etc-ssh-sshd_config.d/` | Key-only SSH fragment | Yes |
| `tools/` | Local-only drift and documentation tools | No |
| `tests/` | Local-only contract tests | No |

## Machine-readable mapping

[deployment-manifest.json](deployment-manifest.json) is the canonical source-to-runtime
map used by `tools/verify-deployment.py`. It records these policies:

| Policy | Meaning |
|---|---|
| `sha256` | Local and remote regular-file contents MUST match |
| `existence` | Destination existence and metadata are checked; contents are not hashed |
| `none` | Local-only source; no runtime destination |
| `sensitive: true` | Runtime-only content is never read or hashed |

The manifest's `docs` entries are the canonical local-only documentation inventory and
MUST match [README.md](README.md#documentation-map). `tools/check-docs.py` validates
that contract. Documentation-support tooling is intentionally not a `docs` manifest
entry because it is local code, not a Markdown reference artifact.

## Source-to-runtime summary

| Repository source | Runtime destination |
|---|---|
| `unbound-etc/unbound.conf` | `/opt/homebrew/etc/unbound/unbound.conf` |
| `unbound-etc/root.hints` | `/opt/homebrew/etc/unbound/root.hints` |
| `unbound-etc/blocklists/hagezi-allow.rpz` | `/opt/homebrew/etc/unbound/blocklists/hagezi-allow.rpz` |
| `unbound-etc/update-blocklist.sh` | `/opt/homebrew/etc/unbound/update-blocklist.sh` |
| `unbound-etc/blocklist-tui.sh` | `/opt/homebrew/etc/unbound/blocklist-tui.sh` |
| `unbound-etc/blocklist-source` | `/opt/homebrew/etc/unbound/blocklist-source` |
| `prometheus-etc/prometheus.yml` | `/opt/homebrew/etc/prometheus.yml` |
| `prometheus-etc/prometheus.rules.yml` | `/opt/homebrew/etc/prometheus.rules.yml` |
| `prometheus-etc/prometheus.args` | `/opt/homebrew/etc/prometheus.args` |
| `prometheus-etc/blackbox.yml` | `/opt/homebrew/etc/blackbox.yml` |
| `prometheus-etc/node_exporter.args` | `/opt/homebrew/etc/node_exporter.args` |
| `grafana-etc/grafana.ini` | `/opt/homebrew/etc/grafana/grafana.ini` |
| `grafana-etc/provisioning/**` | `/opt/homebrew/etc/grafana/provisioning/**` |
| `grafana-etc/dashboards/*.json` | `/opt/homebrew/etc/grafana/dashboards/*.json` |
| `launchd/local.unbound.*.plist` | `/Library/LaunchDaemons/local.unbound.*.plist` |
| `scripts/*.py` | `/Users/localadmin/monitoring/` using manifest filenames |
| `warmup/*` | `/Users/localadmin/warmup/` |
| `usr-local-sbin/*` | `/usr/local/sbin/` |
| `etc-newsyslog.d/unbound-monitoring.conf` | `/etc/newsyslog.d/unbound-monitoring.conf` |
| `etc-ssh-sshd_config.d/500-keyonly.conf` | `/etc/ssh/sshd_config.d/500-keyonly.conf` |

Use the manifest, not wildcard assumptions, for an exact deployment. It also records
generated RPZ zones, textfiles, and sensitive runtime-only files.

## File classes

| Class | Examples | Change consequence |
|---|---|---|
| Intended config | `unbound.conf`, Prometheus YAML, Grafana INI | Requires validation and component activation |
| Runtime script | collectors, updater, warmers | Requires syntax/self-test and next run or kickstart |
| Service definition | `launchd/*.plist` | Requires plist validation and controlled bootstrap |
| Dashboard source | `grafana-etc/dashboards/*.json` | Provider loads after validated copy |
| Local tooling | `tools/*.py`, tests | Never deployed |
| Generated runtime | RPZ zones, `.prom` files, TSDB, Grafana DB | MUST NOT be treated as tracked source |
| Sensitive runtime | ntfy topic, personal warm list | MUST NOT be read by repository tools or committed |

## Stable interfaces

The following names SHOULD NOT change without an explicit migration:

- runtime paths in `deployment-manifest.json`;
- launchd labels;
- Prometheus job names;
- Grafana dashboard and datasource UIDs;
- metric and textfile names;
- RPZ zone names;
- the Unbound control-socket path.

A migration MUST update every reference, consumer, test, document, and manifest entry in
one reviewed change. Repository directories MAY move only when runtime destinations stay
stable or the migration explicitly covers them.

## Shared textfile contract

Producers write atomically under `/opt/homebrew/var/lib/node_exporter/textfile`:

| File | Producer |
|---|---|
| `unbound-blocklist.prom` | Blocklist updater |
| `unbound-warmlist.prom` | Personalized warm-list builder |
| `unbound-warmboot.prom` | Boot warmer |
| `unbound-roothints.prom` | Root-hints updater |
| `unbound-alertnotify.prom` | Alert notifier |
| `unbound-clientstats.prom` | Client RPZ collector |
| `apple-content-cache.prom` | Apple Content Cache collector |

All breadcrumb producers share
`unbound_job_last_success_timestamp_seconds{task="<name>"}`. They MUST emit a single
`TYPE` declaration and MUST NOT emit conflicting per-file `HELP` text for that shared
family. A well-formed but false metric remains inside the accepted `admin` group trust
boundary; `node_textfile_scrape_error` detects syntax errors, not lies.

## Local-only validation

Run the relevant commands from the repository root:

```sh
python3 tools/check-docs.py
python3 -m json.tool deployment-manifest.json >/dev/null
PYTHONPYCACHEPREFIX=/private/tmp/pycache-unbound python3 -m unittest \
  tests.test_monitoring_stack tests.test_deployment_verifier
```

Component validators and deployment activation are specified in
[DEPLOYMENT.md](DEPLOYMENT.md#validation-matrix).
