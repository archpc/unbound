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
| `local.unbound.roothints` | root | Monthly, day 1 at 03:00 | `refresh-root-hints.sh` | `/opt/homebrew/var/log/unbound-roothints.log` |
| `local.unbound.contentcache` | `localadmin` | Every 300 s and at load | `apple_content_cache_exporter.py` | `/opt/homebrew/var/log/apple-content-cache-exporter.log` |
| `local.unbound.clientstats` | `localadmin` | Every 120 s and at load | `unbound_client_rpz_exporter.py` | `/opt/homebrew/var/log/unbound-clientstats.log` |
| `local.unbound.timemachine` | `localadmin` | Every 900 s and at load | `time_machine_exporter.py` | `/opt/homebrew/var/log/time-machine-exporter.log` |
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
| `blackbox_upstream` | 30 s | `1.1.1.1`, `8.8.8.8` | Direct public UDP/53 path |
| `blackbox_resolver` | 30 s | `192.168.0.100` | Hot-name Unbound response |
| `blackbox_rpz_canary` | 30 s | `192.168.0.100` | RPZ NXDOMAIN canary |
| `blackbox_grafana` | 30 s | `http://192.168.0.100:3000/api/health` | HTTP 200 plus Grafana database `ok` |

Prometheus retains its TSDB for 90 days under `/opt/homebrew/var/prometheus`.

The rules file contains 11 recording rules and 39 alerts. Recording rules evaluate at
the global 15-second interval and are listed by exact name in the rules file.

| Recording rule | Purpose | Missing-input behavior |
|---|---|---|
| `monitoring:target_inventory_ok` | Exact per-job and total target cardinality | Missing jobs become explicit mismatch |
| `monitoring:targets_down` | Down targets only when inventory is exact | Empty while inventory is mismatched |
| `monitoring:active_alerts` | Current firing-alert count | Zero only while Prometheus evaluates the rule |
| `monitoring:collection_path_healthy` | Core scrape and inventory health | Empty if the canonical inputs disappear |
| `monitoring:textfile_path_healthy` | node_exporter plus textfile parse health | Empty if node scrape state disappears |
| `monitoring:notifier_local_path_healthy` | Fresh complete notifier processing, resolution safety, persistence, topic readiness, zero blocked transitions, and no current run failure | Empty if any required notifier input disappears; never proves a recent ntfy request or phone receipt |
| `dns:upstream_best_success_ratio_10m` | Better of two public probe paths | Empty unless both result series exist |
| `unbound:cache_hit_ratio_15m` | Hit ratio above one cache lookup/s | Empty during low traffic or missing input |
| `apple_content_cache:local_hit_ratio_24h` | Local-cache bytes / all client bytes | Empty at zero traffic or unavailable source |
| `apple_content_cache:origin_fraction_24h` | Origin bytes / all client bytes | Empty at zero traffic or unavailable source |
| `apple_content_cache:other_source_fraction_24h` | Parent-plus-peer bytes / all client bytes | Empty at zero traffic or unavailable source |

## Alerts

All firing alerts are eligible for ntfy delivery. Pending alerts are observed but never
published. `critical` maps to urgent priority; `warning` and missing severity map to
high. Resolutions use default priority after two complete, dependency-qualified polls.
Notification bodies include only the explicit `job`, `exported_job`, `instance`,
`mountpoint`, `task`, `file`, `rule_group`, and `fstype` label allowlist; `file` is
reduced to a basename and non-system/non-root mountpoints are generalized. Alert
annotations and all other labels remain local. Alerts
that diagnose notifier or ntfy failure may be visible only in Prometheus because the
delivery path is the failed component.

