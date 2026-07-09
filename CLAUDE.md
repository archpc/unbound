# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Configuration and operational scripts for **Unbound**, a recursive DNS resolver running as a Homebrew
service on a Mac mini (`mini.lan`, `192.168.0.100`). It serves DNS for the local LAN (`192.168.0.0/24`)
plus loopback, with DNSSEC validation, ad/tracker blocking via RPZ, and a home `.lan` zone. This is not
an application codebase — there is no build, lint, or test suite. Changes here take effect by editing
config/scripts in place and reloading the live `unbound` process.

**Resolver topology:** clients get `192.168.0.100` as primary DNS with `9.9.9.9` (Quad9) as
fallback (confirmed 2026-07-09 from the mini's own `scutil --dns`; the DHCP setting itself
lives on the router — verify there). The fallback keeps internet name resolution alive during
a mini outage, but it bypasses RPZ ad-blocking and `.lan` names — and clients with loose
resolver-selection (Windows notably) may drift to `9.9.9.9` and stick even while unbound is
healthy, silently unblocked. Dashboards can't see that: the RPZ canary proves unbound blocks,
not that every client still asks unbound.

For a human, task-oriented operator guide (how to log in, health-check, unblock a site,
recover from an outage), see [RUNBOOK.md](RUNBOOK.md). This file is the deep technical
reference behind it.

## Workflow rules for multi-issue fix requests

Codified 2026-07-08 after a 10-item review turned into ~50 tool calls and nearly two
hours of wall-clock time on a task that should have been a single pass — mostly from
per-item edit→verify loops and redundant image-based PDF checks where text extraction
had already answered the question. When given a batch of issues to fix (a review, an
audit, a list of inconsistencies):

1. **Do not commit** unless explicitly asked, every time — no exceptions for "this
   seems like the kind of thing that gets committed."
2. **Do not make cosmetic edits** (line-wrap tidying, rewording that doesn't fix a
   reported issue) bundled into a fix pass — they're free to skip and not free to verify.
3. **Gather facts first, then stop.** Do whatever live/read verification is needed to
   confirm the disputed facts, then show the intended patch (or a precise description
   of it) plus a verification plan, and wait for approval before editing anything.
4. **After approval, make all edits in one pass** — don't interleave edit → verify →
   edit → verify per item. Batch the edits, then verify once at the end.
5. **Regenerate derived artifacts (PDFs, builds, etc.) once**, after all edits land —
   not once per edit.
6. **Verify with text extraction / grep first.** For a PDF, `pdftotext` + `grep` is the
   default verification tool.
7. **Render images only for explicitly visual bugs** (layout, color, clipping,
   spacing) — never to re-confirm something text extraction already answered.
8. **Stop after one verification pass** and show `git status` + `git diff --stat` (not
   full diffs unless asked) — then wait, don't keep re-checking.
9. **SSH success does not prove launchd success.** macOS TCC evaluates the responsible
   process: a command that works in an interactive SSH session can be denied (Full Disk
   Access etc.) when spawned by a LaunchDaemon — this exact trap killed the Time Machine
   exporter (2026-07-08: `tmutil listbackups` fine over SSH, rc=80 under launchd). For any
   job touching TCC-gated resources, the only real verification is `launchctl kickstart`
   plus checking the daemon's actual output/metric/log.

## Layout

- [unbound-etc/unbound.conf](unbound-etc/unbound.conf) — a **snapshot** of
  `/opt/homebrew/etc/unbound/unbound.conf` from the Mac mini. This directory may live on a
  different machine than the resolver: never assume edits here affect the live service, and
  never claim live state (sockets, service status, stats) from these files — ask for pasted
  diagnostics from the mini instead. Deploying a change means syncing the file to the mini,
  then running `unbound-checkconf` and `unbound-control reload` **there**.
- [unbound-etc/unbound.bak](unbound-etc/unbound.bak) — a prior version of the config, kept for reference/diffing.
- [unbound-etc/root.hints](unbound-etc/root.hints) — IANA root server hints.
- Remote control uses a certless unix socket (`/opt/homebrew/var/run/unbound-control.sock`,
  root-owned — hence `sudo` on every `unbound-control` command). The old `unbound_server`/`unbound_control`
  TLS key/cert files are retired and deleted; if the config ever reverts to `control-interface: 127.0.0.1`,
  regenerate them with `unbound-control-setup`.
