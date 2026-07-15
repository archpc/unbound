# Prometheus-to-ntfy delivery hardening audit — 2026-07-15

## Scope and evidence vocabulary

This audit covers the repository and deployed path from Prometheus rule evaluation to
the operator device. It does not deploy, reload, restart, send a real notification,
read a real topic value, or claim phone receipt.

Evidence is classified as:

- **Observed repository:** configuration, source, scripts, plists, tests, or canonical
  documents in commit `33455d5` before this hardening patch.
- **Observed live:** sanitized, read-only evidence from the mini on 2026-07-15. Topic
  content, raw state fingerprints, notification bodies, and private label values were
  never returned to the workstation.
- **Verified by isolated test:** mocks, temporary private files, synthetic secrets, or
  loopback-only HTTP endpoints in `tests/test_prometheus_alert_notifier.py`.
- **Unverified:** requires deployment, the real ntfy service/topic, an operator device,
  or an external witness and was deliberately not exercised.

## Architecture and verified contract

| Stage | Contract | Evidence |
|---|---|---|
| Rule evaluation | Global evaluation interval is 15 seconds; 11 recording and 39 alert rules are loaded and healthy. | Observed repository and observed live `/api/v1/rules`; all four tracked Prometheus/notifier files matched live SHA-256. |
| Alert APIs | The notifier reads `/api/v1/alerts`, `/api/v1/rules?type=alert`, and `/api/v1/status/runtimeinfo`. Pending and firing entries are parsed independently; only firing entries are eligible to notify. | Repository implementation; isolated malformed/pending/firing tests. The live alerts API had zero active entries. |
| Freshness | Every alert-rule group must be nonempty, healthy, and evaluated within the larger of 60 seconds or four group intervals, measured against Prometheus `serverTime`. No fixed rule-count constant is trusted. | Isolated fresh/stale/missing/unhealthy/restart tests. |
| Identity | Exact sorted alert labels are HMAC-SHA-256 identified with a random per-state-file salt. Label order is irrelevant; any label change creates another identity. Raw identity labels are not state keys. | Isolated ordering, changed-label, privacy, migration, and state-file assertions. |
| State | State directory is 0700; JSON state and advisory lock are 0600. Writes use same-directory temporary files, file `fsync`, atomic replace, and directory `fsync`. Strict schema and keyed entry-integrity checks quarantine parseable structural corruption. | Repository implementation and isolated write/permission/corruption/cleanup tests; target-host validation remains required. |
| Transition detection | New firing identities create a durable pending intent before HTTP. Known firing identities suppress repeats. Missing identities require two complete dependency-qualified polls before resolution. | Isolated fire/repeat/resolve, restart, dependency, API failure, malformed entry, and crash-window tests. |
| Corrupt/missing state | The old state is privately quarantined when safe; current firing alerts become a no-replay baseline. This avoids restart/corruption storms and deliberately does not invent past delivery history. | Isolated missing/corrupt baseline tests. |
| ntfy request | Maximum three attempts per logical transition, 10-second request timeout, 1/2-second backoff, bounded `Retry-After`, and at most 10 transitions per run. Permanent 4xx responses other than 429 are blocked without repeated sends. | Isolated 200/202, 4xx, 429, 5xx, timeout/reset/DNS/TLS, rate-limit, and storm-bound tests. |
| Crash dedupe | A deterministic ntfy sequence ID is stored before HTTP. A crash retry uses the same sequence so supported clients update one notification. | Isolated pre-send and post-acceptance crash tests; real client behavior unverified. |
| Acceptance | HTTP 2xx increments server-acceptance metrics even if the body is malformed. Timeout/reset is recorded separately as acceptance-unknown. | Isolated HTTP endpoint and response-body tests. |
| Metrics | Process attempt, query success, complete state processing, resolution readiness, persistence, topic readiness, server acceptance, failures, ambiguity, staleness, and receipt-unknown are distinct metrics. | Repository implementation, exposition tests, rule/dashboard alignment. |
| Dashboard/alerts | `monitoring:notifier_local_path_healthy` requires every current local readiness gate, zero blocked transitions, and zero consecutive failures. Host diagnostics show attempt age, last server acceptance, processing gates, failure counters, and phone receipt UNKNOWN. | Repository rule/generator/generated JSON; live validation required after deployment. |
| Operator device | HTTP acceptance is not a delivery receipt. The local stack has no device acknowledgment. | Architectural fact; `unbound_ntfy_phone_receipt_known 0`; real acceptance test intentionally unverified. |