| Group | Alert | Trigger summary | Hold |
|---|---|---|---:|
| monitoring | `PrometheusTargetInventoryMismatch` | Per-job cardinalities or eight-target total differ from provisioning | 5 m |
| monitoring | `NodeExporterDown` | Shared host/textfile exporter target is down | 5 m |
| monitoring | `BlackboxExporterDown` | One or more blackbox scrape paths are down | 5 m |
| monitoring | `BlackboxProbeResultInventoryMismatch` | Probe result series differ from the four configured jobs | 5 m |
| monitoring | `PrometheusConfigReloadFailed` | Last Prometheus reload failed | 5 m |
| monitoring | `PrometheusSelfScrapeDown` | Prometheus cannot scrape its own endpoint | 5 m |
| monitoring | `PrometheusRuleEvaluationFailures` | Rule evaluation failures increased in five minutes | none |
| automation | `BlocklistUpdateStale` | Blocklist success older than 26 h | none |
| automation | `BlocklistBreadcrumbAbsent` | Blocklist breadcrumb absent | 15 m |
| automation | `WarmlistMineStale` | Warm-list success older than 26 h | none |
| automation | `WarmlistBreadcrumbAbsent` | Warm-list breadcrumb absent | 15 m |
| automation | `RootHintsRefreshStale` | Root-hints success older than 34 d | none |
| automation | `RootHintsBreadcrumbAbsent` | Root-hints breadcrumb absent | 15 m |
| automation | `WarmbootDidNotRun` | Warmboot timestamp predates boot | 15 m |
| automation | `WarmbootBreadcrumbAbsent` | Warmboot breadcrumb absent | 15 m |
| automation | `BlocklistSuspiciouslySmall` | Deployed list is below its selected source floor | none |
| automation | `BlocklistMinimumMetricAbsent` | Source-floor metric is absent | 15 m |
| automation | `ContentCacheExporterStale` | Content-cache textfile older than 15 m | none |
| automation | `ContentCacheTextfileMissing` | Content-cache textfile series absent | 15 m |
| automation | `TextfileScrapeError` | node_exporter reports a textfile parse error | none |
| automation | `AlertNotifierStale` | Notifier success older than 10 m | none |
| automation | `AlertNotifierBreadcrumbAbsent` | Notifier breadcrumb absent | 15 m |
| automation | `ClientStatsStale` | Clientstats success older than 15 m | none |
| automation | `ClientStatsBreadcrumbAbsent` | Clientstats breadcrumb absent | 15 m |
| automation | `NtfyDeliveryFailures` | At least two failed pushes in 30 m | none |
| automation | `TimeMachineMonitoringUnhealthy` | Collector file/metric is stale, absent, or failed | 15 m |
| automation | `TimeMachineBackupStaleOrMissing` | Configured destination has no backup newer than 6 h | 30 m |
| host | `DiskAlmostFull` | Any APFS/HFS filesystem has less than 10% free | 30 m |
| host | `RootDiskFreePredictedLow` | Root predicted below 10 GiB free in seven days | 30 m |
| host | `RootDiskFreePredictedCritical` | Root predicted below 5 GiB free in 24 h | 15 m |
| probes | `UpstreamDNSPathDown` | Both public DNS probes currently fail | 5 m |
| probes | `UpstreamDNSPathDegraded` | Even the healthier public probe is below 95% success over 10 m | 5 m |
| probes | `ResolverNotAnswering` | Resolver probe fails | 5 m |
| probes | `RPZCanaryNotBlocking` | RPZ canary does not return NXDOMAIN | 30 m |
| probes | `GrafanaDown` | Grafana `/api/health` HTTP/database probe fails | 5 m |
| unbound | `UnboundExporterDown` | Exporter target is down | 5 m |
| unbound | `UnboundDown` | Exporter cannot query Unbound | 2 m |
| unbound | `UnboundRequestListJammed` | Request-list depth exceeds 100 | 10 m |
| unbound | `UnboundCacheHitRateLow` | Hit rate below 50% under more than 1 query/s | 30 m |

## Custom metric families

