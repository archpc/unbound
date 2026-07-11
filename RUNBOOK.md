# Operator runbook

This runbook covers routine operation of the existing mini. Use
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) for failures, [DEPLOYMENT.md](DEPLOYMENT.md)
for repository-to-host changes, and [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md) for a
replacement or erased host.

All capitalized requirement words use the RFC 8174 meanings described in
[README.md](README.md#safety-contract).

## Access

### SSH

| Field | Value |
|---|---|
| Execution location | Operator workstation |
| Privilege | Existing SSH key for `localadmin` |
| State | Read-only connection establishment |
| Success | A shell opens as `localadmin` |
| Failure | Authentication failure, timeout, or unreachable host |

```sh
ssh localadmin@192.168.0.100
```

The deployment verifier uses the target recorded in `deployment-manifest.json` and MAY
be used when LAN addressing is unavailable. SSH passwords and keyboard-interactive
authentication are disabled by the tracked sshd fragment.

### Web interfaces

| Interface | URL | Reachability |
|---|---|---|
| Grafana | `http://192.168.0.100:3000` | LAN only |
| Prometheus alerts | `http://192.168.0.100:9090/alerts` | LAN only |

Grafana does not bind loopback. A failed `curl http://127.0.0.1:3000` on the mini is
expected.

## Health check

| Field | Value |
|---|---|
| Execution location | Mini over SSH; the first query MAY also be run from any LAN client using `192.168.0.100` |
| Privilege | `localadmin`; no `sudo` |
| State | Read-only; DNS probes generate normal queries |
| Success | Results match the table below |
| Failure | Timeout, unexpected rcode/flags, firing alerts, fewer than eight healthy targets, or textfile scrape error |

```sh
dig @127.0.0.1 +time=2 +tries=1 cloudflare.com A | grep -E 'status:|flags:'
dig @127.0.0.1 +time=2 +tries=1 google-analytics.com A | grep -E 'status:|flags:'
dig @127.0.0.1 +time=2 +tries=1 dnssec-failed.org A | grep 'status:'
dig @127.0.0.1 +time=2 +tries=1 mini.lan A +short
curl -s http://127.0.0.1:9090/api/v1/targets | grep -o '"health":"up"' | wc -l
curl -s http://127.0.0.1:9090/api/v1/alerts
curl -s http://127.0.0.1:9100/metrics | grep '^node_textfile_scrape_error'
```

| Check | Expected result |
|---|---|
| `cloudflare.com` | `NOERROR` and `ad` flag |
| `google-analytics.com` | `NXDOMAIN` and `aa` flag |
| `dnssec-failed.org` | `SERVFAIL` |
| `mini.lan` | `192.168.0.100` |
| Healthy target count | `8` |
| Alerts API | Empty `alerts` array |
| Textfile collector | `node_textfile_scrape_error 0` |

An alert during an intentional maintenance window is not automatically a defect. The
operator SHOULD confirm that it matches the change and resolves afterward.

## Read service and job state

| Field | Value |
|---|---|
| Execution location | Mini |
| Privilege | `localadmin`; no `sudo` |
| State | Read-only |
| Success | `state = running` for daemons; periodic jobs normally show `not running` with last exit code 0 |
| Failure | Repeated run count growth, nonzero last exit after the job had time to complete, or missing label |

```sh
launchctl print system/homebrew.mxcl.unbound | grep -E 'state|runs|last exit'
launchctl print system/local.unbound.prometheus | grep -E 'state|runs|last exit'
launchctl print system/local.unbound.<name> | grep -E 'state|runs|last exit'
```

Periodic jobs are not expected to remain running. Their freshness is represented by
textfile timestamps and corresponding alerts.

## Allow a domain blocked by RPZ

| Field | Value |
|---|---|
| Execution location | Diagnose on the mini; edit and validate in the repository; deploy to the mini |
| Privilege | `localadmin` for diagnosis; `sudo` for live replacement and control socket |
| State | State-changing; modifies policy and reloads one RPZ zone |
| Success | The domain no longer returns authoritative RPZ NXDOMAIN |
| Failure | Reload error, unchanged authoritative NXDOMAIN, syntax error, or failure to preserve the SOA serial change |

1. Confirm the response is an RPZ block:

   ```sh
   dig @127.0.0.1 example.com A | grep -E 'status:|flags:'
   ```

   `NXDOMAIN` with `aa` is a synthesized authoritative block. `NXDOMAIN` without `aa`
   is ordinary DNS and MUST NOT be treated as an RPZ false positive.

2. Edit `config/unbound/blocklists/hagezi-allow.rpz` in the repository. Add both the bare
   and wildcard `CNAME rpz-passthru.` records and increment the SOA serial.

3. Follow [DEPLOYMENT.md](DEPLOYMENT.md#deploy-the-rpz-allowlist). Reload only the
   allowlist and verify:

   ```sh
   sudo unbound-control auth_zone_reload hagezi.allow
   dig @127.0.0.1 example.com A | grep -E 'status:|flags:'
   ```

The generated blocklist file MUST NOT be hand-edited.

## Add an explicit local block

| Field | Value |
|---|---|
| Execution location | Repository checkout first; mini only for deployment and reload |
| Privilege | None for repository edit; `sudo` for live file replacement and reload |
| State | State-changing |
| Success | Config validates and the domain returns authoritative NXDOMAIN |
| Failure | `unbound-checkconf` error or unexpected result after reload |

Add the following under `server:` in `config/unbound/unbound.conf`:

```text
local-zone: "example.com." always_nxdomain
```

Follow the Unbound procedure in [DEPLOYMENT.md](DEPLOYMENT.md#deploy-unbound-configuration).
Do not put the rule in a generated RPZ file.

## Refresh blocklists

| Field | Value |
|---|---|
| Execution location | Mini |
| Privilege | `sudo` REQUIRED |
| State | State-changing; downloads, atomically replaces zones, and reloads affected RPZ zones |
| Success | Exit 0; source and line count are reported; canary remains authoritative NXDOMAIN |
| Failure | Exit 1; the updater retains or rolls back to the prior zone and does not advance the success breadcrumb |

Refresh from the tracked selector file:

```sh
sudo /usr/local/libexec/unbound-stack/dns/update-blocklist.sh
```

Apply a one-time source override:

```sh
sudo /usr/local/libexec/unbound-stack/dns/update-blocklist.sh <source>
```

The explicit source argument is temporary. To change nightly behavior, edit and deploy
`config/unbound/blocklist-source`. Supported sources and validation floors are in
[REFERENCE.md](REFERENCE.md#blocklist-sources).

The interactive wrapper MAY be used instead:

```sh
sudo /usr/local/libexec/unbound-stack/dns/manage-blocklist.sh
```

## Warm the DNS cache

| Field | Value |
|---|---|
| Execution location | Mini |
| Privilege | `sudo` REQUIRED for control-socket statistics |
| State | State-changing only through normal DNS queries and cache population |
| Success | Script exits 0 and reports before/after cache statistics |
| Failure | Missing domain file, resolver probe failure, or control-socket error |

```sh
sudo /usr/local/libexec/unbound-stack/dns/warm-cache.zsh
```

Use this after an Unbound restart. It is not a substitute for diagnosing slow or failed
recursion.

## Build the personalized warm list

| Field | Value |
|---|---|
| Execution location | Mini |
| Privilege | `sudo` REQUIRED |
| State | State-changing; writes a privacy-sensitive local file and breadcrumb metric |
| Success | Reports counts for personal and curated domains |
| Failure | Control-socket error, empty usable cache, or unwritable destination |

```sh
sudo /usr/local/libexec/unbound-stack/dns/build-warm-list.sh
```

The output contains household behavior data. It MUST NOT be copied into the repository
or shared as diagnostic output.

## Restart or reload a service

Restarts are state-changing and can interrupt service. A restart MUST NOT be the first
diagnostic step. Prefer a component-specific reload when supported.

| Component | Location | Privilege | Command | Effect | Success | Failure |
|---|---|---|---|---|---|---|
| Unbound config | Mini | `sudo` | `sudo unbound-control reload` | Re-reads config and zones without a process restart | Control command succeeds and canaries pass | Control error or changed canary result |
| Unbound process | Mini | `sudo` | `sudo brew services restart unbound` | Process restart; drops cache | One process listens on port 53; canaries pass after warmup | No listener, duplicate process, or failed canary |
| Prometheus config/rules | Mini | `localadmin` | `pkill -HUP -x prometheus` | In-process reload; keeps TSDB hot | `prometheus_config_last_reload_successful == 1` | Reload metric is 0 or rule/target disappears |
| Prometheus process | Mini | `sudo` | `sudo launchctl kickstart -k system/local.unbound.prometheus` | Process restart | `/-/ready` succeeds | Readiness fails or process loops |
| Grafana | Mini | `sudo` | `sudo launchctl kickstart -k system/local.unbound.grafana` | Process restart | LAN `/api/health` reports database `ok` | Health fails, bind error, or process loops |
| node_exporter | Mini | `sudo` | `sudo launchctl kickstart -k system/local.unbound.nodeexporter` | Process restart | Target returns up | Target stays down or textfile scrape breaks |
| Unbound exporter | Mini | `sudo` | `sudo launchctl kickstart -k system/local.unbound.exporter` | Process restart | `unbound_up` is available | Socket permission or scrape failure |
| Blackbox exporter | Mini | `sudo` | `sudo launchctl kickstart -k system/local.unbound.blackbox` | Process restart | Three blackbox jobs recover | Exporter or probe jobs stay down |
| Periodic job | Mini | `sudo` | `sudo launchctl kickstart system/local.unbound.<name>` | Runs one job now | Last exit is 0 and output/breadcrumb updates | Nonzero exit, stale output, or log error |

Prometheus, Grafana, and node_exporter MUST NOT be started through `brew services`.
Unbound MUST NOT be started through non-root `brew services`.

## Screen Sharing

### Enable

| Field | Value |
|---|---|
| Execution location | Mini over SSH |
| Privilege | `sudo` REQUIRED |
| State | State-changing; enables and bootstraps Screen Sharing |
| Success | Helper reports port 5900 listening |
| Failure | Helper exits nonzero or port 5900 remains closed |

```sh
sudo /usr/local/sbin/screenshare on
```

### Disable

| Field | Value |
|---|---|
| Execution location | Mini over SSH |
| Privilege | `sudo` REQUIRED |
| State | State-changing; disables and boots out Screen Sharing |
| Success | Helper reports port 5900 closed |
| Failure | Port remains listening; investigate before ending the session |

```sh
sudo /usr/local/sbin/screenshare off
```

Screen Sharing SHOULD be disabled immediately after use.

## Back up runtime-only state

Repository content is recovered from Git. The following runtime-only items MAY be
backed up from an operator workstation when the mini is healthy.

| Item | Sensitivity | Need |
|---|---|---|
| ntfy topic | Secret | Required to preserve the existing phone channel |
| Personal warm list | Private household profile | Optional |
| Prometheus TSDB | Operational history | Optional |
| Grafana database | Contains credentials/sessions; not required for dashboards | Optional and sensitive |

Backup commands that read secrets SHOULD write directly to a protected local directory
and MUST NOT print contents. See [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md#preserve-runtime-only-state).

## Homebrew maintenance

| Field | Value |
|---|---|
| Execution location | Mini |
| Privilege | Homebrew as `localadmin`; `sudo` only for system LaunchDaemon restart and firewall changes |
| State | State-changing |
| Success | Updated component validates, runs from the new Cellar path, and remains reachable |
| Failure | Bind error, firewall timeout, validation error, duplicate service, or old process path |

After upgrading Prometheus or Grafana:

1. validate the current configuration;
2. restart the owning system LaunchDaemon onto the new binary;
3. add and unblock the resolved new Cellar binary with
   `/usr/libexec/ApplicationFirewall/socketfilterfw`;
4. verify LAN reachability and target health.

node_exporter also requires a LaunchDaemon restart after upgrade, but it is loopback-only
and does not need a firewall allow. Unbound upgrades MUST preserve the root-managed
`brew services` model and SHOULD be followed by config validation, restart, canary tests,
and cache warmup.

## Logs

| Component | Log or query |
|---|---|
| Unbound | `log show --predicate 'process == "unbound"' --last 1h` |
| Prometheus | `/opt/homebrew/var/log/prometheus.log` |
| Grafana | `/opt/homebrew/var/log/grafana/grafana.log`; early launch output in `/opt/homebrew/var/log/unbound-grafana-launchd.log` |
| node_exporter | `/opt/homebrew/var/log/node_exporter.log` |
| Unbound exporter | `/opt/homebrew/var/log/unbound-exporter.log` |
| Blackbox exporter | `/opt/homebrew/var/log/unbound-blackbox.log` |
| Alert notifier | `/opt/homebrew/var/log/unbound-alertnotify.log` |
| Client RPZ collector | `/opt/homebrew/var/log/unbound-clientstats.log` |
| Content cache collector | `/opt/homebrew/var/log/apple-content-cache-exporter.log` |
| Blocklist / warmlist / warmboot | `/opt/homebrew/var/log/unbound-{blocklist,warmlist,warmboot}.log` |
| Root hints | `/opt/homebrew/var/log/unbound-roothints.log` |

Reading logs is read-only and normally needs no `sudo`; unified log visibility MAY vary
by account. Scheduled-job logs are size-rotated by the tracked newsyslog configuration.
