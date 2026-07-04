# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Configuration and operational scripts for **Unbound**, a recursive DNS resolver running as a Homebrew
service on a Mac mini (`mini.lan`, `192.168.0.100`). It serves DNS for the local LAN (`192.168.0.0/24`)
plus loopback, with DNSSEC validation, ad/tracker blocking via RPZ, and a home `.lan` zone. This is not
an application codebase — there is no build, lint, or test suite. Changes here take effect by editing
config/scripts in place and reloading the live `unbound` process.

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
- [unbound-etc/update-blocklist.sh](unbound-etc/update-blocklist.sh) — downloads the latest HaGeZi
  Multi NORMAL list, validates it, and reloads the `hagezi.rpz` zone in place. Driven nightly by the
  `local.unbound.blocklist` launchd job (see below); also safe to run manually with `sudo`.
- [warmup/warm-unbound-cache.sh](warmup/warm-unbound-cache.sh) and
  [warmup/dns-warm-domains.txt](warmup/dns-warm-domains.txt) — pre-populates the resolver cache with the
  top ~300 domains (Tranco list) and reports the resulting cache-hit-rate delta.
- [homebrew.mxcl.unbound.plist](homebrew.mxcl.unbound.plist) — the launchd service definition installed
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
    upstream path — the 2026-07-01 failure mode, seen from outside), `dns_resolver` resolves
    through unbound like a LAN client, and `dns_rpz_canary` *succeeds only* while
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
  - `local.unbound.contentcache.plist` — runs
    [scripts/apple_content_cache_exporter.py](scripts/apple_content_cache_exporter.py) every
    5 minutes, parsing `AssetCacheManagerUtil status` (no privilege needed for the read) into a
    node_exporter textfile at `/opt/homebrew/var/lib/node_exporter/textfile/apple-content-cache.prom`.
    **Not yet installed** (2026-07-04): this box has auto-login off and no active GUI session, so
    neither `launchctl bootstrap gui/<uid>` nor `user/<uid>` can load a per-user job over SSH
    (confirmed live) — it has to be a `UserName localadmin` system daemon like the others, which
    needs `sudo` to install. It also needs a one-time `sudo chmod g+w` on the textfile directory
    (root:admin 755 — localadmin is in the `admin` group but the dir isn't group-writable yet).
    Both are one-time manual steps; see the script's module docstring for the exact commands.
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
    UI bound to `192.168.0.100:9090` (LAN-only, same discipline as unbound's interfaces),
    90-day retention, TSDB at `/opt/homebrew/var/prometheus`.
  - `prometheus.rules.yml` → `/opt/homebrew/etc/prometheus.rules.yml` — alert rules shown at
    `/alerts` in the UI (no Alertmanager, so nothing pages; they're dashboard tripwires).
    Includes a jammed-requestlist rule — the signature that would have surfaced the
    2026-07-01 upstream-path incident hours earlier.
  - `node_exporter.args` → `/opt/homebrew/etc/node_exporter.args` — host metrics (CPU, memory,
    disk fill, network) on `127.0.0.1:9100`, run via the `local.unbound.nodeexporter`
    LaunchDaemon. Same wrapper pattern as `prometheus.args`: **flags only, no comments**.
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
  here if a dashboard is changed interactively). The suite is split by question, not one
  junk-drawer board:
  - `unbound-dns.json` — DNS resolver behavior only ("is DNS broken, and where?")
  - `system-stats.json` — host health: CPU, memory, load, swap, uptime
  - `services.json` — scrape-target health + the automation-job breadcrumb ages
  - `prometheus-monitoring.json` — scrape durations, TSDB size/series, rule eval
  - `network-tailscale.json` — en0 + utun throughput, errors/drops (no Tailscale
    metrics exist yet; tunnel traffic comes from node_exporter's utun series)
  - `storage-backups.json` — volume fill; the TM volume only exports series while
    mounted (macOS mounts it just during backups), so its panels are empty between runs
- [diagnostics.txt](diagnostics.txt) — a point-in-time snapshot (uname, `brew services`, `lsof -i :53`,
  `unbound -V`, `unbound-checkconf`, `unbound-control stats_noreset`) used for troubleshooting. Not
  auto-generated by any script here; recreate manually with the commands embedded in its section headers.

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
- Two `rpz:` zones are loaded in order: `hagezi.allow` (personal passthru allowlist, evaluated first) then
  `hagezi.rpz` (the blocklist, forced to `nxdomain` on match). To un-block a domain, add a passthru entry
  to `hagezi-allow.rpz` (see its `CNAME rpz-passthru.` pattern), bump the SOA serial in its header, then
  reload — don't edit `hagezi-normal.rpz` directly, it gets clobbered daily.
- After any `unbound.conf` edit: run `unbound-checkconf` first, then `sudo unbound-control reload`. Never
  reload/restart on an unvalidated config on a box that's the LAN's only DNS resolver — a bad config
  breaks name resolution for every device on `192.168.0.0/24`.
- On a fresh deploy (or any config change that renames the blocklist zonefile): run
  `sudo update-blocklist.sh` **before** `unbound-checkconf` — checkconf verifies the RPZ zonefile can be
  opened and fails if the generated file doesn't exist yet.
- `do-ip6: no` and only the Ethernet (`192.168.0.100`) + loopback interfaces are bound intentionally — this
  host has no working public IPv6 route and Wi-Fi/Tailscale interfaces are excluded on purpose.