| Producer | Metric family | Semantics |
|---|---|---|
| All scheduled jobs except content cache | `unbound_job_last_success_timestamp_seconds{task=...}` | Unix timestamp of last successful complete run |
| Blocklist updater | `unbound_blocklist_lines`, `unbound_blocklist_minimum_lines` | Current line count and the selected source's validation floor |
| Warm-list builder | `unbound_warmlist_domains{kind=...}` | Personal and curated domain counts |
| Root-hints updater | `unbound_roothints_records` | Count of root-server records |
| Clientstats | `unbound_client_rpz_blocks_5m{client=...}` | Rolling five-minute RPZ blocks per permitted client |
| Clientstats | `unbound_client_last_rpz_block_seconds{client=...}` | Newest block timestamp in current window |
| Clientstats | `unbound_client_rpz_unattributed_5m` | Internal RPZ actions without client address |
| Clientstats | `unbound_client_rpz_parse_errors_5m` | Unparseable RPZ log entries |
| Alert notifier | `unbound_ntfy_last_attempt_timestamp_seconds` | Last notifier process run, regardless of outcome |
| Alert notifier | `unbound_ntfy_last_failure_timestamp_seconds` | Most recent query, state, topic, or delivery failure |
| Alert notifier | `unbound_ntfy_last_server_acceptance_timestamp_seconds` | Most recent ntfy HTTP 2xx; phone receipt remains unknown |
| Alert notifier | `unbound_ntfy_prometheus_query_success` | Whether the alerts API envelope parsed this run |
| Alert notifier | `unbound_ntfy_alert_state_processed`, `unbound_ntfy_resolutions_enabled` | Whether rule/runtime evidence was fresh and whether restart/dependency safety permitted resolutions |
| Alert notifier | `unbound_ntfy_state_persistence_success`, `unbound_ntfy_topic_ready` | Current durable-state and credential-file readiness |
| Alert notifier | `unbound_ntfy_blocked_transitions` | Current transitions latched after permanent ntfy rejection; must be zero for local-path health |
| Alert notifier | `unbound_ntfy_phone_receipt_known` | Always zero: the architecture has no operator-device receipt |
| Alert notifier | `unbound_ntfy_firing_alerts`, `unbound_ntfy_pending_alerts`, `unbound_ntfy_invalid_alerts` | Current valid firing/pending and isolated malformed alert counts |
| Alert notifier | `unbound_ntfy_suppressed_duplicates`, `unbound_ntfy_resolutions_suppressed` | Current dedupe and safety-held resolution counts |
| Alert notifier | `unbound_ntfy_server_acceptances_total` | Persistent ntfy HTTP-acceptance counter; not phone delivery |
| Alert notifier | `unbound_ntfy_notifications_sent_total` | Deprecated compatibility alias of server acceptances |
| Alert notifier | `unbound_ntfy_delivery_failures_total`, `unbound_ntfy_delivery_outcome_unknown_total` | Distinct logical non-acceptances (one count across bounded retry cycles) and timeout/reset ambiguity |
| Alert notifier | `unbound_ntfy_state_errors_total`, `unbound_ntfy_state_write_errors_total`, `unbound_ntfy_state_resets_total` | State validation, durable-write, and no-replay baseline counters |
| Alert notifier | `unbound_ntfy_retry_attempts_total`, `unbound_ntfy_permanent_rejections_total`, `unbound_ntfy_consecutive_failures` | Bounded retry, permanent rejection, and current failure-streak state |
| Content cache | `apple_content_cache_active`, `apple_content_cache_activated`, `apple_content_cache_status_ok` | Service state gauges |
| Content cache | `apple_content_cache_*_bytes` | Cache capacity and use gauges |
| Content cache | `apple_content_cache_*_bytes_total` | Cumulative origin/client byte counters when reset epoch is known |
| Content cache | `apple_content_cache_totals_since_seconds` | Counter reset epoch |
| Content cache | `apple_content_cache_detail_bytes{category=...}` | Cache use by category |
| Content cache | `apple_content_cache_cache_to_client_bytes_total`, `apple_content_cache_origin_to_client_bytes_total`, `apple_content_cache_total_to_client_bytes_total` | Authoritative Metrics.db client traffic since reset; omitted when the reset exceeds Apple's guaranteed 30-day retention |
| Content cache | `apple_content_cache_cache_to_client_last_24h_bytes`, `apple_content_cache_origin_to_client_last_24h_bytes`, `apple_content_cache_total_to_client_last_24h_bytes` | Rolling 24-hour Metrics.db client traffic gauges |
| Content cache | `apple_content_cache_traffic_metrics_available`, `apple_content_cache_since_reset_traffic_complete` | Metrics.db readability/schema and since-reset completeness gauges |
| Time Machine | `time_machine_destination_configured` | Whether a destination is configured; no destination identifier is labeled |
| Time Machine | `time_machine_backup_running` | Current tmutil running state |
| Time Machine | `time_machine_latest_backup_available`, `time_machine_latest_backup_timestamp_seconds` | Latest completed-backup availability and Unix timestamp; `tmutil latestbackup -t` with a completed `SnapshotDates` fallback obtained in memory through the macOS `defaults` preference service |
| Time Machine | `time_machine_exporter_collection_success` | Whether all required read-only Time Machine state was collected |

`unbound_exporter`, node_exporter, blackbox_exporter, Prometheus, and Grafana expose
additional upstream metric schemas. Those binaries are not vendored here; their live
`/metrics` endpoints and the PromQL in dashboards/rules are the schema evidence.

## Dashboards

| File | UID | Title | Panels | Refresh | Default range |
|---|---|---|---:|---:|---:|
| `operations-overview.json` | `operations-overview` | Operational Overview | 15 | 30 s | 6 h |
| `unbound-dns.json` | `unbound-dns` | DNS and Unbound | 20 | 30 s | 6 h |
| `host-services.json` | `host-services` | Host, Services, and Backups | 23 | 30 s | 6 h |
| `apple-content-cache.json` | `apple-content-cache` | Apple Content Cache | 15 | 5 m | 24 h |
| `prometheus-monitoring.json` | `prometheus-monitoring` | Prometheus Diagnostics | 14 | 30 s | 6 h |

The five dashboards contain 87 panel objects, including 14 open row headers and 73
data/text panels. All are immutable in the UI (`editable: false`), the provider rejects
UI saves, and every query references the Prometheus datasource UID `prometheus`.

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
| Notifier state and lock | `/Users/localadmin/monitoring/state/{notified-alerts.json,notified-alerts.lock}` | Runtime only; directory mode 0700 and files mode 0600; alert identities are salted HMACs and transition entries carry keyed integrity checks rather than trusting raw labels or arbitrary JSON |
| ntfy topic | `/Users/localadmin/monitoring/ntfy.topic` | Secret; existence only; MUST NOT be read by repository tooling |
| Personal warm list | `/Users/localadmin/warmup/dns-warm-domains.personal.txt` | `localadmin:staff` mode 0600; private household profile; optional; MUST NOT be committed |
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
