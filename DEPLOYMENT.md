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
| Unbound config | `unbound-checkconf unbound-etc/unbound.conf` when local Unbound is available | `/opt/homebrew/sbin/unbound-checkconf <staged-file>` or validate after a controlled staged-path substitution | `sudo unbound-control reload` |
| RPZ allowlist | Zone syntax through `unbound-checkconf` with the full config | `/opt/homebrew/sbin/unbound-checkconf` | `sudo unbound-control auth_zone_reload hagezi.allow` |
| Blocklist selector | One line; value supported by `update-blocklist.sh` | Run updater for the intended slot | Updater reloads the zone |
| Prometheus config | YAML parse; `promtool check config` when available | `/opt/homebrew/opt/prometheus/bin/promtool check config <file>` | `pkill -HUP -x prometheus` |
| Prometheus rules | YAML parse; `promtool check rules` when available | `/opt/homebrew/opt/prometheus/bin/promtool check rules <file>` | `pkill -HUP -x prometheus` |
| Blackbox config | YAML parse; `promtool check config` through full Prometheus config where applicable | Start or query blackbox after replacement | Restart blackbox exporter |
| Grafana config | Review INI syntax and paths | Start Grafana and query LAN `/api/health` | Restart Grafana |
| Grafana dashboard | `python3 -m json.tool <file>` | Same parser on staged file | File provider scans every 30s; no restart |
| LaunchDaemon | `plutil -lint <file>` | `plutil -lint <staged-file>` | bootout/bootstrap or kickstart as appropriate |
| Python script | `python3 -m py_compile`; script self-test when provided | `/usr/bin/python3 <staged-file> --test` or `--selftest` | Kickstart owning periodic job or daemon |
| Bash script | `bash -n <file>` | `/bin/bash -n <staged-file>` | Next schedule or explicit kickstart |
| Zsh warmer | `zsh -n warmup/warm-unbound-cache.sh` | `/bin/zsh -n <staged-file>` | Next explicit or boot-time run |
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

The generated primary and secondary zonefiles MUST already exist before config
validation when the config references them.

```sh
# Workstation
scp unbound-etc/unbound.conf localadmin@192.168.0.100:~/unbound.conf.stage

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
scp prometheus-etc/prometheus.yml localadmin@192.168.0.100:~/prometheus.yml.stage
scp prometheus-etc/prometheus.rules.yml localadmin@192.168.0.100:~/prometheus.rules.yml.stage

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

## Deploy a Grafana dashboard

| Field | Value |
|---|---|
| Execution location | Workstation and mini |
| Privilege | `localadmin`; dashboard directory is owned for unprivileged deployment |
| State | State-changing visualization only |
| Success | JSON parses, checksum matches, Grafana health is `ok`, provisioner loads the updated UID |
| Failure | JSON error, checksum mismatch, Grafana database error, or old dashboard remains after two scan intervals |

```sh
# Workstation: validate and stage
python3 -m json.tool grafana-etc/dashboards/<name>.json >/dev/null
scp grafana-etc/dashboards/<name>.json localadmin@192.168.0.100:~/<name>.json.stage

# Mini: validate staged content, then replace
python3 -m json.tool ~/<name>.json.stage >/dev/null
cp ~/<name>.json.stage /opt/homebrew/etc/grafana/dashboards/<name>.json
curl -s http://192.168.0.100:3000/api/health
```

The provider scans every 30 seconds. Grafana MUST NOT be restarted merely to load a
dashboard. Dashboard UIDs MUST remain stable. See [DASHBOARDS.md](DASHBOARDS.md).

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
| `scripts/apple_content_cache_exporter.py` | `~/monitoring/apple_content_cache_exporter.py` | `--test` | `local.unbound.contentcache` |
| `scripts/unbound_client_rpz_exporter.py` | `~/monitoring/unbound_client_rpz_exporter.py` | `--test` | `local.unbound.clientstats` |
| `scripts/prometheus_alert_notifier.py` | `~/monitoring/prometheus_alert_notifier.py` | `--selftest` | `local.unbound.alertnotify` |

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
