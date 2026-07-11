# Repository and deployment layout

The repository separates product configuration, service definitions, runtime code,
local tooling, tests, and operating documentation. Runtime generated data and secrets
are never treated as deployed source.

## Directory responsibilities

| Path | Responsibility | Deployed? |
|---|---|---|
| `config/unbound/` | Unbound config, RPZ allowlist and selector, curated warm list | Selected files |
| `config/prometheus/` | Prometheus rules/config, blackbox modules, exporter arguments | Yes |
| `config/grafana/` | Grafana config, provisioning and dashboard JSON | Yes |
| `config/newsyslog/` | Scheduled-job log rotation | Yes |
| `config/ssh/` | Key-only SSH fragment | Yes |
| `launchd/` | System LaunchDaemons and the reference-only Homebrew Unbound plist | Selected files |
| `scripts/dns/` | DNS policy updates, cache maintenance and root-hints maintenance | Yes |
| `scripts/monitoring/` | Runtime metric collectors and alert notifier | Yes |
| `scripts/maintenance/` | Explicit operator maintenance tools | Yes |
| `tools/` | Local validation, drift inspection and document generation | No |
| `tests/` | Local contract and behavior tests | No |
| `docs/` | Dated audits and migration records | No |

Root-level Markdown files remain the canonical operator documentation so entry points
are visible from a fresh checkout.

## Live layout

Custom executable code MUST be installed root-owned outside a user-writable home:

```text
/usr/local/libexec/unbound-stack/
├── dns/          # scheduled and operator-invoked DNS code
└── monitoring/   # collectors and notifier, executed as localadmin where appropriate

/usr/local/share/unbound-stack/
└── dns-warm-domains.txt
```

Product configuration stays in native Homebrew locations under `/opt/homebrew/etc`.
Mutable product state stays under `/opt/homebrew/var`. The ntfy topic, notifier state,
and personalized warm list remain under `/Users/localadmin` because they are sensitive
or user-owned mutable state rather than deployed code.

This split is deliberate: a root LaunchDaemon MUST NOT execute code writable by
`localadmin`, while an unprivileged job MAY execute root-owned, world-readable code.
All managed scripts use stable absolute paths in LaunchDaemon definitions.

## Machine-readable mapping

[deployment-manifest.json](deployment-manifest.json) is the canonical exact mapping.
Its comparison policies are:

| Policy | Meaning |
|---|---|
| `sha256` | Local and remote regular-file contents MUST match |
| `existence` | Destination existence and metadata are checked; contents are not hashed |
| `none` | Local-only source; no runtime destination |
| `sensitive: true` | Runtime-only content is never read or hashed |

The manifest also declares metadata defaults. Deployed scripts MUST be `root:wheel`
mode 0755 and LaunchDaemon plists MUST be `root:wheel` mode 0644. The verifier treats
metadata mismatch as drift.

## Source-to-runtime summary

| Repository source | Runtime destination |
|---|---|
| `config/unbound/unbound.conf` | `/opt/homebrew/etc/unbound/unbound.conf` |
| `config/unbound/root.hints` | `/opt/homebrew/etc/unbound/root.hints` |
| `config/unbound/blocklists/hagezi-allow.rpz` | `/opt/homebrew/etc/unbound/blocklists/hagezi-allow.rpz` |
| `config/unbound/blocklist-source` | `/opt/homebrew/etc/unbound/blocklist-source` |
| `config/unbound/dns-warm-domains.txt` | `/usr/local/share/unbound-stack/dns-warm-domains.txt` |
| `config/prometheus/*` | Product-native paths under `/opt/homebrew/etc` |
| `config/grafana/**` | Product-native paths under `/opt/homebrew/etc/grafana` |
| `scripts/dns/*` | `/usr/local/libexec/unbound-stack/dns/` |
| `scripts/monitoring/*` | `/usr/local/libexec/unbound-stack/monitoring/` |
| `scripts/maintenance/screenshare.sh` | `/usr/local/sbin/screenshare` |
| `launchd/local.unbound.*.plist` | `/Library/LaunchDaemons/` |
| `config/newsyslog/unbound-monitoring.conf` | `/etc/newsyslog.d/unbound-monitoring.conf` |
| `config/ssh/500-keyonly.conf` | `/etc/ssh/sshd_config.d/500-keyonly.conf` |

Use the manifest for individual filenames; wildcard rows above are summaries only.

## Runtime-only and generated data

| Data | Runtime location | Policy |
|---|---|---|
| Generated RPZ | `/opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz` | Atomic replacement plus last-known-good `.bak` |
| Textfile metrics | `/opt/homebrew/var/lib/node_exporter/textfile/*.prom` | Atomic producer writes; not tracked |
| Prometheus TSDB | `/opt/homebrew/var/prometheus` | Runtime only |
| Grafana database | `/opt/homebrew/var/lib/grafana/grafana.db` | Sensitive runtime state |
| Notifier state | `/Users/localadmin/monitoring/state/notified-alerts.json` | Runtime only |
| ntfy topic | `/Users/localadmin/monitoring/ntfy.topic` | Secret; existence and metadata only |
| Personal warm list | `/Users/localadmin/warmup/dns-warm-domains.personal.txt` | Private, mode 0600; existence and metadata only |
| Root trust anchor | `/opt/homebrew/var/lib/unbound/root.key` | Unbound-managed state |

## Shared textfile contract

The seven producers write one stable `.prom` file each. Writes MUST use a same-directory
temporary file followed by atomic replacement. Producers sharing
`unbound_job_last_success_timestamp_seconds{task="<name>"}` MUST emit one `TYPE`
declaration and MUST NOT emit conflicting `HELP` declarations. The group-writable
textfile directory is an explicit trust boundary: syntax checks detect malformed
metrics, not fabricated valid values.

## Stable interfaces

Launchd labels, schedules, Prometheus job and metric names, dashboard UIDs, RPZ zone
names, textfile names, and the Unbound control socket remain stable. A change to any of
these interfaces requires an explicit behavioral migration and acceptance test.

## Local validation

```sh
tools/validate-repository.sh
```

The script is the canonical offline validation entrypoint. It MUST NOT contact the
target host. Component-specific and target-host validators are defined in
[DEPLOYMENT.md](DEPLOYMENT.md#validation-matrix).