Observed live baseline before hardening:

- `local.unbound.alertnotify` was a 120-second system LaunchDaemon job as `localadmin`,
  not running between polls, after 2,471 runs with last exit 0.
- The tracked notifier, plist, Prometheus config, and rule file exactly matched runtime.
- Prometheus 3.13.1 reported 11 recording rules and 39 alert rules, all healthy and
  inactive; notifier query success was 1, firing count 0, delivery failures 0, state
  errors 0, and `monitoring:notifier_local_path_healthy` 1.
- The log contained two classified Prometheus-query errors and no raw ntfy URL,
  message-body marker, or topic-redaction prefix.
- The topic file was correctly `localadmin:staff` mode 0600. The state directory was
  0755 and the state file was 0644, verifying the private-state permission defect.
- Live rule-series inspection exposed label **keys only**. No label values were copied
  into this audit.

## Current 39-alert inventory and label populations

`alertname` is always present. Eleven rules add `severity`; 28 deliberately omit it.
Missing severity therefore remains a supported input and maps to high priority. The
potential dynamic keys below come from repository PromQL plus sanitized live series-key
inspection. Because all live alerts were inactive, active label values are unverified.

| Alerts | Potential dynamic label keys |
|---|---|
| `PrometheusTargetInventoryMismatch`, `BlackboxExporterDown`, `BlackboxProbeResultInventoryMismatch` | none |
| `NodeExporterDown`, `PrometheusConfigReloadFailed`, `PrometheusSelfScrapeDown` | `instance`, `job` |
| `PrometheusRuleEvaluationFailures` | `instance`, `job`, `rule_group` |
| `BlocklistUpdateStale`, `WarmlistMineStale`, `RootHintsRefreshStale`, `WarmbootDidNotRun`, `AlertNotifierStale`, `ClientStatsStale` | `exported_job`, `instance`, `job`, `task` |
| `BlocklistBreadcrumbAbsent`, `WarmlistBreadcrumbAbsent`, `RootHintsBreadcrumbAbsent`, `WarmbootBreadcrumbAbsent`, `BlocklistMinimumMetricAbsent`, `ContentCacheTextfileMissing`, `AlertNotifierBreadcrumbAbsent`, `ClientStatsBreadcrumbAbsent` | none |
| `BlocklistSuspiciouslySmall`, `TextfileScrapeError`, `NtfyDeliveryFailures`, `TimeMachineMonitoringUnhealthy`, `TimeMachineBackupStaleOrMissing` | `instance`, `job` |
| `ContentCacheExporterStale` | `file`, `instance`, `job` |
| `DiskAlmostFull` | `mountpoint` |
| `RootDiskFreePredictedLow`, `RootDiskFreePredictedCritical` | `device`, `fstype`, `instance`, `job`, `mountpoint` |
| `UpstreamDNSPathDown`, `UpstreamDNSPathDegraded` | none |
| `ResolverNotAnswering`, `RPZCanaryNotBlocking`, `GrafanaDown` | `instance`, `job` |
| `UnboundExporterDown`, `UnboundDown` | `instance`, `job` |
| `UnboundRequestListJammed`, `UnboundCacheHitRateLow` | none |

All exact labels participate in the private HMAC identity. Notification bodies admit
only `job`, `exported_job`, `instance`, `mountpoint`, `task`, `file`, `rule_group`, and
`fstype`. `file` becomes a basename; a non-root, non-system mountpoint becomes
`(non-root filesystem)`. `device`, client labels, arbitrary future labels, annotations,
and nonnumeric values never enter a notification.

## Compatibility findings

- No notifier runtime logic depended on the historical count of 29, old jobs/probes,
  dashboard UIDs, rule groups, `count(up)`, `count(probe_success)`, or the 11 recording
  rule results. The only stale `29` references are dated historical audit evidence or
  unrelated test fixtures.
- Pending alerts were already filtered from delivery and remain non-notifying.
- Missing severity was already accepted and remains high priority.
- The old notifier used `/api/v1/alerts`, not a PromQL `ALERTS` selector, so synthetic
  `ALERTS` five-minute stale markers were not directly consumed. Underlying input
  staleness and deliberately empty dependency expressions could still remove an active
  API alert and cause a false resolution; that defect is now explicitly gated.
- The redesigned dependency semantics are encoded as a resolution contract: inventory,
  self-scrape/rule, node/textfile, breadcrumb, blackbox, and Unbound-exporter blockers
  include pending as well as firing alerts. A test asserts every named blocker remains
  in the 39-rule inventory.
