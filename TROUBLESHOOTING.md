# Troubleshooting

Use this guide by symptom. Run verification before applying a fix. Capitalized
requirement words use the RFC 8174 meanings defined in
[README.md](README.md#safety-contract).

## Resolver times out

| Field | Value |
|---|---|
| Symptom | LAN clients report DNS timeouts or use the external fallback |
| Signal | Direct query to `192.168.0.100` times out |
| Noise | A brief boot-time first-exit followed by a stable KeepAlive process |
| Likely causes | Unbound stopped, port 53 not bound, host/interface unavailable, firewall, duplicate service conflict |
| Execution | LAN client first, then mini over SSH |
| Privilege/state | Read-only, no `sudo`; DNS queries generate ordinary traffic |

Verification:

```sh
dig @192.168.0.100 +time=2 +tries=1 cloudflare.com A | grep 'status:'
launchctl print system/homebrew.mxcl.unbound | grep -E 'state|runs|last exit'
lsof -nP -iUDP:53 -iTCP:53
```

Expected result: `NOERROR`, one Unbound process, and listeners on loopback and
`192.168.0.100`. More than one Unbound process is not healthy.

Smallest safe fix: if the process is absent, run `unbound-checkconf` first. Only after it
passes, restart with `sudo brew services restart unbound`, verify the canaries, and warm
the cache. If two processes exist, remove the unintended non-root `brew services` copy;
do not kill processes blindly.

Escalation criteria: config does not validate, the process repeatedly exits, port 53 is
occupied by another program, or the host cannot bind `192.168.0.100`.

## DNS is intermittently slow

| Field | Value |
|---|---|
| Symptom | Cached names are fast but some new names stall for hundreds of milliseconds or seconds |
| Signal | Public DNS probes lose packets; recursion time rises; cache hits remain fast |
| Noise | A single normal cache miss is slower than a cache hit |
| Likely causes | WAN loss, router/ISP path, UDP/53 loss, authoritative-server retries; less often host saturation |
| Execution | Mini |
| Privilege/state | Read-only, no `sudo`; active probes send network traffic |

Verification:

```sh
dig @127.0.0.1 +time=2 +tries=1 cloudflare.com A | grep 'Query time:'
dig @1.1.1.1 +time=2 +tries=1 cloudflare.com A | grep -E 'status:|Query time:'
ping -q -c 20 192.168.0.1
ping -q -c 20 1.1.1.1
curl -sG --data-urlencode 'query=max(avg_over_time(probe_success{job="blackbox_upstream"}[10m]))' \
  http://127.0.0.1:9090/api/v1/query
```

Expected result: gateway loss is zero, public-path loss is negligible, and the query
expression is at least `0.95`.

Smallest safe fix: when the gateway is clean but the public path loses packets, inspect
or restart WAN equipment and contact the ISP. Unbound configuration MUST NOT be changed
to mask external packet loss.

Escalation criteria: gateway loss is nonzero, request-list depth remains above 100, the
host is resource-saturated, or failures persist from another wired device.

## Unbound returns SERVFAIL

| Field | Value |
|---|---|
| Symptom | Queries return `SERVFAIL` |
| Signal | One domain or many domains fail through Unbound |
| Noise | `dnssec-failed.org` MUST return SERVFAIL |
| Likely causes | Broken upstream path, genuinely bogus domain DNSSEC, root trust issue |
| Execution | Mini |
| Privilege/state | Read-only, no `sudo` |

Verification:

```sh
dig @127.0.0.1 +time=2 +tries=1 example.com A | grep 'status:'
dig @1.1.1.1 +time=2 +tries=1 example.com A | grep 'status:'
dig @1.1.1.1 +cd +time=2 +tries=1 example.com A | grep 'status:'
```

Expected result: healthy signed domains resolve through both resolvers. A domain that
works only with `+cd` has DNSSEC trouble at the domain, not a local policy defect.

Smallest safe fix: do not disable DNSSEC. Wait for or report the domain's DNSSEC repair.
If direct public DNS also fails for many names, follow [DNS is intermittently slow](#dns-is-intermittently-slow).

Escalation criteria: many domains fail while direct public DNS succeeds, the trust
anchor is missing, or Unbound logs validator errors across unrelated zones.

## One website is blocked

| Field | Value |
|---|---|
| Symptom | One external domain returns NXDOMAIN |
| Signal | NXDOMAIN with the `aa` flag indicates local RPZ synthesis |
| Noise | NXDOMAIN without `aa` is the ordinary authoritative result |
| Likely causes | Generated RPZ match; explicit local block |
| Execution | Diagnose on mini; edit in repository first |
| Privilege/state | Diagnosis read-only; allowlist deployment changes policy |

Verification:

```sh
dig @127.0.0.1 example.com A | grep -E 'status:|flags:'
```

Expected result: a false positive is `NXDOMAIN` with `aa`.

Smallest safe fix: add bare and wildcard `rpz-passthru.` records to the tracked
allowlist, increment its SOA serial, validate, deploy, and reload only `hagezi.allow`.
Follow [DEPLOYMENT.md](DEPLOYMENT.md#deploy-the-rpz-allowlist).

Escalation criteria: the name is still blocked after a successful allowlist reload, the
SOA or zone syntax is invalid, or the response is not authoritative NXDOMAIN.

## RPZ blocking stopped

| Field | Value |
|---|---|
| Symptom | `RPZCanaryNotBlocking` fires or the canary resolves normally |
| Signal | `google-analytics.com` is not authoritative NXDOMAIN |
| Noise | Client bypass to an external resolver is invisible to the server-side canary |
| Likely causes | Missing/unloaded zone, failed update, config drift, allowlist mistake |
| Execution | Mini |
| Privilege/state | Verification read-only; updater is state-changing and requires `sudo` |

Verification:

```sh
dig @127.0.0.1 google-analytics.com A | grep -E 'status:|flags:'
wc -l /opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz
tail -50 /opt/homebrew/var/log/unbound-blocklist.log
```

Expected result: authoritative NXDOMAIN; the blocklist contains `CNAME` records and
meets its selected source floor.

Smallest safe fix: run the updater only if the log or zone evidence shows stale or bad
generated data. The updater validates and rolls back on failure. Do not hand-edit the
generated zone.

Escalation criteria: `unbound-checkconf` fails, both current and backup zonefiles are
invalid, or the allowlist unexpectedly contains the canary.

## Local `.lan` name is wrong

| Field | Value |
|---|---|
| Symptom | A `.lan` name returns NXDOMAIN or the wrong address |
| Signal | The static forward/reverse entry disagrees with the router reservation |
| Noise | An unlisted `.lan` name intentionally returns NXDOMAIN |
| Likely causes | Stale hand-copied reservation or missing paired record |
| Execution | Verify router externally; edit repository first |
| Privilege/state | Queries read-only; deployment changes DNS state |

Verification:

```sh
dig @127.0.0.1 mini.lan A +short
dig @127.0.0.1 -x 192.168.0.100 +short
```

Expected result: forward and reverse answers agree with `unbound.conf` and the router.

Smallest safe fix: change both `local-data` and `local-data-ptr`, validate, deploy, and
reload Unbound. The repository cannot validate router state.

Escalation criteria: the router reservation cannot be confirmed or changing the record
would affect an unknown client.

## Grafana is unavailable

| Field | Value |
|---|---|
| Symptom | `http://192.168.0.100:3000` hangs or `GrafanaDown` fires |
| Signal | LAN health request fails while Prometheus remains available |
| Noise | `127.0.0.1:3000` fails by design because Grafana is LAN-bound only |
| Likely causes | Process down, bind failure, application-firewall allow lost after upgrade |
| Execution | Mini plus LAN browser/client |
| Privilege/state | Initial checks read-only |

Verification:

```sh
launchctl print system/local.unbound.grafana | grep -E 'state|runs|last exit'
curl -s http://192.168.0.100:3000/api/health
curl -sG --data-urlencode 'query=probe_success{job="blackbox_grafana"}' \
  http://127.0.0.1:9090/api/v1/query
tail -100 /opt/homebrew/var/log/grafana/grafana.log
```

Expected result: running job, database `ok`, and probe value `1`. A missing probe result
with blackbox scrape targets down is a blackbox-exporter fault, not evidence that Grafana
itself failed.

Smallest safe fix: after a Homebrew upgrade, authorize the resolved new Cellar binary
in the macOS application firewall, then test again. Restart Grafana only when process or
bind evidence requires it.

Escalation criteria: database migration errors, repeated crash loop, or LAN failure
after a verified firewall allow.

## Prometheus target or textfile is unhealthy

| Field | Value |
|---|---|
| Symptom | A target is down, a breadcrumb alert fires, or `TextfileScrapeError` fires |
| Signal | Target API, job log, textfile mtime, and node_exporter metric disagree with expected state |
| Noise | A periodic launchd job normally shows `state = not running` between executions |
| Likely causes | Missing dependency, failed scheduled run, malformed metric, stale retained file |
| Execution | Mini |
| Privilege/state | Initial checks read-only |

Verification:

```sh
curl -s http://127.0.0.1:9090/api/v1/targets
curl -s http://127.0.0.1:9100/metrics | grep '^node_textfile_scrape_error'
launchctl print system/local.unbound.<job> | grep -E 'state|runs|last exit'
tail -100 /opt/homebrew/var/log/<job-log>
```

Expected result: scrape target up, textfile error zero, most recent job exit zero, and a
fresh breadcrumb or mtime.

Smallest safe fix: correct the producer's specific failure and kickstart only that job.
Deleting a textfile merely hides evidence and SHOULD NOT be the first response.

Escalation criteria: multiple unrelated producers fail, the textfile directory is not
writable by the intended identity, or node_exporter rejects a shared metric family.

## Phone notifications do not arrive

| Field | Value |
|---|---|
| Symptom | Prometheus has firing alerts but the phone receives nothing |
| Signal | Notifier metrics, state metadata, and sanitized log categories show which local stage failed |
| Noise | Pending alerts are intentionally not sent; duplicates are suppressed |
| Likely causes | Alert still pending, notifier stale, Prometheus unavailable, ntfy failure, phone subscription issue |
| Execution | Mini; phone for final confirmation |
| Privilege/state | Metrics/log checks read-only; `--test-send` sends a real notification |

Verification:

```sh
curl -s http://127.0.0.1:9090/api/v1/alerts
curl -s http://127.0.0.1:9100/metrics | grep '^unbound_ntfy_'
stat -f '%Sp %Su:%Sg %N' \
  /Users/localadmin/monitoring/state \
  /Users/localadmin/monitoring/state/notified-alerts.json \
  /Users/localadmin/monitoring/ntfy.topic
tail -100 /opt/homebrew/var/log/unbound-alertnotify.log
```

Expected result: query, state processing, resolution safety, persistence, and topic
readiness gauges are one; consecutive failures are zero; the firing/pending counts match
the valid alerts API entries after the next 120-second poll. The state directory is mode
0700; state and topic files are mode 0600. `unbound_ntfy_phone_receipt_known` remains
zero by design. A recent server-acceptance timestamp proves only ntfy HTTP 2xx.

Smallest safe fix: correct the evidenced local stage. Run `--selftest` first; it is
offline. If transport and device receipt must be tested, run `--test-send`
intentionally. It sends one real push, withholds the topic, reports only server
acceptance, and still requires the operator to confirm phone receipt. Do not print or
copy the topic into a command line.

Escalation criteria: repeated server non-acceptance, unknown timeout/reset outcomes,
missing or unsafe topic metadata, state/persistence errors, suppressed resolutions that
cannot regain complete evidence, or an ntfy-accepted test that never reaches the
subscribed device.

## Time Machine is stale or unknown

| Field | Value |
|---|---|
| Symptom | `TimeMachineMonitoringUnhealthy` or `TimeMachineBackupStaleOrMissing` fires; dashboard shows UNKNOWN or old backup age |
| Signal | Collector mtime, privacy-preserving `time_machine_*` gauges, launchd exit, and direct read-only `tmutil` disagree |
| Noise | A periodic job is normally not running; an unconfigured destination is disabled, not a collector failure |
| Likely causes | No configured destination, target unavailable, Full Disk Access/TCC denial under launchd, failed backup, stale collector, or changed tmutil output |
| Execution | Mini; initial checks read-only |
| Privilege/state | `localadmin`; kickstart is state-changing and requires explicit intent |

Verification:

```sh
launchctl print system/local.unbound.timemachine | grep -E 'state|runs|last exit'
curl -s http://127.0.0.1:9100/metrics | grep -E '^time_machine_|time-machine.prom'
tmutil status
tmutil latestbackup
tail -100 /opt/homebrew/var/log/time-machine-exporter.log
```

Do not paste `tmutil destinationinfo` or `latestbackup` output into reports: it can contain
private destination identifiers and paths. Expected metric behavior is:

- no destination: `destination_configured 0`, collection success `1`, no timestamp;
- configured and readable: collection success `1`, availability `1`, and a timestamp;
- configured but unreadable/failed parsing: collection success `0`, availability `0` or
  omitted state, and a generic log error with no path.

Smallest safe fix: resolve the evidenced destination/TCC/backup failure, then kickstart
only `local.unbound.timemachine` if a fresh sample is required. Do not delete the
textfile to make the alert disappear.

Escalation criteria: `tmutil` succeeds interactively but the real LaunchDaemon fails,
the latest backup remains over six hours old under the documented hourly schedule, or
the collector would need to expose a private path to diagnose the issue.
