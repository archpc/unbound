# Deployment

## Principles

Deployment is an explicit copy from a reviewed repository checkout to the mini, followed
by component validation and the smallest reload that makes the change active.

- A repository file MUST be validated before it replaces its live counterpart.
- A runtime file MUST NOT be replaced merely to make it match documentation.
- A service MUST NOT be reloaded when its validator fails.
- Only the affected component SHOULD be reloaded.
- Runtime-generated files and secrets MUST NOT be overwritten from repository content.
- The operator MUST verify both file identity and runtime behavior before describing a
  deployment as complete.

Run repository-side commands from the repository root on the operator workstation.
Run live validators and reloads on the mini.

## Offline validation boundary

`tools/validate-repository.sh` runs on the operator workstation and MUST remain offline
with respect to the mini: it MUST NOT use SSH, deployment credentials, runtime secrets,
or live service APIs. It proves repository syntax and structural contracts; it does not
prove that intended files are deployed or that services are healthy.

Target-host validation and functional canaries remain REQUIRED where the validation
matrix specifies them.

## Change workflow

1. Inspect `git status --short` and preserve unrelated worktree changes.
2. Identify the source file, runtime destination, owner, validator, and reload method in
   `deployment-manifest.json` and [REFERENCE.md](REFERENCE.md).
3. Edit the repository source.
4. Run the local validator.
5. Stage the file in a non-runtime path on the mini.
6. Run the target-host validator against the staged file where supported.
7. Replace the live file while preserving required owner and mode.
8. Reload only the owning service.
9. Compare SHA-256 for files whose manifest policy is `sha256`.
10. Run the component's functional checks.
11. Run `python3 tools/verify-deployment.py`; treat `INCOMPLETE` as unverified.

Copy operations are state-changing. The examples below use `scp` for clarity; an
operator MAY use `rsync` with equivalent review and verification. Finder MUST NOT be
used for repository synchronization.

## Validation matrix

| Component | Repository validator | Target validator | Activation |
|---|---|---|---|
| Unbound config | `unbound-checkconf config/unbound/unbound.conf` when local Unbound is available | `/opt/homebrew/sbin/unbound-checkconf <staged-file>` or validate after a controlled staged-path substitution | `sudo unbound-control reload` |
| RPZ allowlist | Zone syntax through `unbound-checkconf` with the full config | `/opt/homebrew/sbin/unbound-checkconf` | `sudo unbound-control auth_zone_reload hagezi.allow` |
| Blocklist selector | One line; value supported by `update-blocklist.sh` | Run updater with the selected source | Updater reloads `hagezi.rpz` |
| Prometheus config | YAML parse; `promtool check config` when available | `/opt/homebrew/opt/prometheus/bin/promtool check config <file>` | `pkill -HUP -x prometheus` |
| Prometheus rules | YAML parse; `promtool check rules` when available | `/opt/homebrew/opt/prometheus/bin/promtool check rules <file>` | `pkill -HUP -x prometheus` |
| Blackbox config | Structural/YAML validation where available | `/usr/local/sbin/blackbox_exporter --config.file=<staged-file> --config.check` | Restart blackbox only after the staged check passes |
| Grafana config | Review INI syntax and paths | Start Grafana and query LAN `/api/health` | Restart Grafana |
| Grafana dashboard | `python3 -m json.tool <file>` | Same parser on staged file | File provider scans every 30s; no restart |
| LaunchDaemon | `plutil -lint <file>` | `plutil -lint <staged-file>` | bootout/bootstrap or kickstart as appropriate |
| Python script | `python3 -m py_compile`; script self-test when provided | `/usr/bin/python3 <staged-file> --test` or `--selftest` | Kickstart owning periodic job or daemon |
| Bash script | `bash -n <file>` | `/bin/bash -n <staged-file>` | Next schedule or explicit kickstart |
| Zsh warmer | `zsh -n scripts/dns/warm-cache.zsh` | `/bin/zsh -n <staged-file>` | Next explicit or boot-time run |
| sshd fragment | Structural review only off-host | `sudo sshd -t` with a second session kept open | New SSH connection reads it automatically |
| newsyslog | Review format | `sudo newsyslog -nv -f <file>` | macOS periodic newsyslog run |

If `promtool` is unavailable on the workstation, target-host `promtool` validation is
REQUIRED before Prometheus reload.

## Read-only drift verification