- A successful empty alerts list means genuine zero only while rule/runtime evidence is
  fresh and complete. A Prometheus restart holds resolutions for ten minutes, matching
  Prometheus's default restored-alert grace boundary.

## Verified defects ordered by operational impact

1. **False RESOLVED on unknown evidence.** A malformed alert was silently discarded;
   disappearance during dependency loss, rule staleness, or an early Prometheus restart
   could resolve a previously firing identity.
2. **Private label exposure.** Every label entered message bodies and raw fingerprints;
   the live state file was world-readable mode 0644.
3. **Crash and overlap duplication.** There was no process lock, no durable `fsync`, and
   HTTP acceptance occurred before the only state update, so crash retries could create
   another notification.
4. **Unbounded recovery amplification across runs.** There was no per-run event cap,
   permanent 4xx suppression, `Retry-After` support, or explicit timeout/reset ambiguity.
5. **Corruption storm.** Invalid state became an empty state and re-announced every
   currently firing alert.
6. **Misleading health semantics.** “sent/delivered” meant HTTP 2xx; process, query,
   state processing, persistence, server acceptance, and phone receipt were not fully
   distinguishable. The canonical record could remain green after an incomplete stage.
7. **Unsafe malformed-entry isolation.** One malformed entry did not crash the run, but
   filtering it out also made a known identity look resolved.

## Exact notifier state machine

| Input/state | Action | Durable result |
|---|---|---|
| Missing/invalid state | Create private salted schema; adopt current firing set without sending. | `observed_baseline`; state-reset/error counters advance. |
| Pending alert | Count and use as dependency evidence; never publish. | No alert transition. |
| New firing identity | Persist `pending_firing`; publish with stable sequence ID. | On 2xx: `firing_notified`; transient: pending with bounded next-attempt; permanent 4xx: `blocked_firing` and a nonzero blocked gauge. One logical failure is counted across retry cycles. |
| Repeated firing identity | Reset missing-poll count. | No HTTP; duplicate counter advances for the run. |
| Same labels in another order | HMAC canonicalization produces the same identity. | No HTTP. |
| Any label changes | Create a new firing identity; old identity begins qualified disappearance handling. | New firing can notify; old resolution waits independently. |
| API/rule/runtime unavailable or stale | Increment evidence-failure streak; after three failures publish one direct state-unavailable transition if possible. | Known alert states remain untouched; no resolution. Recovery rejection remains durable, unhealthy, and subject to the persisted `Retry-After`. |
| One malformed alert plus valid alerts | Valid new firing identities remain eligible; all resolutions are held. | Malformed count advances; after three incomplete polls direct state-unavailable transition is eligible. |
| Prometheus uptime under ten minutes | Firing additions allowed with fresh evidence; resolutions held. | No false restart resolution. |
| Missing alert with dependency blocker pending/firing | Hold and reset disappearance confirmation. | No resolution. |
| Missing alert, complete evidence, no blocker | Increment missing-poll count. | First poll holds; second poll persists `pending_resolved` and publishes default-priority update. |
| Resolution HTTP 2xx | Remove identity after durable post-acceptance save. | Exactly one normal resolution transition. |
| Crash after intent/before HTTP | Pending intent remains. | Next run retries same sequence ID. |
| Crash after possible acceptance/before commit | Pending intent remains; acceptance is ambiguous. | Next run retries same sequence ID; client update collapse is expected, transactional server exactly-once is not claimed. |
| State write failure | Do not start a new HTTP request; emit failure metric when possible. | Nonzero exit; stale metrics detect total writer death. |
| Overlapping process | Second process cannot acquire advisory lock. | It exits without polling or sending. |

## Failure matrix and results