- [unbound-etc/blocklists/hagezi-normal.rpz](unbound-etc/blocklists/hagezi-normal.rpz) — the HaGeZi
  "Multi NORMAL" DNS blocklist (response-policy-zone format), refreshed daily by the update script.
  Don't hand-edit; it's overwritten on every run. (The larger "Pro" tier was retired 2026-07-01 —
  note Normal blocks fewer domains, so use `google-analytics.com`, not `doubleclick.net`, as the
  "is RPZ working" canary.)
- [unbound-etc/blocklists/hagezi-allow.rpz](unbound-etc/blocklists/hagezi-allow.rpz) — personal allowlist
  (RPZ passthru overrides). This is the file to edit when the blocklist wrongly blocks something.
- [unbound-etc/update-blocklist.sh](unbound-etc/update-blocklist.sh) — downloads, validates, and
  atomically swaps in the RPZ blocklist(s), reloading just the affected zone(s). **Pluggable
  two-slot design:** `primary` (zone `hagezi.rpz`) + `secondary` (zone `secondary.rpz`) each load
  an independently-chosen source (HaGeZi/1Hosts/OISD tiers) side by side — a domain blocked by
  either is blocked. Each slot's permanent source lives in a one-line file
  ([blocklist-source](unbound-etc/blocklist-source) = primary, default `1hosts-lite`;
  [blocklist-source-secondary](unbound-etc/blocklist-source-secondary) = secondary, default
  `oisd-big`); `sudo update-blocklist.sh <source> [slot]` is a one-time override, `off secondary`
  disables the second slot. Driven nightly (no-arg → refreshes both slots from their files) by the
  `local.unbound.blocklist` launchd job; the script header is the full reference.
