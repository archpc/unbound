# Technical reference

This document is the tabular reference for the intended repository state. Runtime state
MUST be verified on the mini before it is described as current. Capitalized requirement
words use the RFC 8174 meanings defined in [README.md](README.md#safety-contract).

## Host and network

| Item | Value | Evidence |
|---|---|---|
| Host | `mini.lan` | Static Unbound record |
| Ethernet address | `192.168.0.100` | `unbound.conf`, Prometheus and Grafana binds |
| LAN | `192.168.0.0/24` | Unbound access control |
| Router | `router.lan`, `192.168.0.1` | Static Unbound record |
| DNS transport | IPv4 UDP and TCP | `do-ip4: yes`, `do-ip6: no` |
| Remote access | SSH; Tailscale is external to this repository | sshd fragment and operational evidence |
| Router DNS fallback | Reported as `9.9.9.9`; not configured here | Router MUST be checked directly |

### Listening ports

| Port | Bind | Component | Exposure |
|---:|---|---|---|
| 22 | macOS sshd configuration | SSH | LAN and any OS-managed remote interfaces |
| 53 | `127.0.0.1`, `192.168.0.100` | Unbound, UDP/TCP | Loopback and Ethernet LAN |
| 3000 | `192.168.0.100` | Grafana | Ethernet LAN only |
| 9090 | `127.0.0.1`, `192.168.0.100` | Prometheus | Loopback and Ethernet LAN |
| 9100 | `127.0.0.1` | node_exporter | Loopback only |
| 9115 | `127.0.0.1` | blackbox_exporter | Loopback only |
| 9167 | `127.0.0.1` | unbound_exporter | Loopback only |
| 5900 | Disabled by default | Screen Sharing | On demand only |

SMB port 445 and the local Kerberos KDC port 88 are not components of this repository
and SHOULD remain closed unless a separately documented requirement exists.

## DNS records and policy

The `lan.` zone is static. An unlisted `*.lan` name returns NXDOMAIN.

| Name | Type | Value |
|---|---|---|
| `mini.lan.` | A | `192.168.0.100` |
| `router.lan.` | A | `192.168.0.1` |
| `k1-0217.lan.` | A | `192.168.0.200` |
| `df-4q0ts74.lan.` | A | `192.168.0.222` |

The reverse entries map the same four IPv4 addresses back to the names above. Forward
and reverse records MUST be changed together.

| Policy order | Zone | Runtime file | Action |
|---:|---|---|---|
| 1 | `hagezi.allow` | `/opt/homebrew/etc/unbound/blocklists/hagezi-allow.rpz` | `rpz-passthru` exceptions |
| 2 | `hagezi.rpz` | `/opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz` | Forced NXDOMAIN |

| Canary | Expected result | Meaning |
|---|---|---|
| `cloudflare.com` | `NOERROR` with `ad` | Recursion and DNSSEC validation work |
| `google-analytics.com` | `NXDOMAIN` with `aa` | RPZ blocking works |
| `dnssec-failed.org` | `SERVFAIL` | Bogus DNSSEC is rejected |
| `mini.lan` | `192.168.0.100` | Static local zone works |

## Unbound parameters

| Area | Setting |
|---|---|
| Modules | `respip validator iterator` |
| Control | Certless Unix socket `/opt/homebrew/var/run/unbound-control.sock` |
| Logging | syslog, verbosity 1, query/reply logging off, extended statistics on |
| Cache | message 128 MiB; rrset 256 MiB; negative 4 MiB |
| TTL | minimum 60 s; maximum 86,400 s; negative minimum 5 s |
| Stale data | enabled; stale TTL 86,400 s; reply TTL 30 s; client timeout 0 |
| Prefetch | response and DNSKEY prefetch enabled |
| Concurrency | four threads; 8,192 outgoing range; 4,096 queries per thread |
| Rate limits | 1,000 global responses/s; 500 responses/s per client IP |
| Interfaces excluded | IPv6 transport, Wi-Fi, and Tailscale |

## Blocklist sources

The selector file currently contains `hagezi-normal`. An explicit command-line source is
a one-time override and does not rewrite the selector file.

| Source | Download endpoint | Minimum accepted lines | Approximate sample size in script comments |
|---|---|---:|---:|
| `hagezi-normal` | jsDelivr HaGeZi Multi RPZ | 100,000 | 316k |
| `hagezi-pro` | jsDelivr HaGeZi Pro RPZ | 300,000 | 464k |
| `1hosts-lite` | 1Hosts Lite RPZ on GitHub | 130,000 | 199k |
| `1hosts-xtra` | 1Hosts Xtra RPZ on GitHub | 900,000 | 1,423k |
| `oisd-small` | `small.oisd.nl/rpz` | 70,000 | 111k |
| `oisd-big` | `big.oisd.nl/rpz` | 600,000 | 987k |
Downloads MUST contain `CNAME` records and meet
the source-specific floor before replacement.

## Services and scheduled jobs

Jobs with no `UserName` run as root. `homebrew.mxcl.unbound` is managed by root
`brew services`; the other labels are system LaunchDaemons.

| Label | Identity | Type or schedule | Program | Log |
|---|---|---|---|---|
| `homebrew.mxcl.unbound` | root, then drops to `unbound` | KeepAlive, boot | `/opt/homebrew/opt/unbound/sbin/unbound` | macOS unified log |
| `local.unbound.exporter` | root | KeepAlive, boot | `/usr/local/sbin/unbound_exporter` | `/opt/homebrew/var/log/unbound-exporter.log` |
| `local.unbound.blackbox` | `localadmin` | KeepAlive, boot | `/usr/local/sbin/blackbox_exporter` | `/opt/homebrew/var/log/unbound-blackbox.log` |
| `local.unbound.prometheus` | `localadmin` | KeepAlive, boot | Homebrew service wrapper | `/opt/homebrew/var/log/prometheus.log` |
| `local.unbound.nodeexporter` | `localadmin` | KeepAlive, boot | Homebrew service wrapper | `/opt/homebrew/var/log/node_exporter.log` |
| `local.unbound.grafana` | `localadmin` | KeepAlive, boot | `/opt/homebrew/opt/grafana/bin/grafana` | Grafana file log plus launch log |
| `local.unbound.blocklist` | root | Daily 03:30 | `update-blocklist.sh` | `/opt/homebrew/var/log/unbound-blocklist.log` |
| `local.unbound.warmlist` | root | Daily 23:30 | `build-warm-list.sh` | `/opt/homebrew/var/log/unbound-warmlist.log` |
| `local.unbound.warmboot` | root | Once at boot | `warm-at-boot.sh` | `/opt/homebrew/var/log/unbound-warmboot.log` |
| `local.unbound.roothints` | root | Monthly, day 1 at 03:00 | `unbound-refresh-hints` | `/var/log/unbound-refresh-hints.log` |
| `local.unbound.contentcache` | `localadmin` | Every 300 s and at load | `apple_content_cache_exporter.py` | `/opt/homebrew/var/log/apple-content-cache-exporter.log` |
| `local.unbound.clientstats` | `localadmin` | Every 120 s and at load | `unbound_client_rpz_exporter.py` | `/opt/homebrew/var/log/unbound-clientstats.log` |
| `local.unbound.alertnotify` | `localadmin` | Every 120 s and at load | `prometheus_alert_notifier.py` | `/opt/homebrew/var/log/unbound-alertnotify.log` |

Grafana's file log is `/opt/homebrew/var/log/grafana/grafana.log`; early process output
uses `/opt/homebrew/var/log/unbound-grafana-launchd.log`. The tracked newsyslog file
rotates scheduled-job logs at 1 MiB and retains five compressed generations. It does not
rotate logs held open by KeepAlive daemons.

## Prometheus jobs

Global scrape and rule-evaluation intervals are 15 seconds.

| Job | Interval | Target instances | Purpose |
|---|---:|---|---|
| `unbound` | 15 s | `127.0.0.1:9167` | Unbound control statistics |
| `prometheus` | 15 s | `127.0.0.1:9090` | Prometheus self-monitoring |
| `node` | 15 s | `127.0.0.1:9100` | Host and textfile metrics |
| `grafana` | 15 s | `192.168.0.100:3000` | Grafana `/metrics` health |
| `blackbox_upstream` | 30 s | `1.1.1.1`, `8.8.8.8` | Direct public UDP/53 path |
| `blackbox_resolver` | 30 s | `192.168.0.100` | Hot-name Unbound response |
| `blackbox_rpz_canary` | 30 s | `192.168.0.100` | RPZ NXDOMAIN canary |

Prometheus retains its TSDB for 90 days under `/opt/homebrew/var/prometheus`.

## Alerts

All firing alerts are eligible for ntfy delivery. `critical` maps to urgent priority;
`warning` and missing severity map to high. The notifier sends resolutions at default
priority. Alerts that diagnose notifier or ntfy failure may be visible only in
Prometheus because the delivery path is the failed component.

| Group | Alert | Trigger summary | Hold |
|---|---|---|---:|
| automation | `BlocklistUpdateStale` | Blocklist success older than 26 h | none |
| automation | `BlocklistBreadcrumbAbsent` | Blocklist breadcrumb absent | 15 m |
| automation | `WarmlistMineStale` | Warm-list success older than 26 h | none |
| automation | `WarmlistBreadcrumbAbsent` | Warm-list breadcrumb absent | 15 m |
| automation | `RootHintsRefreshStale` | Root-hints success older than 34 d | none |
| automation | `RootHintsBreadcrumbAbsent` | Root-hints breadcrumb absent | 15 m |
| automation | `WarmbootDidNotRun` | Warmboot timestamp predates boot | 15 m |
| automation | `WarmbootBreadcrumbAbsent` | Warmboot breadcrumb absent | 15 m |
| automation | `BlocklistSuspiciouslySmall` | Any emitted blocklist has fewer than 100k lines | none |
| automation | `ContentCacheExporterStale` | Content-cache textfile older than 15 m | none |
| automation | `ContentCacheTextfileMissing` | Content-cache textfile series absent | 15 m |
| automation | `TextfileScrapeError` | node_exporter reports a textfile parse error | none |
| automation | `AlertNotifierStale` | Notifier success older than 10 m | none |
| automation | `AlertNotifierBreadcrumbAbsent` | Notifier breadcrumb absent | 15 m |
| automation | `ClientStatsStale` | Clientstats success older than 15 m | none |
| automation | `ClientStatsBreadcrumbAbsent` | Clientstats breadcrumb absent | 15 m |
| automation | `NtfyDeliveryFailures` | At least two failed pushes in 30 m | none |
| host | `DiskAlmostFull` | Any APFS/HFS filesystem has less than 10% free | 30 m |
| host | `RootDiskFreePredictedLow` | Root predicted below 10 GiB free in seven days | 30 m |
| host | `RootDiskFreePredictedCritical` | Root predicted below 5 GiB free in 24 h | 15 m |
| probes | `UpstreamDNSPathDown` | Both public DNS probes currently fail | 5 m |
| probes | `UpstreamDNSPathDegraded` | Even the healthier public probe is below 95% success over 10 m | 5 m |
| probes | `ResolverNotAnswering` | Resolver probe fails | 5 m |
| probes | `RPZCanaryNotBlocking` | RPZ canary does not return NXDOMAIN | 30 m |
| probes | `GrafanaDown` | Grafana scrape fails | 5 m |
| unbound | `UnboundExporterDown` | Exporter target is down | 5 m |
| unbound | `UnboundDown` | Exporter cannot query Unbound | 2 m |
| unbound | `UnboundRequestListJammed` | Request-list depth exceeds 100 | 10 m |
| unbound | `UnboundCacheHitRateLow` | Hit rate below 50% under more than 1 query/s | 30 m |

## Custom metric families

| Producer | Metric family | Semantics |
|---|---|---|
| All scheduled jobs except content cache | `unbound_job_last_success_timestamp_seconds{task=...}` | Unix timestamp of last successful complete run |
| Blocklist updater | `unbound_blocklist_lines` | Current generated blocklist line count |
| Warm-list builder | `unbound_warmlist_domains{kind=...}` | Personal and curated domain counts |
| Root-hints updater | `unbound_roothints_records` | Count of root-server records |
| Clientstats | `unbound_client_rpz_blocks_5m{client=...}` | Rolling five-minute RPZ blocks per permitted client |
| Clientstats | `unbound_client_last_rpz_block_seconds{client=...}` | Newest block timestamp in current window |
| Clientstats | `unbound_client_rpz_unattributed_5m` | Internal RPZ actions without client address |
| Clientstats | `unbound_client_rpz_parse_errors_5m` | Unparseable RPZ log entries |
| Alert notifier | `unbound_ntfy_last_attempt_timestamp_seconds` | Last notifier run attempt |
| Alert notifier | `unbound_ntfy_last_failure_timestamp_seconds` | Most recent delivery failure |
| Alert notifier | `unbound_ntfy_prometheus_query_success` | Current Prometheus poll status |
| Alert notifier | `unbound_ntfy_firing_alerts` | Firing alert count seen in current poll |
| Alert notifier | `unbound_ntfy_suppressed_duplicates` | Already-announced alerts in current poll |
| Alert notifier | `unbound_ntfy_notifications_sent_total` | Persistent successful-push counter |
| Alert notifier | `unbound_ntfy_delivery_failures_total` | Persistent failed-push counter |
| Alert notifier | `unbound_ntfy_state_errors_total` | Persistent notifier-state error counter |
| Content cache | `apple_content_cache_active`, `apple_content_cache_activated`, `apple_content_cache_status_ok` | Service state gauges |
| Content cache | `apple_content_cache_*_bytes` | Cache capacity and use gauges |
| Content cache | `apple_content_cache_*_bytes_total` | Cumulative origin/client byte counters when reset epoch is known |
| Content cache | `apple_content_cache_totals_since_seconds` | Counter reset epoch |
| Content cache | `apple_content_cache_detail_bytes{category=...}` | Cache use by category |

`unbound_exporter`, node_exporter, blackbox_exporter, Prometheus, and Grafana expose
additional upstream metric schemas. Those binaries are not vendored here; their live
`/metrics` endpoints and the PromQL in dashboards/rules are the schema evidence.

## Dashboards

| File | UID | Title | Panels | Refresh | Default range |
|---|---|---|---:|---:|---:|
| `apple-content-cache.json` | `apple-content-cache` | Apple Content Cache | 19 | 1 m | 6 h |
| `network-tailscale.json` | `network-tailscale` | Network / Tailscale | 6 | 30 s | 6 h |
| `prometheus-monitoring.json` | `prometheus-monitoring` | Prometheus / Monitoring | 9 | 30 s | 6 h |
| `services.json` | `services` | Services | 9 | 30 s | 6 h |
| `storage-backups.json` | `storage-backups` | Storage / Backups | 11 | 30 s | 7 d |
| `system-stats.json` | `system-stats` | System Stats | 10 | 30 s | 6 h |
| `unbound-dns.json` | `unbound-dns` | Unbound DNS | 29 | 30 s | 6 h |

All dashboard files are immutable in the UI (`editable: false`) and reference the
Prometheus datasource by UID `prometheus`.

## Runtime data and sensitive state

| Data | Path | Repository policy |
|---|---|---|
| Unbound config | `/opt/homebrew/etc/unbound/unbound.conf` | SHA-256 comparison |
| Generated RPZ zones | `/opt/homebrew/etc/unbound/blocklists/*.rpz` | Generated zones checked for existence; allowlist tracked separately |
| Prometheus config/rules | `/opt/homebrew/etc/prometheus.yml`, `/opt/homebrew/etc/prometheus.rules.yml` | SHA-256 comparison |
| Prometheus TSDB | `/opt/homebrew/var/prometheus` | Runtime only |
| Grafana config/dashboards | `/opt/homebrew/etc/grafana/` | SHA-256 comparison |
| Grafana database | `/opt/homebrew/var/lib/grafana/grafana.db` | Sensitive runtime state; optional backup |
| Textfiles | `/opt/homebrew/var/lib/node_exporter/textfile/*.prom` | Generated; existence only |
| Notifier state | `/Users/localadmin/monitoring/state/notified-alerts.json` | Runtime only |
| ntfy topic | `/Users/localadmin/monitoring/ntfy.topic` | Secret; existence only; MUST NOT be read by repository tooling |
| Personal warm list | `/Users/localadmin/warmup/dns-warm-domains.personal.txt` | Private household profile; optional; MUST NOT be committed |
| Root trust anchor | `/opt/homebrew/var/lib/unbound/root.key` | Managed runtime state |

## External dependencies

| Dependency | Role | Managed here? |
|---|---|---|
| macOS and launchd | Host and service manager | No |
| Homebrew under `/opt/homebrew` | Unbound, Prometheus, Grafana, node_exporter, Go | No |
| `unbound_exporter` and `blackbox_exporter` Go modules | Exporter binaries under `/usr/local/sbin` | Build instructions only; versions are not pinned |
| Router DHCP/reservations | Supplies resolver addresses and keeps static names aligned | No |
| DNS root and authoritative servers | Iterative recursion | No |
| InterNIC root hints | Monthly bootstrap hints source | URL embedded in script |
| HaGeZi, 1Hosts, OISD | RPZ sources | URLs and validation floors embedded in script |
| ntfy.sh | External phone notification transport | No |
| Apple Content Caching and `AssetCacheManagerUtil` | Native macOS cache and status interface | No |
| Tailscale app/System Extension | Remote access and MagicDNS | No |
| macOS application firewall | LAN reachability for versioned binaries | No |