| Area | Cases | Result |
|---|---|---|
| Prometheus transport | Connection refused, timeout, DNS/URL error, HTTP non-2xx | PASS, isolated |
| Prometheus payload | Invalid UTF-8, malformed/truncated JSON, bad envelope/shape, malformed individual alert | PASS, fail closed; valid sibling alert retained |
| Prometheus semantics | Pending/firing split, fresh empty list, zero firing, stale rule timestamp, unhealthy/missing rules, restart uptime, no fixed alert count | PASS, isolated |
| ntfy success | HTTP 200/202 and malformed response body | PASS; 2xx is server acceptance |
| ntfy permanent | 400/401/403/404 | PASS; one attempt, durable block, no repeated storm |
| ntfy rate/transient | 429 with short/long `Retry-After`, 500/502/503, timeout, reset | PASS; bounded retry/deferral and ambiguity counter |
| ntfy pre-connect | DNS failure, connection refusal, TLS verification failure | PASS; known non-acceptance and sanitized error |
| Local state | Missing/corrupt state, invalid metrics/timestamps, mode/atomic cleanup, unwritable state | PASS, isolated |
| Process lifecycle | First fire, repeat, two-poll resolution, changed labels, label order, restart/reboot-equivalent reload, two crash windows | PASS, isolated |
| Concurrency/rate | Advisory overlap lock, maximum ten total rule/direct transitions per run, permanent/transient suppression | PASS, isolated |
| Privacy | Synthetic topic, URL, body, annotation, private labels, mountpoint, file path, exception text, state and metrics | PASS; leak assertions |
| Metrics/dashboard | Metric types, server-acceptance alias, explicit receipt unknown, rule/dependency inventory alignment | PASS, repository tests; live post-deploy unverified |

The one independent post-implementation review found seven defects. The remediation
regressions verify outage-recovery rejection/deferral health, read-only dry-run,
complete legacy outage migration, parseable state corruption quarantine, helper
quiesce/automatic restoration, a shared ten-transition budget, and latched permanent
rejections counted once per logical transition. No repeated review loop was performed.

## Privacy and credential handling

- The topic is read only from a regular file owned by the notifier user with no
  group/other permission. No topic character is used in logs, test output, metrics,
  state, helper commands, diffs, or this report.
- Network exceptions print only an exception class and a generic endpoint-withheld
  message. HTTP errors print only the status class/code.
- Dry-run performs no state read, lock creation, permission repair, or quarantine and
  withholds bodies and labels. The real test-send path requires an
  explicit operator confirmation in the helper and reports “server accepted,” never
  “delivered.”
- The operator helper never reads or backs up the topic. Its rollback bundle is mode
  0700 and records state presence because schema rollback requires exact restoration.
  It quiesces the notifier job and automatically restores the pre-operation bundle on
  an install or validation error.
- Synthetic fixtures contain obvious fake secret strings and assert absence from logs,
  metrics, state, and notification bodies.

## Remaining limitations

- ntfy sequence IDs update one client notification, but ntfy's server cache is
  append-only. No local transaction spans state storage and remote HTTP, so a crash
  after remote acceptance cannot prove exactly one server event or exactly one device
  alert. Adding a local database or queue would not solve the remote transaction.
- HTTP 2xx cannot prove APNS/FCM delivery, phone connectivity, subscription state,
  notification permissions, or human receipt.
- Missing/corrupt state intentionally suppresses replay of currently active alerts.
  The operator must inspect local Prometheus alerts after a state reset.
- A permanent rejection stays blocked for that alert identity. Operator repair is
  required; a changed identity or later refire creates a new transition.
- Dependency resolution gating is explicit for the current 39-rule inventory. A new or
  materially changed alert rule must classify its evidence dependencies and update the
  contract test.
- Complete mini power loss, Prometheus/notifier/node_exporter co-failure, WAN loss that
  also blocks ntfy, and operator-device failure require an independent witness.

## External witness recommendation

Deploy one external witness outside the mini, LAN power, and residential WAN failure
domains. It should probe the externally meaningful DNS/service symptom and maintain a
dead-man heartbeat through a notification path independent of this local notifier.
The witness should alert on missed heartbeats and recoveries, while continuing to label
phone receipt as unknown unless the device supplies an acknowledgment. A scheduled
ntfy dead-man alone improves mini-death detection but still shares ntfy and the operator
device, so a second provider or managed monitor is preferred for the highest-confidence
witness.

## Prepared operator commands — not executed by this audit

Run from the exact clean committed checkout on the mini.

```sh
# Optional one real ntfy request; operator must separately confirm phone receipt.
CONFIRM_REAL_NTFY_SEND=YES \
  tools/ntfy-delivery-hardening-operator.sh acceptance

# Deploy after all documented validators pass.
backup="$HOME/monitoring/rollback/ntfy-hardening-$(date +%Y%m%d-%H%M%S)"
tools/ntfy-delivery-hardening-operator.sh deploy "$backup"

# Roll back with the exact backup path printed by deployment.
tools/ntfy-delivery-hardening-operator.sh rollback "$backup"

# Run after at least 130 seconds and two Grafana provider scans.
tools/ntfy-delivery-hardening-operator.sh verify
```

These modes were prepared and shell-validated only. None was run during this audit.