- [unbound-etc/blocklist-tui.sh](unbound-etc/blocklist-tui.sh) — interactive menu over the same
  two-slot system: `sudo blocklist-tui.sh` shows both slots' current source, live line count, and
  breadcrumb age, and lets you pick a new source per slot (one-time or made permanent by writing
  the slot's source file). It does no downloading/validating/swapping itself — every real change
  is delegated to `update-blocklist.sh` next to it, so there's one place that knows how to do that
  safely. Read-only display logic is hand-kept in sync with `resolve_source`/`slot_source` in that
  script.
- [warmup/warm-unbound-cache.sh](warmup/warm-unbound-cache.sh) and
  [warmup/dns-warm-domains.txt](warmup/dns-warm-domains.txt) — pre-populates the resolver cache with the
  top ~300 domains (Tranco list) and reports the resulting cache-hit-rate delta.
- [launchd/homebrew.mxcl.unbound.plist](launchd/homebrew.mxcl.unbound.plist) — the launchd service definition installed
  by `brew services` (`~/Library/LaunchAgents/` or `/Library/LaunchDaemons/`).
  **Always use `sudo brew services ...`** for this one — running it without `sudo` installs a second,
  user-domain copy in `~/Library/LaunchAgents/` alongside the root one in `/Library/LaunchDaemons/`.
  Found on 2026-07-04: both existed on the mini (likely from an earlier non-sudo invocation); the
  user-domain copy was booted out (`launchctl bootout gui/$(id -u) ...`) and parked in
  `~/DisabledLaunchAgents/` rather than deleted. `lsof -i :53` should always show exactly one
  `unbound` process — if it ever shows two, or `brew services list` disagrees between sudo and
  non-sudo invocations, check for this duplicate first.
- [launchd/](launchd/) — snapshots of the hand-rolled `LaunchDaemon` jobs that live in
  `/Library/LaunchDaemons/` on the mini (they run as root, so blocklist writes + control-socket
  access work). These are captured for disaster recovery; editing them here does nothing until
  copied to `/Library/LaunchDaemons/` and reloaded (`sudo launchctl bootout/bootstrap system/...`).
  - `local.unbound.blocklist.plist` — runs `update-blocklist.sh` **daily at 03:30**, logging to
    `/opt/homebrew/var/log/unbound-blocklist.log`.
  - `local.unbound.warmlist.plist` — runs `~localadmin/warmup/build-warm-list.sh` **daily at
    23:30** (when the cache is richest), logging to `/opt/homebrew/var/log/unbound-warmlist.log`.
    Regenerates the personalized warmup list; sends no queries. A *scheduled* warmup is
    deliberately absent — `prefetch` + `serve-expired` keep the live cache warm continuously.
  - `local.unbound.warmboot.plist` — runs [warmup/warm-at-boot.sh](warmup/warm-at-boot.sh)
    **once per boot** (`RunAtLoad`), logging to `/opt/homebrew/var/log/unbound-warmboot.log`.
    Waits up to ~90s for unbound (whose first launch attempt dies in the known opendirectoryd
    boot race before KeepAlive relaunches it), then warms from the personal list, falling back
    to the curated one. The only automated warmup — the cache only needs rescuing after restarts.
  - `local.unbound.roothints.plist` — runs [usr-local-sbin/unbound-refresh-hints](usr-local-sbin/unbound-refresh-hints)
    (deployed at `/usr/local/sbin/unbound-refresh-hints`) **monthly, 1st at 03:00**, logging to
    `/var/log/unbound-refresh-hints.log`. Refreshes `root.hints` only: downloads from internic
    with `curl -fsS`, sanity-checks it lists the 13 roots, and atomically swaps it in. Does NOT
    reload unbound — root.hints only matters at the next (re)start, and the running resolver
    already has live root data — so there's no monthly cache flush. (Until 2026-07-03 it also
    regenerated a vestigial `hagezi-light.conf` blocklist the live config doesn't include; that
    dead half was dropped. The live blocklist is the RPZ `hagezi-normal.rpz`.)
  - `local.unbound.exporter.plist` — runs `unbound_exporter` (deployed at
    `/usr/local/sbin/unbound_exporter`, built from `github.com/letsencrypt/unbound_exporter`
    via `go install`) **continuously** (`RunAtLoad` + `KeepAlive`), logging to
    `/opt/homebrew/var/log/unbound-exporter.log`. Root, because it reads the root-owned control
    socket; serves Prometheus metrics on `127.0.0.1:9167` only (Prometheus scrapes it locally —
    raw metrics are never exposed to the LAN). It connects per-scrape, so it tolerates unbound's
    known boot race without a wait loop: it just reports `unbound_up 0` until the socket appears.
  - `local.unbound.blackbox.plist` — runs `blackbox_exporter` (no Homebrew formula exists;
    built like `unbound_exporter` via `go install github.com/prometheus/blackbox_exporter@latest`,
    deployed at `/usr/local/sbin/blackbox_exporter`)
    **continuously** as `localadmin` (`UserName` key — DNS probing needs no privileges),
    listening on `127.0.0.1:9115`, logging to `/opt/homebrew/var/log/unbound-blackbox.log`.
    Sends real DNS queries every 30s per `prometheus-etc/blackbox.yml` (deployed at
    `/opt/homebrew/etc/blackbox.yml`): `dns_upstream` asks 1.1.1.1/8.8.8.8 directly (raw
    upstream path — the 2026-07-01 failure mode, seen from outside), `dns_resolver` checks
    unbound is *answering* (NOT that recursion works: it repeats one hot name every 30s and
    serve-expired serves it from cache up to a day with recursion dead — recursion health is
    `dns_upstream` + the requestlist-jam alert), and `dns_rpz_canary` *succeeds only* while
    `google-analytics.com` returns NXDOMAIN — i.e. it alarms when ad-blocking silently stops.
  - `local.unbound.prometheus.plist` / `local.unbound.nodeexporter.plist` /
    `local.unbound.grafana.plist` — Prometheus, node_exporter, and Grafana as **system daemons
    running as `localadmin`** (`UserName` key). Converted from `brew services` user agents on
    2026-07-04 because **auto-login is OFF** on the mini: user LaunchAgents start at GUI login,
    not boot, so an unattended reboot would leave monitoring down while DNS came back fine.
    Consequences: (a) do **not** run `brew services start` for these three — it would create a
    second, user-domain copy (the unbound duplicate gotcha, inverted); (b) prometheus and
    node_exporter exec Homebrew's `*_brew_services` wrapper scripts so their `.args` files keep
    working, grafana gets Homebrew's standard CLI args verbatim; (c) after a
    `brew upgrade` of any of them: `sudo launchctl kickstart -k system/local.unbound.<name>` to
    restart onto the new binary — and for grafana/prometheus re-run the `socketfilterfw` allow
    (new Cellar path).
  - `local.unbound.alertnotify.plist` — runs
    [scripts/prometheus_alert_notifier.py](scripts/prometheus_alert_notifier.py) **every 120s**
    as `localadmin`, logging to `/opt/homebrew/var/log/unbound-alertnotify.log`. Polls
    Prometheus `/api/v1/alerts` and pushes newly-firing alerts (priority high) and resolutions
    (priority default) to ntfy.sh — the deliberate lightweight alternative to Alertmanager+bridge
    (two extra daemons for grouping/silencing features this small local rule set doesn't need).
    The topic name lives ONLY in `~localadmin/monitoring/ntfy.topic` (mode 600) — it is the only auth ntfy has, treat
    it like a password, never commit it. Subscribe the phone app to that topic. State in
    `~/monitoring/state/notified-alerts.json` (reboot wipes it → still-firing alerts re-announce
    once, intentionally). Known gap: a dead notifier can't push its own death —
    `AlertNotifierStale` shows on the dashboard/alerts page only.
  - `local.unbound.contentcache.plist` — runs
    [scripts/apple_content_cache_exporter.py](scripts/apple_content_cache_exporter.py) every
    5 minutes, parsing `AssetCacheManagerUtil status` (no privilege needed for the read) into a
    node_exporter textfile at `/opt/homebrew/var/lib/node_exporter/textfile/apple-content-cache.prom`.
    **Installed and running** as a `UserName localadmin` system daemon (verified live
    2026-07-07: `runs`>0, breadcrumb fresh, `apple-content-cache.prom` rewritten every 5 min).
    It is a system daemon rather than a per-user agent because this box has auto-login off and
    no active GUI session, so neither `launchctl bootstrap gui/<uid>` nor `user/<uid>` can load
    a per-user job over SSH (confirmed live) — same reason as prometheus/grafana/node_exporter.
    Two one-time steps were needed at install (kept here for disaster recovery): `sudo` to
    bootstrap the daemon, and a one-time `sudo chmod g+w` on the textfile directory (it ships
    root:admin 755 — localadmin is in `admin` but the dir wasn't group-writable). See the
    script's module docstring for the exact commands if rebuilding from scratch.
    **Accepted trust boundary:** that `g+w` means *any* admin-group process can write metric
    files the alert rules treat as ground truth (a malformed one trips `TextfileScrapeError`;
    a well-formed lie trips nothing). Fine for a single-operator box; noted so it's a
    decision, not an accident.
    `TotalBytesReturnedToClients`/`TotalBytesStoredFromOrigin` are exported as counters
    (`_total`) because the tool itself reports a `TotalBytesAreSince` reset-epoch timestamp —
    that's the evidence, not a guess — and `apple_content_cache_totals_since_seconds` is exported
    alongside so a 24h `increase()` panel can be sanity-checked against how long the counters
    have actually been accumulating. Public/private IP fields are deliberately never exported.
  (monitoring stack: `unbound_exporter`/`node_exporter`/`blackbox_exporter` → Prometheus at
  `http://192.168.0.100:9090`, Grafana dashboards at `http://192.168.0.100:3000`; see
  `grafana-etc/`). This replaced a short-lived DIY canvas dashboard
  (removed 2026-07-04; see `1fc441a`/`bbfeff7`). Prometheus is installed with
  `brew install prometheus` but runs via the `local.unbound.prometheus` LaunchDaemon (as
  `localadmin`, unprivileged — it never touches the control socket), **not** `brew services`;
  see the launchd bullets above for why. Config reload = edit files, then
  `sudo launchctl kickstart -k system/local.unbound.prometheus`.
  - `prometheus.yml` → `/opt/homebrew/etc/prometheus.yml` — scrapes the exporter and itself
    every 15s. Validate with `promtool check config` before restarting the service.
  - `prometheus.args` → `/opt/homebrew/etc/prometheus.args` — Homebrew's service wrapper
    word-splits this file into CLI flags, so it must contain **flags only, no comments**:
    UI bound explicitly to both `127.0.0.1:9090` (local automation) and `192.168.0.100:9090`
    (LAN access) via two repeated `--web.listen-address` flags — not `0.0.0.0`, same
    explicit-interface discipline as unbound's own binding (Wi-Fi/Tailscale excluded on
    purpose), 90-day retention, TSDB at `/opt/homebrew/var/prometheus`.
  - `prometheus.rules.yml` → `/opt/homebrew/etc/prometheus.rules.yml` — alert rules shown at
    `/alerts` in the UI (no Alertmanager, so nothing pages; they're dashboard tripwires).
    Includes a jammed-requestlist rule — the signature that would have surfaced the
    2026-07-01 upstream-path incident hours earlier.
  - `node_exporter.args` → `/opt/homebrew/etc/node_exporter.args` — host metrics (CPU, memory,
    disk fill, network) on `127.0.0.1:9100`, run via the `local.unbound.nodeexporter`
    LaunchDaemon. Same wrapper pattern as `prometheus.args`: **flags only, no comments** — so
    the rationale for `--no-collector.thermal` lives here instead: the thermal collector fails
    on every scrape on this hardware (`no CPU power status has been recorded`, a known
    node_exporter/Apple Silicon limitation), which logged one ERROR line per 15s forever into
    the unrotated daemon log (~1 MB/day of pure noise, observed 2026-07-09) and buried any
    real error. No thermal metrics are lost — none were ever successfully collected.
  - **macOS application firewall gotcha** (found 2026-07-04): the mini's firewall is enabled and
    silently drops inbound SYNs to unapproved binaries — no GUI prompt on a headless box, so the
    symptom is a *hanging* connection to a port `lsof` swears is listening. Prometheus needed
    `sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add/--unblockapp` on its **resolved
    Cellar path** (not the `/opt/homebrew/opt/...` symlink). Because the allow is keyed to that
    versioned path, `brew upgrade prometheus` will re-break it — if the dashboard goes
    unreachable after an upgrade, re-run the socketfilterfw pair on the new Cellar binary first.
    The loopback-only exporter (:9167) doesn't need an entry.
- [grafana-etc/](grafana-etc/) — Grafana as code, deployed under `/opt/homebrew/etc/grafana/`
  (`brew install grafana`, runs via the `local.unbound.grafana` LaunchDaemon as `localadmin`;
  UI on `http://192.168.0.100:3000`, first login admin/admin then forced change). LAN-facing, so it
  needs the same `socketfilterfw` allow as Prometheus — including again after every
  `brew upgrade grafana`. `grafana.ini` is a minimal override (unset keys fall back to shipped
  defaults; telemetry/update-checks off). The Prometheus datasource and all dashboards are
  file-provisioned from [dashboards/](grafana-etc/dashboards/) — the JSON files are the source
  of truth (regenerable via a script; UI edits do **not** write back, so export + re-snapshot
  here if a dashboard is changed interactively — and every board sets `editable: false` to
  block that footgun). The editing conventions (units-per-use, threshold/color rules, tags,
  refresh cadence, per-dashboard admission rules) are codified in [DASHBOARDS.md](DASHBOARDS.md).
  The suite is split by question, not one junk-drawer board:
  - `unbound-dns.json` — DNS resolver behavior only ("is DNS broken, and where?")
  - `system-stats.json` — host health: CPU, memory, load, swap, uptime
  - `services.json` — scrape-target health + the automation-job breadcrumb ages
  - `prometheus-monitoring.json` — scrape durations, TSDB size/series, rule eval
  - `network-tailscale.json` — en0 + utun throughput, errors/drops (no Tailscale
    metrics exist yet; tunnel traffic comes from node_exporter's utun series)
  - `storage-backups.json` — volume fill; the TM volume only exports series while
    mounted (macOS mounts it just during backups), so its panels are empty between runs
- [diagnostics.txt](diagnostics.txt) — a **historical** point-in-time snapshot from 2026-07-01
  (pre-monitoring-stack), kept for incident archaeology only. Never use it for current state —
  that's what Grafana/Prometheus are for. Recreate manually with the commands embedded in its
  section headers if a fresh text snapshot is ever wanted.

## Common commands

Config validation and reload (paths are Homebrew's, `/opt/homebrew/...`, since this runs on Apple Silicon):

```sh
unbound-checkconf                                  # validate unbound.conf before reloading
sudo unbound-control reload                         # full reload (re-reads unbound.conf + all zones)
sudo unbound-control auth_zone_reload hagezi.rpz     # reload just one RPZ zone (used by update-blocklist.sh)
sudo unbound-control stats_noreset                   # dump counters without resetting them
brew services list                                   # confirm the unbound service state
sudo brew services restart unbound                   # restart the whole service (last resort; drops cache)
```

Blocklist refresh (also runs on a schedule; safe to run manually):

```sh
sudo unbound-etc/update-blocklist.sh
```

It downloads HaGeZi Multi NORMAL, sanity-checks the file (must contain `CNAME` entries and have >100k
lines) before replacing the live zone, backs up the previous file to `hagezi-normal.rpz.bak`, and rolls back automatically
if `unbound-checkconf` fails after the swap. It reloads only the `hagezi.rpz` zone via
`auth_zone_reload`, falling back to a full `reload` if that fails.

Cache warming (run after a reload/restart to avoid a cold-cache burst of recursive lookups):

```sh
sudo warmup/warm-unbound-cache.sh                          # sudo required: unbound-control talks to a root-owned
                                                           # unix socket; uses the dns-warm-domains.txt next to
                                                           # the script by default
sudo warmup/warm-unbound-cache.sh path/to/domains.txt       # or an explicit domain list
sudo DNS_SERVER=192.168.0.100 warmup/warm-unbound-cache.sh  # query via the LAN address (must still run ON the mini —
                                                            # stats come from the local unbound-control socket)
```

## Editing unbound.conf

- The `.lan` zone (`local-zone`/`local-data`/`local-data-ptr` block) is the source of truth for LAN
  hostnames. `local-zone "lan." static` means any `*.lan` name not explicitly listed returns NXDOMAIN —
  add both the forward (`local-data`) and reverse (`local-data-ptr`) entries when adding a host.
- Tailscale (`*.tail3fd0f4.ts.net`, `100.64.0.0/10` PTR) is deliberately **not** handled here — MagicDNS
  resolves those at the OS level. Do not add forwarding for Tailscale zones to this config.
- Three `rpz:` zones are loaded in order: `hagezi.allow` (personal passthru allowlist, evaluated
  **first** so it overrides both blocklists) then `hagezi.rpz` (primary blocklist) then
  `secondary.rpz` (secondary blocklist) — the last two both force `nxdomain` on match, so a domain
  in either is blocked. To un-block a domain, add a passthru entry to `hagezi-allow.rpz` (see its
  `CNAME rpz-passthru.` pattern), bump the SOA serial in its header, then reload — that one
  allowlist covers both blocklists. Don't edit `hagezi-normal.rpz` or `blocklist-secondary.rpz`
  directly, they get clobbered on every update.
- After any `unbound.conf` edit: run `unbound-checkconf` first, then `sudo unbound-control reload`. Never
  reload/restart on an unvalidated config on the LAN's primary DNS resolver — a bad config degrades every
  device on `192.168.0.0/24` to the unfiltered `9.9.9.9` fallback (no RPZ blocking, no `.lan` names).
- On a fresh deploy (or any config change that renames the blocklist zonefile): run
  `sudo update-blocklist.sh` **before** `unbound-checkconf` — checkconf verifies the RPZ zonefile can be
  opened and fails if the generated file doesn't exist yet.
- `do-ip6: no` and only the Ethernet (`192.168.0.100`) + loopback interfaces are bound intentionally — this
  host has no working public IPv6 route and Wi-Fi/Tailscale interfaces are excluded on purpose.

## When DNS breaks (the 1:37 AM checklist)

Work down this list in order; each step isolates one layer. All commands run **on the mini**.

```sh
# 1. Is unbound even up and answering?
dig @127.0.0.1 +time=2 +tries=1 cloudflare.com A | /usr/bin/grep status:
#    NOERROR -> resolver fine, problem is client-side/DHCP. Stop here.
#    timeout -> step 2.  SERVFAIL -> step 3.

# 2. Process dead or socket gone?
ps aux | /usr/bin/grep '[u]nbound'
launchctl print system/homebrew.mxcl.unbound | /usr/bin/grep -E 'state|last exit'
#    Not running -> sudo brew services restart unbound, then
#    sudo ~localadmin/warmup/warm-at-boot.sh. If it crash-loops, check
#    unbound-checkconf before touching anything else.

# 3. SERVFAIL: upstream path dead, or DNSSEC?
#    Check Grafana "Upstream path (:53)" stat, or from a shell:
dig @1.1.1.1 +time=2 +tries=1 cloudflare.com | /usr/bin/grep status:
#    1.1.1.1 direct FAILS too -> network-path problem (the 2026-07-01 mode).
#    NOT a resolver bug - do not edit unbound.conf. Check router/modem/ISP.
#    1.1.1.1 works but unbound SERVFAILs one domain -> likely that domain's
#    DNSSEC is genuinely broken (verify: does 1.1.1.1 resolve it with +cd?).

# 4. "DNS works but some site is blocked"
dig @127.0.0.1 thesite.com | /usr/bin/grep -E 'status:|flags:'
#    NXDOMAIN **with aa flag** = RPZ block -> allowlist procedure below.
#    NXDOMAIN without aa = the domain really doesn't exist.
```

Known boot behavior (do not chase at 1:37 AM): after a reboot,
`launchctl print` shows `runs = 2, last exit code = 1` for unbound
(opendirectoryd race), prometheus, and grafana (bind before the interface
has its IP). KeepAlive relaunches all three within ~10s. This is
self-healing by design; it is only a problem if `runs` keeps climbing.

## Adding / removing / testing block+allow rules

Un-block a domain (the only rule edit you'll routinely make):

```sh
# 1. confirm it IS an RPZ block (aa + NXDOMAIN):
dig @127.0.0.1 example.com | /usr/bin/grep -E 'status:|flags:'
# 2. edit /opt/homebrew/etc/unbound/blocklists/hagezi-allow.rpz — copy the
#    placeholder pattern (bare name + *.wildcard), and BUMP THE SOA SERIAL.
# 3. reload just that zone (no cache loss):
sudo unbound-control auth_zone_reload hagezi.allow
# 4. verify the unblock took:
dig @127.0.0.1 example.com | /usr/bin/grep status:     # now NOERROR (or real NXDOMAIN)
# 5. sync the repo copy on Honk (rsync/scp, never Finder), commit.
```

Block a domain the list misses: don't edit `hagezi-normal.rpz` (clobbered
nightly). Add a `local-zone: "domain.com." always_nxdomain` line to
unbound.conf's server section, `unbound-checkconf`, `sudo unbound-control reload`.

Known test domains (memorize the first one):
- `google-analytics.com` — must be BLOCKED (NXDOMAIN + `aa`). The canary; blackbox alerts if not.
- `cloudflare.com` — must resolve NOERROR with `ad` (DNSSEC validated).
- `dnssec-failed.org` — must SERVFAIL (validation working). If it resolves, DNSSEC is off/broken.
- `mini.lan` / `router.lan` — must return their static IPs; any other `.lan` name -> NXDOMAIN.

## Monitoring operations

| Service | Restart | Log |
|---|---|---|
| unbound | `sudo brew services restart unbound` (drops cache — warm after) | syslog (`log show --predicate 'process == "unbound"' --last 1h`) |
| prometheus | `sudo launchctl kickstart -k system/local.unbound.prometheus`; config-only reload: `pkill -HUP -f Cellar/prometheus` (no sudo, keeps TSDB hot) | `/opt/homebrew/var/log/prometheus.log` |
| grafana | `sudo launchctl kickstart -k system/local.unbound.grafana` | `/opt/homebrew/var/log/unbound-grafana-launchd.log` + `/opt/homebrew/var/log/grafana/grafana.log` |
| node_exporter | `sudo launchctl kickstart -k system/local.unbound.nodeexporter` | `/opt/homebrew/var/log/node_exporter.log` |
| unbound_exporter | `sudo launchctl kickstart -k system/local.unbound.exporter` | `/opt/homebrew/var/log/unbound-exporter.log` |
| blackbox | `sudo launchctl kickstart -k system/local.unbound.blackbox` | `/opt/homebrew/var/log/unbound-blackbox.log` |
| contentcache | runs itself every 5 min; force: `sudo launchctl kickstart system/local.unbound.contentcache` | `/opt/homebrew/var/log/apple-content-cache-exporter.log` |

Job status without sudo: `launchctl print system/local.unbound.<name> | /usr/bin/grep -E 'state|last exit'`.
Scheduled-job logs: `/opt/homebrew/var/log/unbound-{blocklist,warmlist,warmboot}.log`, `/var/log/unbound-refresh-hints.log`.

Log rotation: [etc-newsyslog.d/unbound-monitoring.conf](etc-newsyslog.d/unbound-monitoring.conf)
(deployed at `/etc/newsyslog.d/`) rotates the scheduled-job logs at 1 MB, 5 gzipped
generations; macOS's built-in newsyslog run (every 30 min) does the work. Daemon-held logs
are deliberately excluded — see the comments in that file. Dry-run test:
`sudo newsyslog -nv -f /etc/newsyslog.d/unbound-monitoring.conf`.

Phone alerts: firing Prometheus alerts push to ntfy (see the `alertnotify` launchd bullet).
Test the channel: `curl -H "Title: test" -d "hello" https://ntfy.sh/$(cat ~localadmin/monitoring/ntfy.topic)`.

After `brew upgrade` of prometheus/grafana: kickstart the daemon onto the
new binary AND re-run the `socketfilterfw --add/--unblockapp` pair on the
new **Cellar** path (LAN-facing binaries only; loopback exporters exempt).

Health one-liners:
```sh
curl -s 'http://127.0.0.1:9090/api/v1/targets' | /usr/bin/grep -o '"health":"up"' | wc -l   # expect 8
#  (-o then wc, NOT grep -c: the API returns single-line JSON, so -c always says 1)
curl -s 'http://127.0.0.1:9090/api/v1/alerts'                                       # expect no firing alerts
curl -s http://127.0.0.1:9100/metrics | /usr/bin/grep node_textfile_scrape_error        # expect 0
```

## Backup / rollback

- Config bundle (run on the mini): `tar czf ~/unbound-backup-$(date +%Y%m%d-%H%M%S).tar.gz /opt/homebrew/etc/unbound /opt/homebrew/etc/{prometheus,blackbox}* /opt/homebrew/etc/grafana /Library/LaunchDaemons/local.unbound.*.plist 2>/dev/null`
- The repo IS the restore source: every deployed file is committed here (binaries excepted:
  `unbound_exporter`/`blackbox_exporter` are rebuilt via `go install`, see launchd bullets).
- Rolling back a bad change = `git log` -> `git revert <sha>` on Honk -> redeploy that file ->
  validate (`unbound-checkconf` / `promtool check config` / `plutil -lint`) -> reload the one
  affected service. The blocklist rolls itself back automatically on a failed update.

## Remote access / attack surface

Access this box over **SSH** (LAN `192.168.0.100` or the tailnet) or, occasionally,
**Screen Sharing**. The macOS Application Firewall is enabled (see the socketfilterfw
gotcha under monitoring). On 2026-07-07 three unused inbound services were shut off to
shrink the surface — all had been listening on `*` (LAN + tailnet):

- **SMB file sharing (`:445`)** — was on but shared nothing (`sharing -l` empty).
  Disabled: `sudo launchctl disable system/com.apple.smbd` (+ `bootout`).
- **Screen Sharing (`:5900`)** — rarely used, so kept off by default and flipped on
  on-demand (below) rather than left listening. Disabled the same way
  (`system/com.apple.screensharing`).
- **Local Kerberos KDC (`:88`)** — the machine's LKDC (realm `LKDC:SHA1.…`), which exists
  only to back Kerberos SSO for those sharing services. With both off it has nothing to
  serve, so disabled too (`system/com.apple.Kerberos.kdc`).

These are `launchctl disable` (persists across reboots via the override DB; works under
SIP). Re-enable any with `sudo launchctl enable system/<label>` + a reboot or `bootstrap`.
**Don't `kill` the PIDs** — launchd relaunches them; disable the job instead. The
`RemoteManagement.framework`/`remotemanagementd` fleet also seen running is the macOS
baseline Declarative-Management subsystem — this box is **not** MDM-enrolled
(`profiles status -type enrollment` → No), it exposes no port, leave it alone.

On-demand Screen Sharing (deployed to `/usr/local/sbin/`, need sudo — same root-owned
dir as `unbound-refresh-hints`; install with
`sudo install -m 755 /tmp/screenshare-* /usr/local/sbin/`). **Invoke by full path:**
`sudo`'s `secure_path` does not include `/usr/local/sbin`, so `sudo screenshare-on`
gives "command not found" — use `sudo /usr/local/sbin/screenshare-on`.
- [usr-local-sbin/screenshare-on](usr-local-sbin/screenshare-on) —
  `sudo /usr/local/sbin/screenshare-on` enables `com.apple.screensharing` and confirms
  `:5900` is listening.
- [usr-local-sbin/screenshare-off](usr-local-sbin/screenshare-off) —
  `sudo /usr/local/sbin/screenshare-off` disables it again and confirms `:5900` is
  closed. Run it when done.

With the KDC off, Screen Sharing authenticates by password rather than Kerberos SSO —
which is how a remote VNC login works anyway. To audit the live surface:
`sudo lsof -nP -iTCP -sTCP:LISTEN` (non-root lsof can't see root-owned `:22`/`:53`).

## Do not do this unless you mean it

- **Never `brew services start` prometheus/grafana/node_exporter** — creates a second,
  login-scoped copy fighting the system daemon. They are launchd-managed now.
- **Never plain `brew services` (without sudo) for unbound** — same duplicate problem, inverted.
- **Never edit `hagezi-normal.rpz`** — nightly clobber. Allowlist goes in `hagezi-allow.rpz`.
- **Never reload unbound on an unvalidated config** — it's the LAN's primary resolver; breaking
  it degrades every client to the unfiltered `9.9.9.9` fallback.
- **Never run `tailscale funnel`** — everything here is LAN/tailnet-only by design.
- **Never Finder-drag into this repo** — it has deleted tracked files twice. rsync/scp only.
