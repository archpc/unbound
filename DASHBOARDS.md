# Grafana Dashboard Conventions

This repo has no UI component library — but the **7 Grafana dashboards are the
operational design system** for the Mac mini. They are how state is presented, what
counts as healthy vs unhealthy, and how failures are read at a glance. This file is the
explicit ruleset so the suite stays consistent instead of drifting into a junk-drawer of
one-off panels. New panels and new dashboards should follow these rules; if you need to
break one, say why in the commit.

The dashboard JSON under [grafana-etc/dashboards/](grafana-etc/dashboards/) is the
**source of truth**. See [RUNBOOK.md](RUNBOOK.md) to *use* the dashboards and
[CLAUDE.md](CLAUDE.md) for the monitoring-stack internals.

## 0. The golden rule: don't edit in the Grafana UI

Dashboards are **file-provisioned**. UI edits do **not** write back to the JSON — they
live only in Grafana's DB and vanish on the next provisioning reload or restart. That's
silent drift: the UI looks changed, the repo disagrees, and the repo eventually wins.

- Every dashboard sets **`"editable": false`** to enforce this. Keep it that way.
- To change a dashboard: edit the JSON here → deploy → commit. Never the reverse.
- Deploy: `scp` the file to `/opt/homebrew/etc/grafana/dashboards/` on the mini (that dir
  is `localadmin`-writable, no sudo). Grafana's file provisioner rescans on an interval —
  no restart. Bump the dashboard's top-level `version` on every change.
- Validate before deploy: `python3 -m json.tool <file> >/dev/null`.

## 1. One dashboard per operational question

The suite is split by the question an operator asks, not by exporter or metric source.
Keep it that way — do not let a dashboard absorb panels outside its charter.

| Dashboard | UID | Admission rule — ONLY panels that answer… |
|---|---|---|
| Unbound DNS | `unbound-dns` | resolver correctness, DNSSEC, RPZ/ad-blocking, cache behavior, query flow, upstream/probe health |
| Apple Content Cache | `apple-content-cache` | content-cache status, counters, rates, exporter health |
| Storage / Backups | `storage-backups` | disk capacity, backup-volume behavior, free-space prediction, filesystem state |
| System Stats | `system-stats` | host CPU, memory, load, swap, uptime — basic machine health only |
| Services | `services` | scrape-target liveness, scheduled-job breadcrumb ages, daemon health |
| Prometheus / Monitoring | `prometheus-monitoring` | scrape health, rule eval, TSDB size/series, Prometheus internals |
| Network / Tailscale | `network-tailscale` | interface throughput, errors, drops, utun/Tailscale visibility |

UIDs are stable and kebab-case — never change a UID (it breaks links and provisioning identity).

## 2. Datasource

- Every panel references the datasource by **UID `prometheus`** — never by name, never a
  hardcoded URL. This is what makes the JSON portable across Grafana instances.
- If a second datasource is ever added, give it an explicit UID and role; don't rely on "default".

## 3. Tags

Every dashboard carries, at minimum, **`mac-mini`** and **`homelab`**, plus topic tags
(`dns`, `storage`, `prometheus`, …). Order: topic tags first, then `mac-mini`, then
`homelab`. Consistent tags are the suite's search/filter affordance.

## 4. Units (the design tokens) — standard per use

Every field sets a unit. Pick by what the metric *is*, not by what looks close:

| Metric kind | Unit | Examples |
|---|---|---|
| Raw counts / dimensionless | `short` | query counts, series counts, load average |
| Storage & memory sizes | `bytes` | cache used, disk free, TSDB size, memory |
| Byte throughput (per second) | `Bps` | en0/utun throughput, swap activity |
| DNS request/query rates | `reqps` | query rate, rcode rates, prefetch rate |
| Ratios already in 0–1 form | `percentunit` | cache-hit fraction, CPU busy, disk used |
| Durations, ages, latencies | `s` | scrape/answer/recursion time, job ages, uptime |
| Multi-day ages | `d` | "days until full" |
| 0/1 boolean state | `none` | Resolver up, Upstream path, RPZ canary |

Two known, deliberate exceptions: `Root disk burn rate (GiB/day)` uses `none` because the
query pre-scales to GiB and there is no native GiB/day unit (the title carries it); 0/1
health booleans use `none`.

## 5. Scales

- **`percentunit` fields set `min: 0, max: 1`.** This applies to *both* timeseries (axis
  bounds) and stat panels (proportional threshold fill). A percentage can't exceed 100%,
  so pin the scale — don't let it autorange.
- `bytes` / rate panels autoscale from the data (no forced min/max) — that's correct.

## 6. Thresholds & color — color is a state language

Green = healthy, yellow/orange = warning, red = bad/failed, **neutral (gray) =
informational, no judgment**. Apply thresholds only where a value has a health meaning:

| Panel purpose | Color rule |
|---|---|
| Health / liveness (0/1) | red@base, green@1 (up = green) |
| Capacity / usage (%) | green → orange → red at meaningful cutoffs |
| Freshness / age | green, red past the "too old" threshold |
| Error / failure counts | green, red at ≥1 (or a tolerated floor) |
| **Raw informational counts** | **neutral: `color.mode = "fixed"`, `fixedColor = "text"`** |

> **Why counts get an explicit neutral color, not "nothing":** a Grafana stat panel with
> no color config defaults to `thresholds` mode with a **green base step** — so "leave it
> alone" renders a raw count *green*, falsely implying "more is good." `fixed`/`text`
> forces true neutral. Applies to DNS queries (24h), ads blocked (24h), cache hits/misses
> (24h) — counts with no inherent good/bad.

## 7. Refresh cadence

Refresh should match how fast the underlying data actually changes — uniform where the
data is live, slower where the exporter is slow. Don't blindly standardize to one value.

| Dashboard kind | Refresh |
|---|---|
| Live DNS / services / network / host | `30s` |
| Backed by a slow scheduled exporter | match the exporter, e.g. `apple-content-cache` = `1m` (its exporter writes every 5 min; faster refresh shows nothing newer) |

## 8. Time range

| Dashboard kind | Default window |
|---|---|
| Operational health (most boards) | `now-6h` |
| Storage / backups / trends | `now-7d` (disk growth & backup mounts need the longer view) |

## 9. Panel descriptions — add only where the title can't carry it

Titles are the primary documentation and should be self-explanatory (`Upstream path
(:53)`, `Warmboot after boot (negative = missed)`). Add a `description` **only** when the
title cannot convey a caveat or interpretation — specifically for panels involving:

- predictive / regression math (disk free-space prediction),
- series that are **expected** to be empty sometimes (Time Machine volume only mounts
  during backups; utun series when idle),
- non-obvious PromQL, reset epochs, or "negative = missed" / freshness semantics,
- interpretation that needs external context (which utun is Tailscale — by routes).

Don't spray descriptions on self-evident panels; that's noise.

## 10. Checklist for a new dashboard or panel

- [ ] Belongs to exactly one operational question (§1); UID kebab-case, stable.
- [ ] `"editable": false`; `version` bumped on change.
- [ ] Datasource = UID `prometheus`; tags include `mac-mini` + `homelab`.
- [ ] Unit chosen per §4; `percentunit` gets `min:0/max:1` (§5).
- [ ] Thresholds only where health-meaningful; raw counts neutral `fixed`/`text` (§6).
- [ ] Refresh matches data cadence (§7); time range per §8.
- [ ] Description only if the title can't carry the meaning (§9).
- [ ] `python3 -m json.tool` clean → deploy → verify checksum → commit.
