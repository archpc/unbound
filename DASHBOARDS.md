# Grafana dashboard contribution rules

The seven file-provisioned dashboards are operational interfaces. Their JSON under
`grafana-etc/dashboards/` is authoritative. Capitalized requirement words use the RFC
8174 meanings defined in [README.md](README.md#safety-contract).

## Source and deployment

- Dashboards MUST be changed in repository JSON, not in Grafana's UI.
- Every dashboard MUST keep `editable: false`.
- UI changes do not write back to JSON and MAY be replaced by provisioning.
- The top-level dashboard `version` SHOULD increase when content changes.
- JSON MUST parse before deployment.
- The provider scans every 30 seconds; Grafana MUST NOT be restarted merely to load a
  dashboard file.
- Deployment MUST preserve the dashboard UID and verify the live checksum.

See [DEPLOYMENT.md](DEPLOYMENT.md#deploy-a-grafana-dashboard).

## Dashboard ownership

Each panel MUST answer the question owned by exactly one dashboard.

| Dashboard | UID | Admission rule |
|---|---|---|
| Unbound DNS | `unbound-dns` | Resolver correctness, DNSSEC, RPZ, cache, query flow, recursion and probes |
| Apple Content Cache | `apple-content-cache` | Native content-cache state, capacity, counters, rates, exporter health |
| Storage / Backups | `storage-backups` | Filesystem capacity, backup-volume behavior, free-space trends |
| System Stats | `system-stats` | CPU, memory, load, swap and host uptime |
| Services | `services` | Scrape liveness and scheduled-job freshness |
| Prometheus / Monitoring | `prometheus-monitoring` | Scrapes, rule evaluation, TSDB and Prometheus internals |
| Network / Tailscale | `network-tailscale` | Interface traffic, errors, drops and `utun` visibility |

UIDs are stable interfaces and MUST NOT be renamed casually.

## Datasource and metadata

- Panels MUST reference datasource UID `prometheus`, not a datasource name or URL.
- Every dashboard MUST include topic tags followed by `mac-mini` and `homelab`.
- The default window SHOULD be six hours, except storage/trend dashboards SHOULD use
  seven days.
- Live dashboards SHOULD refresh every 30 seconds. Slow-exporter dashboards MAY refresh
  more slowly; Apple Content Cache uses one minute for its five-minute producer.

## Units

| Metric meaning | Grafana unit |
|---|---|
| Raw counts and dimensionless values | `short` |
| Storage and memory | `bytes` |
| Byte rate | `Bps` |
| DNS request rate | `reqps` |
| Ratio in the range 0–1 | `percentunit` with `min: 0`, `max: 1` |
| Duration, latency, age, uptime | `s` |
| Multi-day age | `d` |
| Boolean 0/1 state | `none` |

A title MAY carry a pre-scaled unit when Grafana has no suitable native unit, such as
GiB/day.

## Color semantics

| Meaning | Color |
|---|---|
| Healthy | Green |
| Warning | Yellow or orange |
| Failed or unsafe | Red |
| Informational without judgment | Neutral text |

Raw counts MUST use a neutral fixed text color unless a documented threshold gives the
value operational meaning. Grafana's implicit green base MUST NOT turn an informational
count into a false health signal.

## Titles and descriptions

Titles SHOULD explain the operational question. Add a description only for a caveat
that the title cannot carry, including:

- predictive or regression math;
- a series expected to disappear, such as an unmounted backup volume;
- reset epochs, freshness semantics, or non-obvious PromQL;
- interface interpretation that depends on external routing state.

Descriptions MUST state what a panel does not prove when a healthy-looking signal can
mask a failure. The resolver probe, for example, does not prove fresh recursion because
cache and serve-expired can answer it.

## Review checklist

- [ ] Panel belongs to one dashboard's admission rule.
- [ ] UID is unchanged; dashboard remains non-editable.
- [ ] Datasource UID is `prometheus`.
- [ ] Tags, unit, bounds, thresholds, refresh, and time range follow this document.
- [ ] Informational counts are neutral.
- [ ] Caveats and empty-series behavior are documented where necessary.
- [ ] JSON parses and `tools/check-docs.py` passes.
- [ ] Deployment follows [DEPLOYMENT.md](DEPLOYMENT.md#deploy-a-grafana-dashboard).
