# Grafana dashboard contribution rules

The five file-provisioned dashboards are operational interfaces, not metric catalogs.
Their deployed JSON lives under `config/grafana/dashboards/`; the deterministic source
is [tools/generate-grafana-dashboards.py](tools/generate-grafana-dashboards.py).
Capitalized requirement words use the RFC 8174 meanings defined in
[README.md](README.md#safety-contract).

## Source and deployment

- Change dashboard structure, PromQL, descriptions, navigation, or presentation in the
  generator, then run `python3 tools/generate-grafana-dashboards.py --write`.
- Generated JSON MUST match `--check` and MUST remain committed beside the generator.
- Every dashboard MUST keep `editable: false`; the provider MUST keep
  `allowUiUpdates: false` so a database-only edit cannot masquerade as repository state.
- Every Prometheus datasource reference MUST use UID `prometheus`.
- Dashboard and panel IDs MUST be unique. Stable retained UIDs SHOULD remain stable;
  new dashboards MUST use concise semantic UIDs.
- Grafana scans the directory every 30 seconds. Dashboard-only deployment MUST NOT
  restart Grafana.
- Removing a dashboard requires a backup of the watched JSON, removal from the watched
  directory and deployment manifest, and verification that Grafana deletes the
  provisioned resource. Git history alone is the canonical repository rollback source.

See [DEPLOYMENT.md](DEPLOYMENT.md#deploy-the-grafana-dashboard-set).

## Information architecture

| UID | Admission rule | Default range | Refresh |
|---|---|---:|---:|
| `operations-overview` | Current health, active failures, correlated failure history, and first diagnostic hop | 6 h | 30 s |
| `unbound-dns` | Resolver path, DNS workload/latency, cache pressure, and bounded RPZ client attribution | 6 h | 30 s |
| `host-services` | Shared host/resource paths, Time Machine, scheduled-producer freshness, and notifier diagnostics | 6 h | 30 s |
| `apple-content-cache` | Fresh Content Cache state, capacity, and source-complete traffic composition | 24 h | 5 m |
| `prometheus-monitoring` | Expected inventory, scrape paths, alerts, rule evaluation, ingestion, and TSDB diagnostics | 6 h | 30 s |

The first viewport of `operations-overview` MUST remain decisive. It owns verified
collection coverage, active alerts, Unbound control state, client-path DNS response,
upstream reachability, RPZ canary state, node_exporter, local notifier health, Time
Machine recency, Grafana health, and the explicit external-witness gap. Routine traffic
graphs belong in drill-down views.

## Panel admission

Every non-row panel MUST have a description and MUST answer an operator decision:

1. What state or trend is being shown?
2. What source produced it and how fresh is it?
3. What does missing data mean?
4. Which threshold or alert, if any, gives color operational meaning?
5. What should be inspected next when it is abnormal?

A panel MUST NOT be admitted only because a metric exists. Decorative totals,
duplicated resource graphs, generic `utun*` traffic mislabeled as Tailscale, transient
Time Machine mount rankings, and cumulative counters without an explicit cumulative
question are not dashboard content.

Rows MAY group related drill-down sections. Critical current state MUST NOT be hidden in
a collapsed row. Comparable panels SHOULD align and use consistent heights; arbitrary
gaps and walls of tiny stats are defects.

## Query and reducer rules

- Current-state stat and ranking panels MUST use instant queries. Their
  `lastNotNull` reducer therefore reduces one current result, not the last value from a
  six-hour range.
- Historical panels MUST preserve gaps (`spanNulls: false`). A stale textfile metric
  MUST be gated by its breadcrumb or `node_textfile_mtime_seconds` so node_exporter's
  retained sample does not draw a false continuous line.
- Counters MUST use `rate()` for activity or `increase()` for a bounded count. Raw
  counters MAY appear only when the title and description explicitly ask a cumulative
  since-reset question.
- Gauges, booleans, timestamps, and sampled rolling-window metrics MUST NOT be passed
  through `rate()`.
- Timestamp ages MUST use `time() - <timestamp>` and unit seconds.
- Histogram quantiles MUST aggregate bucket rates by `le` before
  `histogram_quantile()`. Summaries MUST NOT be aggregated across invalid populations.
- Ratios MUST use compatible numerator and denominator populations. Zero traffic MUST
  yield no ratio rather than a fabricated zero; the adjacent freshness/state panel must
  distinguish idle from missing monitoring.
- Label reduction MUST preserve the dimension used by the legend or operational
  question. Any vector match modifier MUST be explainable from the source labels.
- `or vector(0)` is allowed only when a successful query makes an empty population a
  genuine zero and a separate inventory/freshness signal prevents missing monitoring
  from appearing healthy.
- Repeated canonical expressions SHOULD be recording rules. Their comments MUST state
  source semantics, retained labels, evaluation interval, and missing-input behavior.

Canonical recording rules use explicit five- and 15-minute windows. Exploratory trend
panels use `$__rate_interval`; with the configured 15-second scrape interval Grafana
keeps the range at least 60 seconds and expands it for coarse long-range query steps.
Do not shorten a rate window to match dashboard refresh cadence.

## State and missing-data vocabulary

These states are distinct:

| State | Required treatment |
|---|---|
| Healthy | Green only after a current, freshness-qualified success condition |
| Degraded | Amber text/color tied to a documented warning, alert precursor, or engineering bound |
| Failed | Red text/color tied to a current failure condition |
| Unknown or stale | Gray/muted `UNKNOWN`, `STALE`, or an honest gap; never green zero |
| Disabled/not configured | Gray textual state such as `NOT CONFIGURED`; not failure or health |
| Not applicable | Textual `N/A` or a documented absent panel value |
| No activity | `IDLE` or `NO ACTIVITY`; only valid while collection freshness is independently healthy |

Every state panel MUST pair color with text. Missing state MUST remain distinguishable
from boolean zero. A datasource error is unknown monitoring state, not service failure.

## Visual and unit rules

- Stat panels are for decisive current values; state timelines are for discrete history;
  time series are for rates, latency, utilization, and trends; horizontal bar gauges are
  for bounded rankings.
- Pie charts and gauges without a meaningful fixed range MUST NOT be used.
- Stacking is permitted only for additive parts of one displayed whole. Dual axes SHOULD
  be avoided.
- Ordinary measurements use neutral palette colors. Green is reserved for verified
  healthy state, amber for degradation, red for active failure, and gray for unknown,
  disabled, or not applicable.
- Boolean values MUST map to text. Percentages use `percentunit`; latency uses seconds;
  byte quantities/rates use `bytes`/`Bps`; event rates use `ops`; ages use seconds.
- Precision MUST reflect source cadence. Query rates do not need thousandths; ratios
  normally use one decimal percentage point; state and counts normally use integers.
- Legends MUST expose only meaningful bounded dimensions such as job, instance, rcode,
  client, or cache category. Table legends are justified only when the summary value
  aids comparison.

## Threshold contract

Every severity threshold MUST cite one of:

- the matching Prometheus alert;
- a configured system limit;
- a measured operating baseline;
- an explicit engineering bound documented in the panel and audit.

Current justified dashboard thresholds are:

| Signal | Threshold | Basis |
|---|---:|---|
| Expected targets | exact per-job counts and exactly 8 total | Static configured inventory |
| Upstream success | red below 95% over 10 m; amber partial; green near 100% | `UpstreamDNSPathDegraded` plus verified probe history |
| Requestlist depth | red at 100 | `UnboundRequestListJammed` |
| Cache hit ratio | red below 50% only above 1 lookup/s | `UnboundCacheHitRateLow` |
| Root used | red at 90% | `DiskAlmostFull` |
| Producer freshness | per-producer alert age | LaunchDaemon cadence and matching alert |
| Time Machine latest backup | amber at 6 h, red at 24 h | Observed hourly cadence; documented engineering judgment |
| Blocklist size | red below 100% of selected source floor | Source-aware updater validation |
| Notifier server non-acceptances | red at 2 in 30 m | `NtfyDeliveryFailures`; timeout/reset ambiguity is shown separately and neither series proves phone receipt |

Do not add severity colors to uptime, byte totals, TSDB counts, cache use, CPU, memory,
or ordinary network variation without an alert, limit, SLO, or evidence-based bound.

## Variables and navigation

Only `unbound-dns` currently has a variable. `client` is a multi-select/All filter over
the bounded rolling client metric and MUST interpolate as `${client:regex}` with
`allValue: .*`. Validation MUST cover one, multiple, All, and no-match selections.

Every dashboard MUST link to the other four dashboards while preserving the time range.
Panel links SHOULD point to the most specific drill-down or Prometheus alerts view.
Links MUST NOT include secrets or raw private client values.

The repository has no reliable deployment/restart event series. Dashboard annotations
MUST remain empty until a trustworthy event producer exists; inferred restarts or
heuristic annotations MUST NOT be fabricated.

## Alert alignment

- `monitoring:target_inventory_ok`, `monitoring:collection_path_healthy`,
  `dns:upstream_best_success_ratio_10m`, and
  `unbound:cache_hit_ratio_15m` are canonical shared definitions.
- A dashboard indicator MUST NOT map to healthy while its corresponding alert
  expression is true. It MAY show amber during an alert's `for` hold or warning window.
- Blackbox scrape health and `probe_success` MUST be shown separately. Exporter failure
  makes probe results unknown and fires `BlackboxExporterDown`.
- node_exporter is the shared failure path for host and all textfile metrics; its current
  state MUST precede individual producer diagnosis.
- Every important alert MUST be visible through the overview alert count/history and
  the Prometheus alerts link.

## Validation

Before deployment:

```sh
python3 tools/generate-grafana-dashboards.py --check
python3 -m unittest discover -s tests -p 'test_*.py'
tools/validate-repository.sh
```

On a target or isolated staging host, before any Prometheus reload or plist bootstrap:

```sh
/opt/homebrew/opt/prometheus/bin/promtool check config <staged-prometheus.yml>
/opt/homebrew/opt/prometheus/bin/promtool check rules <staged-prometheus.rules.yml>
/usr/local/sbin/blackbox_exporter --config.file=<staged-blackbox.yml> --config.check
plutil -lint <staged-plist>
python3 tools/validate-dashboard-promql.py \
  --dashboard-dir <staged-dashboard-directory> \
  --rules <staged-prometheus.rules.yml>
```

After provisioning, verify the five UIDs, populated variables, panel query errors,
missing-state text, units, legends, links, first-viewport hierarchy, laptop width, and a
narrower viewport. Screenshots prove layout only; they do not replace semantic or live
query validation.