| Field | Value |
|---|---|
| Execution location | Repository root on the operator workstation |
| Privilege | SSH access as `localadmin`; no remote `sudo` |
| State | Read-only locally and remotely |
| Success | `RESULT: CLEAN`, with no drift, missing required item, or unknown item |
| Failure | `RESULT: DRIFT`; `RESULT: INCOMPLETE` is also operationally unverified despite exit 0 |

```sh
python3 tools/verify-deployment.py
```

The verifier hashes only entries with `compare: sha256`. It checks existence and
metadata for generated files and sensitive runtime-only files. It MUST NOT read or hash
the ntfy topic or personal warm list. Exit status 0 for `INCOMPLETE` is deliberate: an
unreachable target is not proven drift. Automation wrapping this command MUST inspect
the result text or unknown count, not exit status alone.

## Deploy Unbound configuration

> **Warning:** an invalid resolver configuration can degrade every LAN client. A
> process restart also drops the cache.

| Field | Value |
|---|---|
| Execution location | Repository validation on workstation; stage, validate, replace, and reload on mini |
| Privilege | `sudo` for runtime replacement and control socket |
| State | State-changing |
| Success | `unbound-checkconf` passes; reload succeeds; DNSSEC, RPZ, and `.lan` canaries pass |
| Failure | Validator error, control error, timeout, wrong canary result, or more than one process on port 53 |

The generated blocklist zonefile MUST already exist before config validation when the
config references it.

```sh
# Workstation
scp config/unbound/unbound.conf localadmin@192.168.0.100:~/unbound.conf.stage

# Mini
/opt/homebrew/sbin/unbound-checkconf ~/unbound.conf.stage
sudo cp ~/unbound.conf.stage /opt/homebrew/etc/unbound/unbound.conf
sudo chown unbound:unbound /opt/homebrew/etc/unbound/unbound.conf
sudo chmod 644 /opt/homebrew/etc/unbound/unbound.conf
sudo /opt/homebrew/sbin/unbound-control reload
```

Run the health checks in [RUNBOOK.md](RUNBOOK.md#health-check). Use a process restart
only when the changed setting cannot reload. After restart, warm the cache.

## Deploy the RPZ allowlist

| Field | Value |
|---|---|
| Execution location | Workstation and mini |
| Privilege | `sudo` on mini |
| State | State-changing policy update |
| Success | Full config validates, `hagezi.allow` reload succeeds, intended domain resolves normally |
| Failure | Config/reload error or domain remains authoritative NXDOMAIN |

The SOA serial MUST increase whenever the allowlist content changes. Deploy the staged
file, validate the full Unbound configuration, then reload only `hagezi.allow`.

## Deploy Blackbox Exporter configuration

| Field | Value |
|---|---|
| Execution location | Workstation and mini |
| Privilege | `localadmin` for config; `sudo` for the system LaunchDaemon |
| State | State-changing probe configuration and exporter restart |
| Success | Staged `--config.check` passes, exporter health returns success, and configured probe canaries return expected results |
| Failure | Config check error, LaunchDaemon failure, exporter health failure, or probe regression |

```sh
# Workstation
scp config/prometheus/blackbox.yml \
  localadmin@192.168.0.100:~/blackbox.yml.stage

# Mini: validate before replacing or restarting anything
/usr/local/sbin/blackbox_exporter \
  --config.file="$HOME/blackbox.yml.stage" --config.check
cp /opt/homebrew/etc/blackbox.yml "$HOME/blackbox.yml.rollback"
install -m 0644 "$HOME/blackbox.yml.stage" /opt/homebrew/etc/blackbox.yml
sudo launchctl bootout system/local.unbound.blackbox
sudo launchctl bootstrap system \
  /Library/LaunchDaemons/local.unbound.blackbox.plist
curl -fsS http://127.0.0.1:9115/-/healthy >/dev/null
```

After the matching Prometheus scrape job is deployed and reloaded, require the resolver,
RPZ, upstream, and Grafana `probe_success` series to be present. Restore
`$HOME/blackbox.yml.rollback`, repeat `--config.check`, and restart the same LaunchDaemon
to roll back. Never restart blackbox after a failed staged check.

## Deploy Prometheus configuration or rules

| Field | Value |
|---|---|
| Execution location | Workstation and mini |
| Privilege | Runtime files are normally owned by `localadmin`; no `sudo` is required when ownership matches the manifest deployment |
| State | State-changing monitoring reload; DNS service is unaffected |
| Success | Live `promtool` passes, reload-success metric is 1, targets and rule API are healthy |
| Failure | Validator error, reload-success metric 0, missing rule, or target regression |

```sh
# Workstation: choose one or both files
scp config/prometheus/prometheus.yml localadmin@192.168.0.100:~/prometheus.yml.stage
scp config/prometheus/prometheus.rules.yml localadmin@192.168.0.100:~/prometheus.rules.yml.stage

# Mini: validate staged files before replacement
/opt/homebrew/opt/prometheus/bin/promtool check config ~/prometheus.yml.stage
/opt/homebrew/opt/prometheus/bin/promtool check rules ~/prometheus.rules.yml.stage
```

After copying validated content to `/opt/homebrew/etc/`, reload without restarting the
TSDB process:

```sh
pkill -HUP -x prometheus
curl -sG --data-urlencode 'query=prometheus_config_last_reload_successful' \
  http://127.0.0.1:9090/api/v1/query
```

The returned sample value MUST be `1`. A rule change SHOULD also be confirmed through
`/api/v1/rules`.

## Deploy the Grafana dashboard set

| Field | Value |
|---|---|
| Execution location | Workstation and mini |
| Privilege | `localadmin`; dashboard directory is owned for unprivileged deployment |
| State | State-changing visualization; removes four obsolete provisioned resources after an on-host backup |
| Success | Generator/checks pass, all five JSON files parse, checksum matches, obsolete watched files are absent, Grafana health is `ok`, and all five UIDs load |
| Failure | Generator drift, JSON error, checksum mismatch, Grafana database error, missing new UID, or obsolete UID remains after two scan intervals |

> **Warning:** moving the four obsolete JSON files out of Grafana's watched directory
> removes those dashboards from the UI. The procedure first copies every watched JSON
> file to a timestamped mode-0700 backup and moves retired files into that backup. Do
> not run the retirement step until the new rules, scrape job, and metric producers are
> available, or the new dashboards will correctly display UNKNOWN.

```sh
# Workstation: regenerate, validate, and stage the complete set
python3 tools/generate-grafana-dashboards.py --check
for file in config/grafana/dashboards/*.json; do
  python3 -m json.tool "$file" >/dev/null
done
(cd config/grafana/dashboards && shasum -a 256 *.json) \
  > grafana-dashboard-SHA256SUMS.stage
ssh localadmin@192.168.0.100 \
  'test ! -e ~/grafana-dashboard-stage && mkdir -m 700 ~/grafana-dashboard-stage'
scp config/grafana/dashboards/*.json \
  localadmin@192.168.0.100:~/grafana-dashboard-stage/
scp grafana-dashboard-SHA256SUMS.stage \
  config/grafana/provisioning/dashboards/unbound.yml \
  localadmin@192.168.0.100:~/

# Mini: validate staged content and prove the exact five-file inventory
for file in ~/grafana-dashboard-stage/*.json; do
  python3 -m json.tool "$file" >/dev/null || exit 1
done
test "$(find ~/grafana-dashboard-stage -type f -name '*.json' | wc -l | tr -d ' ')" = 5
(cd ~/grafana-dashboard-stage && \
  shasum -a 256 -c ~/grafana-dashboard-SHA256SUMS.stage)
ruby -e '
require "yaml"
document = YAML.safe_load(File.read(ARGV.fetch(0)), aliases: false)
provider = document.fetch("providers").fetch(0)
abort unless provider.fetch("type") == "file"
abort unless provider.fetch("allowUiUpdates") == false
abort unless provider.fetch("disableDeletion") == false
abort unless provider.fetch("options").fetch("path") == "/opt/homebrew/etc/grafana/dashboards"
' ~/unbound.yml

# Mini: preserve the inherited definitions, install the new set, then retire
# only the four explicitly named obsolete watched files.
backup="$HOME/grafana-dashboard-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup" "$backup/retired"
cp /opt/homebrew/etc/grafana/dashboards/*.json "$backup"/
cp /opt/homebrew/etc/grafana/provisioning/dashboards/unbound.yml \
  "$backup/unbound-provider.yml"
install -m 0644 ~/grafana-dashboard-stage/*.json \
  /opt/homebrew/etc/grafana/dashboards/
for name in network-tailscale services storage-backups system-stats; do
  test ! -e "/opt/homebrew/etc/grafana/dashboards/$name.json" || \
    mv "/opt/homebrew/etc/grafana/dashboards/$name.json" "$backup/retired/"
done
(cd /opt/homebrew/etc/grafana/dashboards && \
  shasum -a 256 -c ~/grafana-dashboard-SHA256SUMS.stage)

# Wait for two 30-second scans, then verify health and exact dashboard UIDs.
# Grafana 13 stores dashboards in unified storage; the legacy dashboard table is
# empty even while the dashboards are healthy and visible.
curl -fsS http://192.168.0.100:3000/api/health | \
  python3 -c 'import json,sys; assert json.load(sys.stdin).get("database") == "ok"'
uids="$(/usr/bin/sqlite3 -readonly /opt/homebrew/var/lib/grafana/grafana.db \
  'select name from resource where "group"="dashboard.grafana.app" and resource="dashboards" order by name;')"
expected="$(printf '%s\n' apple-content-cache host-services operations-overview \
  prometheus-monitoring unbound-dns)"
test "$uids" = "$expected"
```

The provider scans every 30 seconds. Grafana MUST NOT be restarted merely to load JSON.
The accompanying `allowUiUpdates: false` provider change is read at process startup.
After the JSON set is healthy, install the already staged and structurally checked
provider, then perform one controlled Grafana restart using its LaunchDaemon procedure:

```sh
install -m 0644 ~/unbound.yml \
  /opt/homebrew/etc/grafana/provisioning/dashboards/unbound.yml
sudo launchctl kickstart -k system/local.unbound.grafana
curl -fsS http://192.168.0.100:3000/api/health | \
  python3 -c 'import json,sys; assert json.load(sys.stdin).get("database") == "ok"'
```

The expected API health response contains HTTP 200 and database `ok`; repeat the
unified-storage UID query above. On a deliberately supported pre-unified-storage
Grafana release, use its legacy `dashboard.uid` table only after confirming that
version and storage mode; an empty legacy table is not evidence that Grafana 13 lost
its dashboards. Use the actual service label and domain from
[REFERENCE.md](REFERENCE.md#services-and-scheduled-jobs) if they differ from this
example. See [DASHBOARDS.md](DASHBOARDS.md).

### Roll back the Grafana dashboard set

Use the exact `$backup` directory printed/recorded during deployment. This removes the
new files, restores every inherited watched JSON, and restores the previous provider:

```sh
for name in operations-overview host-services; do
  rm -f "/opt/homebrew/etc/grafana/dashboards/$name.json"
done
cp "$backup"/*.json /opt/homebrew/etc/grafana/dashboards/
cp "$backup/retired"/*.json /opt/homebrew/etc/grafana/dashboards/ 2>/dev/null || true
cp "$backup/unbound-provider.yml" \
  /opt/homebrew/etc/grafana/provisioning/dashboards/unbound.yml
sudo launchctl kickstart -k system/local.unbound.grafana
curl -fsS http://192.168.0.100:3000/api/health | \
  python3 -c 'import json,sys; assert json.load(sys.stdin).get("database") == "ok"'
```

Wait two provider scans and verify the inherited seven UIDs. Rollback does not touch
Grafana's database history, Prometheus TSDB data, or any monitoring history.

## Deploy a LaunchDaemon

> **Warning:** bootout interrupts a running daemon. A malformed or incorrectly owned
> plist can prevent boot-time service restoration.

| Field | Value |
|---|---|
| Execution location | Workstation and mini |
| Privilege | `sudo` REQUIRED on mini |
| State | State-changing |
| Success | plist validates, is root:wheel mode 644, bootstrap succeeds, runtime state matches job type |
| Failure | `plutil` error, bootstrap error, missing dependency, or repeated crash loop |

```sh
# Workstation
plutil -lint launchd/local.unbound.<name>.plist
scp launchd/local.unbound.<name>.plist localadmin@192.168.0.100:~/local.unbound.<name>.plist.stage

# Mini
plutil -lint ~/local.unbound.<name>.plist.stage
sudo cp ~/local.unbound.<name>.plist.stage /Library/LaunchDaemons/local.unbound.<name>.plist
sudo chown root:wheel /Library/LaunchDaemons/local.unbound.<name>.plist
sudo chmod 644 /Library/LaunchDaemons/local.unbound.<name>.plist
sudo launchctl bootout system/local.unbound.<name> 2>/dev/null || true
sudo launchctl bootstrap system /Library/LaunchDaemons/local.unbound.<name>.plist
```

The `homebrew.mxcl.unbound.plist` file is reference-only. Unbound MUST be managed with
root `brew services`, not hand-deployed from that snapshot.

## Deploy runtime Python collectors

| Field | Value |
|---|---|
| Execution location | Workstation and mini |
| Privilege | `localadmin` destination; `sudo` only to force a system job with `launchctl` |
| State | State-changing collector code and metric output |
| Success | Self-test passes, job exits 0, textfile updates atomically, scrape error remains 0 |
| Failure | Self-test error, log error, stale textfile, or scrape error |

| Source | Runtime path | Self-test | Job |
|---|---|---|---|
| `scripts/monitoring/apple_content_cache_exporter.py` | `/usr/local/libexec/unbound-stack/monitoring/apple_content_cache_exporter.py` | `--test` | `local.unbound.contentcache` |
| `scripts/monitoring/unbound_client_rpz_exporter.py` | `/usr/local/libexec/unbound-stack/monitoring/unbound_client_rpz_exporter.py` | `--test` | `local.unbound.clientstats` |
| `scripts/monitoring/time_machine_exporter.py` | `/usr/local/libexec/unbound-stack/monitoring/time_machine_exporter.py` | `--test` | `local.unbound.timemachine` |
| `scripts/monitoring/prometheus_alert_notifier.py` | `/usr/local/libexec/unbound-stack/monitoring/prometheus_alert_notifier.py` | `--selftest` | `local.unbound.alertnotify` |

On macOS 26, `tmutil latestbackup` requires root plus Full Disk Access. A system
LaunchDaemon does not gain that TCC permission merely by running as root. The Time
Machine collector therefore falls back to the newest completed `SnapshotDates` entry
returned by `/usr/bin/defaults export /Library/Preferences/com.apple.TimeMachine -`.
The export is captured only in process memory; the collector examines only
`Destinations[*].SnapshotDates` and never persists, logs, or emits raw destination
metadata. Direct file access and `plutil` are also TCC-blocked in the deployed launchd
domain. A deployment MUST preflight the staged collector through a temporary system
LaunchDaemon before the first live change; a successful interactive or SSH invocation
is not equivalent evidence for the launchd execution domain. Do not print the raw
preference export during troubleshooting.

The notifier self-test MUST NOT be replaced with `--test-send` unless a real ntfy push
is intentionally authorized. Deployment procedures MUST NOT read or print the topic.

## Full installation order

For a blank host, dependencies require this order:

1. Install macOS, create `localadmin`, enable SSH, and install Xcode command-line tools.
2. Install Homebrew under `/opt/homebrew`.
3. Install `unbound`, `prometheus`, `grafana`, `node_exporter`, and `go`.
4. Build and install `unbound_exporter` and `blackbox_exporter` under `/usr/local/sbin`.
5. Create runtime directories and deploy configuration, scripts, and selector files.
6. Generate both RPZ zonefiles before validating `unbound.conf`.
7. Validate and start Unbound with `sudo brew services start unbound`.
8. Deploy Prometheus, Grafana, blackbox, node_exporter, and their system LaunchDaemons.
9. Create the textfile directory and grant the deliberate `admin` group write access.
10. Deploy collectors and periodic jobs.
11. Restore or recreate the ntfy topic without exposing it; deploy the notifier.
12. Apply application-firewall allows to resolved Cellar paths for LAN-facing binaries.
13. Install and validate SSH hardening while keeping an existing session open.

## One-time runtime-layout migration

The reviewed migration helper installs the root-owned libexec layout, validates native
configuration, replaces only the seven affected LaunchDaemons, runs DNS and monitoring
acceptance checks, and restores the supplied rollback bundle automatically on error.
It MUST be run only from the staged commit recorded in the helper.

```sh
sudo /Users/localadmin/unbound-migration-dcb19cf/migrate-layout.sh \
  /Users/localadmin/unbound-migration-dcb19cf \
  /Users/localadmin/unbound-rollback-20260711-dcb19cf
```

The sudo password MUST be entered interactively and MUST NOT be placed in a command,
file, shell history expansion, or automation transcript. The rollback directory MUST
remain on the mini after successful migration.
14. Run the complete validation checklist in [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md#acceptance-checklist).

Detailed rebuild commands and rollback criteria are in
[DISASTER-RECOVERY.md](DISASTER-RECOVERY.md).

## Post-deployment record

Before handing off, record:

- repository commit or exact dirty-tree diff used;
- source and destination paths;
- local and target validators run;
- whether a reload or restart occurred;
- functional checks and their outcomes;
- drift-verifier result;
- any unverified external dependency such as router DHCP or firewall state.

Do not record secret values.
