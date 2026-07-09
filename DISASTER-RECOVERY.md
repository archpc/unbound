# Mac Mini DNS + Monitoring Recovery Runbook

**Scope:** full rebuild of `mini` (192.168.0.100) — Unbound DNS resolver, Prometheus +
Grafana monitoring, and supporting exporters — from a blank macOS install to a fully
validated, LAN-serving state.

**Audience:** you, at an unpleasant hour, with the physical box or a fresh replacement
in front of you.

**Source of truth:** this repo. Every config file referenced below lives here; the live
box is a deployment target, never the record.

---

## 0. One-page emergency checklist

Use this if DNS is down *right now* and you have **not yet decided** this is a full
rebuild. Most DNS outages are not disasters — work this list first.

- [ ] **1. Check local resolver** — from any LAN device: `dig @192.168.0.100 cloudflare.com +short`. Prints an IP → not a rebuild situation, stop and see [RUNBOOK.md](RUNBOOK.md) §5 instead.
- [ ] **2. Check the Unbound process** — SSH to the box: `ps aux | grep '[u]nbound'`. Not running → `sudo brew services restart unbound`, then warm the cache (step 6).
- [ ] **3. Check the upstream path** — `dig @1.1.1.1 +time=2 +tries=1 cloudflare.com | grep status:`. Also fails → this is your internet/router/ISP, **not** this box. Do not touch unbound.conf.
- [ ] **4. Check for an RPZ false positive** — `dig @127.0.0.1 thesite.com | grep -E 'status:|flags:'`. `NXDOMAIN` + `aa` → blocklist is blocking it, not broken; see RUNBOOK.md §4.1 to allowlist.
- [ ] **5. Restart only if the config validates** — `unbound-checkconf` must print no errors *before* `sudo unbound-control reload` or a service restart. Never reload/restart on an unvalidated config.
- [ ] **6. Warm the cache after any restart** — `sudo /Users/localadmin/warmup/warm-unbound-cache.sh` (a cold cache means every query recurses from scratch for a while).
- [ ] **7. Confirm clients resolve through 192.168.0.100** — from a phone or laptop on the LAN, load a site that wasn't cached. If it resolves, you're done.

**If none of this brings DNS back — hardware is dead, the box won't boot, or you're
standing in front of its replacement — this is a rebuild. Continue to §1.**

---

## 1. Overview

This Mac mini is the **primary DNS resolver for the home LAN** (`192.168.0.0/24`) —
clients carry `9.9.9.9` as DHCP fallback (set on the router; verify there), so a mini
outage degrades the LAN (no ad-blocking, no `.lan` names) rather than killing it. It:

- Recursively resolves DNS for every device on the network, with **DNSSEC validation**.
- Blocks ads/trackers via an **RPZ blocklist** (HaGeZi "Multi NORMAL"), refreshed nightly.
- Serves a local **`.lan` zone** for in-house hostnames (`mini.lan`, `router.lan`, …).
- Caches **Apple software updates** for the LAN (Content Caching), and
- Runs a full **Prometheus + Grafana monitoring stack** watching all of the above, with
  phone push alerts via ntfy.

If this box is down, **the house degrades to the unfiltered fallback resolver** — ads
return, `.lan` names break, and DNS runs without your policy — that's why this document
exists, and why every step below errs toward "validate before you trust it."

For day-to-day operation (not rebuild), see [RUNBOOK.md](RUNBOOK.md). For the deep
technical reference behind every file, see [CLAUDE.md](CLAUDE.md).

---

## 2. System map

```
                         Home LAN  192.168.0.0/24
                                │
                                │  DHCP hands out DNS = 192.168.0.100, fallback 9.9.9.9
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│  Mac mini "mini"  —  192.168.0.100  (also on Tailscale: mini.tail3fd0f4.ts.net)
│                                                                      │
│  ┌───────────┐   RPZ (allow→block)   ┌────────────────────────┐    │
│  │  unbound   │──────────────────────▶│ hagezi.allow (repo)   │    │
│  │  :53       │                       │ hagezi.rpz (generated)│    │
│  │  DNSSEC    │                       └────────────────────────┘    │
│  │  .lan zone │                                                     │
│  └─────┬──────┘                                                     │
│        │ stats                                                     │
│        ▼                                                            │
│  unbound_exporter :9167 (loopback only, root — control socket)     │
│                                                                      │
│  node_exporter :9100 (loopback) ── textfile dir ◀── scheduled jobs  │
│  blackbox_exporter :9115 (loopback) ── active DNS probes            │
│  apple_content_cache_exporter (LaunchDaemon every 5 min, writes textfile)
│                                                                      │
│              all scraped by ▼                                      │
│         Prometheus :9090 (loopback + LAN, explicit binds)           │
│                     │                                                │
│                     ├── alert rules (no Alertmanager) ──┐           │
│                     ▼                                    ▼           │
│            Grafana :3000 (LAN)              prometheus_alert_notifier.py
│         (7 dashboards, file-provisioned)          (poll /api/v1/alerts,
│                                                     push to ntfy.sh) │
└────────────────────────────────────────────────────────────────────┘
                                │
                        Access: SSH (key-only) + Tailscale
                        Screen Sharing: off by default, on-demand only
```

---

## 3. What the repo contains

Everything below is tracked in git and is the **restore source** for that file. Paths
are repo-relative; the runbook tells you where each deploys to on the live box.

| Path | Deploys to | Purpose |
|---|---|---|
| `unbound-etc/unbound.conf` | `/opt/homebrew/etc/unbound/unbound.conf` | resolver config: interfaces, `.lan` zone, RPZ zones, DNSSEC |
| `unbound-etc/root.hints` | `/opt/homebrew/etc/unbound/root.hints` | IANA root server hints |
| `unbound-etc/blocklists/hagezi-allow.rpz` | `.../blocklists/hagezi-allow.rpz` | hand-curated allowlist (passthru overrides) |
| `unbound-etc/update-blocklist.sh` | `/opt/homebrew/etc/unbound/update-blocklist.sh` | nightly blocklist refresh + validate + rollback |
| `usr-local-sbin/unbound-refresh-hints` | `/usr/local/sbin/unbound-refresh-hints` | monthly root.hints refresh |
| `usr-local-sbin/screenshare-on` / `-off` | `/usr/local/sbin/screenshare-{on,off}` | on-demand Screen Sharing toggle |
| `prometheus-etc/prometheus.yml` | `/opt/homebrew/etc/prometheus.yml` | scrape targets |
| `prometheus-etc/prometheus.args` | `/opt/homebrew/etc/prometheus.args` | CLI flags (flags only, no comments) |
| `prometheus-etc/prometheus.rules.yml` | `/opt/homebrew/etc/prometheus.rules.yml` | the full local alert rule set (`grep -c '- alert:'` the file for the current count) |
| `prometheus-etc/blackbox.yml` | `/opt/homebrew/etc/blackbox.yml` | DNS probe module definitions |
| `prometheus-etc/node_exporter.args` | `/opt/homebrew/etc/node_exporter.args` | CLI flags |
| `grafana-etc/grafana.ini` | `/opt/homebrew/etc/grafana/grafana.ini` | Grafana server config |
| `grafana-etc/provisioning/**` | `/opt/homebrew/etc/grafana/provisioning/**` | datasource + dashboard provider config |
| `grafana-etc/dashboards/*.json` (7 files) | `/opt/homebrew/etc/grafana/dashboards/*.json` | all dashboards (source of truth — see [DASHBOARDS.md](DASHBOARDS.md)) |
| `launchd/local.unbound.*.plist` (12 files) | `/Library/LaunchDaemons/local.unbound.*.plist` | every hand-rolled scheduled job + daemon definition — deployed directly, see §10 |
| `launchd/homebrew.mxcl.unbound.plist` | — (reference snapshot only) | **not** hand-deployed — managed entirely by `sudo brew services start/restart unbound` (§8); kept here for disaster-recovery reference, not copied into place |
| `etc-newsyslog.d/unbound-monitoring.conf` | `/etc/newsyslog.d/unbound-monitoring.conf` | log rotation for scheduled-job logs |
| `etc-ssh-sshd_config.d/500-keyonly.conf` | `/etc/ssh/sshd_config.d/500-keyonly.conf` | SSH key-only hardening |
| `scripts/apple_content_cache_exporter.py` | `/Users/localadmin/monitoring/apple_content_cache_exporter.py` | content-cache metrics |
| `scripts/unbound_client_rpz_exporter.py` | `/Users/localadmin/monitoring/unbound_client_rpz_exporter.py` | RPZ block visibility by client only — not general DNS-client visibility |
| `scripts/prometheus_alert_notifier.py` | `/Users/localadmin/monitoring/prometheus_alert_notifier.py` | polls alerts → pushes ntfy |
| `warmup/*.sh`, `warmup/dns-warm-domains.txt` | `~localadmin/warmup/` | cache warming (curated list only — personal list is NOT in repo, see §4) |
| `CLAUDE.md`, `RUNBOOK.md`, `DASHBOARDS.md` | — | technical reference, operator guide, dashboard conventions |

**This document does not repeat every file's internals** — CLAUDE.md is the deep
reference. This document is the *sequence* to put them all back together correctly.

---

## 4. What the repo does **not** contain

These either can't be committed (secrets), regenerate themselves, or are pure runtime
state. **Do not expect `git clone` to restore any of these:**

| Item | Location (live) | Why it's excluded | Rebuild impact if lost |
|---|---|---|---|
| **ntfy topic** | `~localadmin/monitoring/ntfy.topic` (mode 600) | it's the *only* auth ntfy has — treated like a password | phone alerts silently stop; you must recreate the topic and file (§17) |
| Generated blocklist | `.../blocklists/hagezi-normal.rpz` | ~9 MB, clobbered nightly by `update-blocklist.sh` | none — regenerates on first run, but **must exist before `unbound-checkconf`** (§9) |
| Personal warmup list | `~localadmin/warmup/dns-warm-domains.personal.txt` | household behavior profile — privacy-sensitive | none — self-regenerates from the live cache over time; curated list still works meanwhile |
| Notifier state | `~localadmin/monitoring/state/notified-alerts.json` | ephemeral by design (reboot already wipes it) | none — still-firing alerts just re-announce once |
| Prometheus TSDB | `/opt/homebrew/var/prometheus` (~143 MB observed) | runtime metrics data, not config | historical graphs are gone; dashboards repopulate as new data arrives |
| Grafana's internal DB | `/opt/homebrew/var/lib/grafana/grafana.db` | holds users/sessions/API keys — dashboards themselves are file-provisioned, not stored here | admin password resets to `admin`/`admin` (forced change); any manually-created users/API keys are gone; **dashboards are unaffected** (they reprovision from JSON) |
| SSH private key | lives on **Honk**, never on the mini | the mini only ever holds the *public* key | if Honk's key is lost, you need physical/console access to add a new one |
| Grafana admin password | nowhere — resets to default | not a secret worth storing; forced-change flow handles it | you'll be prompted to set a new one on first login, every fresh install |
| Tailscale device identity | Apple's System Extension keychain state | tied to the specific install/auth, not file-based | rejoining is a new node unless you dis-associate the old one first (§17) |
| macOS itself, Xcode CLT, Homebrew | — | not this repo's concern | see §6 |

---

## 5. Required backups — do this **before** you wipe anything

If the box is dying but still reachable, grab these first. If it's already dead, skip
to §6 — you've lost the items below and §17/§4 tell you how to recreate or accept the loss.

```sh
# Run FROM Honk (or any machine with SSH access), while the mini is still reachable.
mkdir -p ~/mini-final-backup
scp localadmin@mini.tail3fd0f4.ts.net:~/monitoring/ntfy.topic ~/mini-final-backup/
scp localadmin@mini.tail3fd0f4.ts.net:~/warmup/dns-warm-domains.personal.txt ~/mini-final-backup/ 2>/dev/null
ssh localadmin@mini.tail3fd0f4.ts.net \
  'tar czf - /opt/homebrew/var/prometheus 2>/dev/null' > ~/mini-final-backup/prometheus-tsdb.tar.gz  # optional, only if you want historical graphs
```

> **⚠️ The ntfy topic file is the one irreplaceable secret.** Everything else in this
> list is optional convenience. If you only grab one file, grab that one — losing it
> means recreating a new ntfy topic and updating your phone's subscription.

---

## 6. Clean macOS prep

- [ ] Install macOS (current release at time of writing: **26.5.2**, build 25F84 — a
  newer point release is fine; a different major version means re-verify Homebrew's
  Apple Silicon support before proceeding).
- [ ] Create the **`localadmin`** account (the account every service in this stack runs
  as, other than root-owned pieces). Standard admin account, no auto-login (this box
  deliberately runs with **auto-login OFF** — see §17 for why that matters).
- [ ] Install Xcode Command Line Tools: `xcode-select --install`
- [ ] Install Homebrew (Apple Silicon path, `/opt/homebrew`):
  ```sh
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```
- [ ] Enable **Remote Login (SSH)**: System Settings → General → Sharing → Remote Login → on.
  Leave **Screen Sharing off** for now — it's re-enabled deliberately, on-demand, at the
  end (§17).
- [ ] Confirm you're logged in as `localadmin` for the remainder of this runbook.

---

## 7. Homebrew + package installation

**Required for the stack** (confirmed installed set as of 2026-07-07 — Homebrew will
give you current versions, which is fine; these are not hard pins):

| Formula | Version observed | Role |
|---|---|---|
| `unbound` | 1.25.1 | the resolver |
| `prometheus` | 3.13.0 | metrics + alert rules |
| `grafana` | 13.1.0 | dashboards |
| `node_exporter` | 1.11.1 | host metrics |
| `go` | 1.26.4 | builds `unbound_exporter` + `blackbox_exporter` (not brew formulae) |

```sh
brew install unbound prometheus grafana node_exporter go
```

**Not required for the stack, but present on the current box** (personal shell tools —
`bat`, `btop`, `dust`, `eza`, `fd`, `fzf`, `htop`, `iftop`, `iperf3`, `jq`, `mtr`,
`ncdu`, `nmap`, `python@3.14`, `ripgrep`, `rsync`, `smartmontools`, `tree`, `wget`,
`zoxide`, zsh plugins). Install these only if you personally want them — **do not treat
them as dependencies of DNS or monitoring.**

> **⚠️ `iperf3` note:** it's a legitimate formula (network throughput testing), but a
> stray `iperf3 -s -D` server process was found running unmanaged on `*:5201` during a
> security review (2026-07-07) — nobody's job starts it. If you reinstall it, don't
> leave a server instance running; it's for on-demand testing only.

Build the two Go-based exporters (no Homebrew formula exists for either):

```sh
go install github.com/letsencrypt/unbound_exporter@latest
go install github.com/prometheus/blackbox_exporter@latest

# go install drops binaries in $(go env GOPATH)/bin (default ~/go/bin) — move them
# to /usr/local/sbin, root-owned, matching every other daemon binary on this box:
sudo cp ~/go/bin/unbound_exporter /usr/local/sbin/unbound_exporter
sudo cp ~/go/bin/blackbox_exporter /usr/local/sbin/blackbox_exporter
sudo chown root:wheel /usr/local/sbin/unbound_exporter /usr/local/sbin/blackbox_exporter
sudo chmod 755 /usr/local/sbin/unbound_exporter /usr/local/sbin/blackbox_exporter
```

Clone this repo onto the mini (or work from Honk and `scp`/`rsync` — **never Finder**,
it has deleted tracked files here before):

```sh
git clone <this-repo-url> ~/unbound-repo   # or rsync from Honk
```

---

## 8. Unbound restore

```sh
sudo mkdir -p /opt/homebrew/etc/unbound/blocklists
sudo cp ~/unbound-repo/unbound-etc/unbound.conf        /opt/homebrew/etc/unbound/unbound.conf
sudo cp ~/unbound-repo/unbound-etc/root.hints          /opt/homebrew/etc/unbound/root.hints
sudo cp ~/unbound-repo/unbound-etc/blocklists/hagezi-allow.rpz \
        /opt/homebrew/etc/unbound/blocklists/hagezi-allow.rpz
sudo cp ~/unbound-repo/unbound-etc/update-blocklist.sh /opt/homebrew/etc/unbound/update-blocklist.sh
sudo chmod +x /opt/homebrew/etc/unbound/update-blocklist.sh
```

> **⚠️ Ordering matters — do §9 (generate the blocklist) before running
> `unbound-checkconf` below.** `unbound.conf` references `hagezi.rpz`, and checkconf
> fails if that zonefile doesn't exist yet on a from-scratch box.

Continue here only **after** §9 has produced `hagezi-normal.rpz`:

```sh
unbound-checkconf                       # must print "no errors" — do not proceed otherwise
sudo brew services start unbound        # NOTE: sudo, always — non-sudo creates a duplicate
                                         # user-domain copy fighting the root one (see §21)
```

Warm the cache (a cold resolver means every query recurses from scratch):

```sh
sudo ~/unbound-repo/warmup/warm-unbound-cache.sh
```

---

## 9. Blocklist/RPZ restore

The blocklist runs as **two independent RPZ slots** — `primary` (zone `hagezi.rpz`,
file `hagezi-normal.rpz`) and `secondary` (zone `secondary.rpz`, file
`blocklist-secondary.rpz`) — each with its own one-line source file
(`blocklist-source` / `blocklist-source-secondary`). A no-arg run refreshes **both**:

```sh
sudo /opt/homebrew/etc/unbound/update-blocklist.sh
```

This reads each slot's source file (defaults: primary `1hosts-lite`, secondary
`oisd-big`), downloads each, sanity-checks it (**must** contain `CNAME` entries and
clear that source's line floor), writes both zonefiles, and rolls back any slot that
fails. Run this **before** `unbound-checkconf` on a fresh box (see §8) — **both**
zonefiles must exist for checkconf to pass, and this creates both. (An "off" secondary
still gets a valid empty zonefile, so checkconf passes either way.)

> **⚠️ On a fresh box this exits 1 — that's expected, keep going.** With unbound not
> yet running, the script's final `unbound-control` reload step fails (no control
> socket) *after* the zonefile has already been downloaded, validated, and staged.
> Tell the two cases apart by the error text: `connect ... failed` / socket errors =
> expected here, zonefile is in place, continue to §8's checkconf. `downloaded file
> looks wrong` or `checkconf failed; rolling back` = a real failure, stop and fix.
> The §Validate line below (`wc -l`) is the ground truth either way.

Validate (both zonefiles present and populated):
```sh
wc -l /opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz        # primary
wc -l /opt/homebrew/etc/unbound/blocklists/blocklist-secondary.rpz  # secondary
```

---

## 10. LaunchDaemon restore

All 12 hand-rolled daemons/jobs, plus the one managed by `brew services` (unbound):

```sh
# unbound's plist is managed BY brew services — do not hand-copy it; §8 already
# handled it via `sudo brew services start unbound`.

# Everything else: copy, fix ownership/mode, then bootstrap.
for p in local.unbound.alertnotify local.unbound.blackbox local.unbound.blocklist \
         local.unbound.clientstats local.unbound.contentcache local.unbound.exporter \
         local.unbound.grafana local.unbound.nodeexporter local.unbound.prometheus \
         local.unbound.roothints local.unbound.warmboot local.unbound.warmlist; do
  sudo cp ~/unbound-repo/launchd/$p.plist /Library/LaunchDaemons/$p.plist
  sudo chown root:wheel /Library/LaunchDaemons/$p.plist
  sudo chmod 644 /Library/LaunchDaemons/$p.plist
done
```

**Do not bootstrap these yet** — several depend on config files, binaries, or
directories from later steps (Prometheus, Grafana, exporters, content-cache). Each
later section tells you exactly when to `launchctl bootstrap` its daemon. Bootstrapping
early just means you'll see it in a crash-loop until its dependency shows up — annoying,
not harmful, but out of order.

---

## 11. Prometheus restore

```sh
sudo cp ~/unbound-repo/prometheus-etc/prometheus.yml       /opt/homebrew/etc/prometheus.yml
sudo cp ~/unbound-repo/prometheus-etc/prometheus.args      /opt/homebrew/etc/prometheus.args
sudo cp ~/unbound-repo/prometheus-etc/prometheus.rules.yml /opt/homebrew/etc/prometheus.rules.yml

promtool check config /opt/homebrew/etc/prometheus.yml     # must pass before starting
sudo launchctl bootstrap system /Library/LaunchDaemons/local.unbound.prometheus.plist
```

Validate:
```sh
curl -s http://127.0.0.1:9090/-/ready                       # "Prometheus Server is Ready."
```

---

## 12. Grafana restore

```sh
sudo cp ~/unbound-repo/grafana-etc/grafana.ini /opt/homebrew/etc/grafana/grafana.ini
sudo mkdir -p /opt/homebrew/etc/grafana/provisioning
sudo cp -R ~/unbound-repo/grafana-etc/provisioning/* /opt/homebrew/etc/grafana/provisioning/
sudo mkdir -p /opt/homebrew/etc/grafana/dashboards
sudo cp ~/unbound-repo/grafana-etc/dashboards/*.json /opt/homebrew/etc/grafana/dashboards/
sudo chown -R localadmin:admin /opt/homebrew/etc/grafana

sudo launchctl bootstrap system /Library/LaunchDaemons/local.unbound.grafana.plist
```

Validate — **Grafana binds `192.168.0.100` only, no loopback** (confirmed live
2026-07-07: `curl 127.0.0.1:3000` fails on the box itself; this is deliberate, same
LAN-only discipline as the rest of this stack, not a bug to "fix"):
```sh
curl -s http://192.168.0.100:3000/api/health   # {"database":"ok", ...}
```

First browser login is `admin`/`admin` — Grafana forces an immediate password change.
This is expected on every fresh install (see §4 — `grafana.db` isn't backed up on purpose).

---

## 13. Exporters restore

```sh
# node_exporter — brew formula + its args file + its daemon
sudo cp ~/unbound-repo/prometheus-etc/node_exporter.args /opt/homebrew/etc/node_exporter.args
sudo launchctl bootstrap system /Library/LaunchDaemons/local.unbound.nodeexporter.plist

# unbound_exporter — go-built binary already at /usr/local/sbin (§7)
sudo launchctl bootstrap system /Library/LaunchDaemons/local.unbound.exporter.plist

# blackbox_exporter — go-built binary (§7) + its probe-module config
sudo cp ~/unbound-repo/prometheus-etc/blackbox.yml /opt/homebrew/etc/blackbox.yml
sudo launchctl bootstrap system /Library/LaunchDaemons/local.unbound.blackbox.plist
```

Validate (expect **8/8** once everything below is also bootstrapped):
```sh
curl -s http://127.0.0.1:9090/api/v1/targets | grep -o '"health":"up"' | wc -l
```

---

## 14. Apple Content Cache exporter restore

```sh
mkdir -p ~/monitoring
sudo cp ~/unbound-repo/scripts/apple_content_cache_exporter.py /Users/localadmin/monitoring/apple_content_cache_exporter.py
```
(confirmed live 2026-07-07 via `local.unbound.contentcache.plist`'s `ProgramArguments` —
if you ever change the deploy path, update it there first, then here.)

**One-time permission fix required** (the textfile directory ships `root:admin 755`,
not group-writable, and `localadmin` is in `admin` but needs write access):
```sh
sudo mkdir -p /opt/homebrew/var/lib/node_exporter/textfile
sudo chmod g+w /opt/homebrew/var/lib/node_exporter/textfile
```

Enable Content Caching itself (macOS feature, not this repo): System Settings →
General → Sharing → Content Caching → on.

```sh
sudo launchctl bootstrap system /Library/LaunchDaemons/local.unbound.contentcache.plist
```

Validate:
```sh
curl -s http://127.0.0.1:9100/metrics | grep 'node_textfile_mtime_seconds{file="/opt/homebrew/var/lib/node_exporter/textfile/apple-content-cache.prom"}'
curl -s http://127.0.0.1:9100/metrics | grep -E '^(apple_content_cache_active|apple_content_cache_activated|apple_content_cache_status_ok) '
curl -s http://127.0.0.1:9100/metrics | grep '^node_textfile_scrape_error 0$'
```

---

## 15. RPZ clientstats exporter restore

This powers the dashboard's per-client RPZ block view only. It does **not** export
general DNS-client visibility, total queries, or NXDOMAINs; with query/reply logging
off, RPZ block log lines are the only per-client DNS trace this stack keeps.

```sh
mkdir -p ~/monitoring
sudo cp ~/unbound-repo/scripts/unbound_client_rpz_exporter.py /Users/localadmin/monitoring/unbound_client_rpz_exporter.py
sudo -u localadmin /usr/bin/python3 /Users/localadmin/monitoring/unbound_client_rpz_exporter.py --test

sudo launchctl bootstrap system /Library/LaunchDaemons/local.unbound.clientstats.plist
```

Validate:
```sh
curl -s http://127.0.0.1:9100/metrics | grep -E '^unbound_client_rpz_(blocks_5m|unattributed_5m|parse_errors_5m)'
curl -s http://127.0.0.1:9100/metrics | grep 'unbound_job_last_success_timestamp_seconds{task="clientstats"}'
launchctl print system/local.unbound.clientstats | grep -E 'state|last exit'
# expect last exit code = 0 after the first run
```

---

## 16. Firewall allow rules

```sh
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate on
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setstealthmode on
```

Add explicit allows for every LAN/loopback-facing binary, using the **resolved Cellar
path**, not the `/opt/homebrew/opt/...` symlink — the allow is keyed to that exact path,
and a `brew upgrade` changes it:

```sh
for b in unbound prometheus grafana; do
  bin=$(readlink -f /opt/homebrew/opt/$b/bin/$b 2>/dev/null || readlink -f /opt/homebrew/opt/$b/sbin/$b)
  sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$bin"
  sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp "$bin"
done
```

> **Observed nuance (2026-07-07), worth knowing:** on the current live box, Grafana has
> **no explicit allow entry at all** yet still answers on the LAN port, and Prometheus's
> existing entry was registered on the **symlink** path rather than the resolved Cellar
> path (unbound's entry is the only one on the fully-resolved path). Both currently
> work anyway — but don't copy that inconsistency forward. Use the resolved-path loop
> above for all three; it's the documented, brew-upgrade-safe convention.

Re-run this pair after every `brew upgrade` of prometheus or grafana (new Cellar path
invalidates the old allow). The loopback-only exporters (`:9100`, `:9115`, `:9167`)
never need an entry.

---

## 17. Tailscale / SSH access

**SSH (the primary remote path):**
```sh
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# Paste the PUBLIC key (from Honk's ~/.ssh/id_ed25519.pub) into:
echo "ssh-ed25519 AAAA... sein@Honk.local" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

sudo cp ~/unbound-repo/etc-ssh-sshd_config.d/500-keyonly.conf /etc/ssh/sshd_config.d/500-keyonly.conf
sudo sshd -t && echo "sshd config OK"        # MUST pass before trusting this
```

> **⚠️ Verify key login from a SECOND terminal before closing your first one.** macOS
> serves sshd per-connection (config re-reads on every new connection, no daemon
> restart needed) — but if the key doesn't work, you want your original session still
> open to fix it, not a locked door.
> ```sh
> ssh localadmin@<mini-ip-or-tailnet-name> 'echo KEYLOGIN_OK'   # from a fresh terminal
> ```

**Tailscale (the secondary/remote access path):**

- Install from **tailscale.com/download** (the `Tailscale.app` System Extension) — **not**
  `brew install tailscale`. A stray `homebrew.mxcl.tailscale.plist` was found on the
  live box pointing at a now-nonexistent Homebrew Cellar path (an orphan from an old,
  since-removed brew install) — the box's actual Tailscale connectivity has only ever
  come from the `Tailscale.app` System Extension. Don't recreate the brew-based leftover.
- Sign in with your existing account. This will likely register as a **new node**
  unless you remove the old machine entry first in the Tailscale admin console — decide
  before signing in whether you want to reuse the old device's tailnet identity or let
  it be new (a new node means updating any place that referenced the old
  `mini.tail3fd0f4.ts.net` name, including your SSH usage in this very document).

**Screen Sharing:** leave it **off** by default. It's controlled on-demand:
```sh
sudo cp ~/unbound-repo/usr-local-sbin/screenshare-on  /usr/local/sbin/
sudo cp ~/unbound-repo/usr-local-sbin/screenshare-off /usr/local/sbin/
sudo chmod 755 /usr/local/sbin/screenshare-on /usr/local/sbin/screenshare-off
# use later with:  sudo /usr/local/sbin/screenshare-on   (and -off when done)
```

**ntfy (phone alerts) — the one secret that must be recreated or restored:**
```sh
mkdir -p ~/monitoring
# If you saved it in §5:
cp ~/mini-final-backup/ntfy.topic ~/monitoring/ntfy.topic
# If not, generate a new random topic name and re-subscribe the phone app to it:
openssl rand -hex 16 > ~/monitoring/ntfy.topic
chmod 600 ~/monitoring/ntfy.topic
```
Then deploy the notifier and its daemon (deploy path confirmed live 2026-07-07 via
`local.unbound.alertnotify.plist`'s `ProgramArguments`):
```sh
sudo cp ~/unbound-repo/scripts/prometheus_alert_notifier.py /Users/localadmin/monitoring/prometheus_alert_notifier.py
sudo cp ~/unbound-repo/launchd/local.unbound.alertnotify.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/local.unbound.alertnotify.plist
```
Test the channel:
```sh
curl -H "Title: test" -d "hello" https://ntfy.sh/$(cat ~/monitoring/ntfy.topic)
```

---

## 18. Validation checklist

Work this in order. Every check should pass before you consider the rebuild complete.

- [ ] **Resolver answers:** `dig @127.0.0.1 cloudflare.com | grep status:` → `NOERROR`, with `ad` flag (DNSSEC validated)
- [ ] **Ad-blocking works:** `dig @127.0.0.1 google-analytics.com | grep -E 'status:|flags:'` → `NXDOMAIN` + `aa`
- [ ] **DNSSEC validation is real:** `dig @127.0.0.1 dnssec-failed.org | grep status:` → `SERVFAIL`
- [ ] **`.lan` zone works:** `dig @127.0.0.1 mini.lan +short` → returns `192.168.0.100`
- [ ] **All scrape targets up:** `curl -s http://127.0.0.1:9090/api/v1/targets | grep -o '"health":"up"' | wc -l` → **8**
- [ ] **No alerts firing:** `curl -s http://127.0.0.1:9090/api/v1/alerts` → empty alerts array
- [ ] **No textfile scrape errors:** `curl -s http://127.0.0.1:9100/metrics | grep node_textfile_scrape_error` → `0`
- [ ] **Grafana reachable + dashboards render:** open `http://192.168.0.100:3000` from a LAN device, confirm all 7 dashboards load with data
- [ ] **ntfy pushes work:** the test push from §17 arrived on your phone
- [ ] **Job breadcrumbs are fresh:** `curl -s http://127.0.0.1:9100/metrics | grep unbound_job_last_success` → all 6 tasks present (will show old timestamps until each job's next scheduled run — that's expected, not a failure)
- [ ] **Firewall correct:** `sudo lsof -nP -iTCP -sTCP:LISTEN` shows exactly what §22's port table expects — nothing extra
- [ ] **SSH key-only confirmed:** a forced-password connection attempt is refused (`Permission denied (publickey)`)

Only after every box above is checked, proceed to §19.

---

## 19. DNS client cutover

If this rebuild happened on a **replacement** box (new hardware, same IP plan), or you
temporarily pointed clients elsewhere during the outage:

- [ ] Confirm the new box has IP **192.168.0.100** (static reservation or manual assignment — match whatever the router's DHCP already advertises as DNS).
- [ ] Router DHCP should advertise DNS as `192.168.0.100` **primary** with `9.9.9.9` fallback — that fallback is standing config, not an outage hack; don't remove it. If you pointed *primary* elsewhere during the outage, restore it now — not before validation.
- [ ] Flush client DNS caches if a device seems to still be using a stale resolver (`sudo dscacheutil -flushcache` on Macs; most other devices just need a network toggle).
- [ ] Watch Grafana's "Unbound DNS" dashboard for 10–15 minutes as real household traffic starts hitting the box — confirm cache hit rate climbs and no SERVFAIL spike appears.

---

## 20. Rollback plan

- **If the rebuild is going wrong and the old box (or its disk) still exists:** stop,
  don't make it worse. Re-point DHCP at nothing / a public resolver temporarily (see the
  design note in this repo's DNS-failover discussion) while you diagnose, rather than
  forcing a half-working rebuild onto the LAN.
- **If a specific config change (not the whole rebuild) is the problem:** this repo is
  the restore source — `git log` → identify the last-known-good commit → redeploy just
  that file → validate (`unbound-checkconf` / `promtool check config` / `plutil -lint`)
  → reload only the one affected service. Don't restart everything to fix one file.
- **The blocklist rolls itself back automatically** on a failed validation — you never
  need to manually revert `hagezi-normal.rpz`.
- **If Grafana looks wrong after a dashboard edit:** the JSON in this repo is the source
  of truth; redeploy the file from git, don't try to fix it by clicking around the UI
  (UI edits don't persist anyway — see DASHBOARDS.md).

---

## 21. Do-not-do list

- **Don't restart or reload unbound on an unvalidated config.** `unbound-checkconf`
  first, every time, no exceptions — it's the LAN's primary resolver; breaking it
  degrades every client to the unfiltered fallback.
- **Don't run `update-blocklist.sh` after `unbound-checkconf` on a fresh box** — the
  ordering in §8/§9 is deliberate; checkconf needs the RPZ zonefile to already exist.
- **Don't `brew services start unbound` without sudo.** Non-sudo creates a second,
  user-domain copy that fights the root one — the exact "two unbound processes"
  gotcha this repo has hit before. Verify with `lsof -i :53`: expect exactly one process.
- **Don't `brew services start` prometheus/grafana/node_exporter at all.** They are
  hand-rolled `localadmin` system LaunchDaemons, not `brew services`-managed — starting
  them the brew way creates a login-scoped duplicate fighting the system one.
- **Don't hand-edit `hagezi-normal.rpz`.** It's clobbered nightly. Allowlist exceptions
  go in `hagezi-allow.rpz`, which **is** tracked in the repo.
- **Don't assume `grafana.db` needs restoring.** It doesn't — see §4. Restoring it is
  more likely to reintroduce stale state than help.
- **Don't reinstall Tailscale via Homebrew.** See §17 — the real install path is the
  System Extension app, not the brew formula (whose old LaunchDaemon is dead weight).
- **Don't blindly restart every process `ps aux` shows you.** Not everything running on
  a live box is supposed to be there — a stray manually-started `iperf3 -s -D` server
  was found and killed during a 2026-07-07 review; it wasn't part of any job. Cross-check
  against this repo's `launchd/` directory before assuming a process belongs.
- **Don't trust the live firewall's current allow-list paths as a template without
  checking them** — see the observed symlink-vs-Cellar-path inconsistency in §16.
- **Don't drag files into this repo with Finder.** `scp`/`rsync` only.
- **Don't run `tailscale funnel`.** Nothing in this stack is meant to face the public internet.
- **Don't skip the fresh-login verification in §17** before closing your original SSH session.

---

## 22. Appendix: paths, ports, services, commands

### Services & restart commands
| Service | Managed by | Restart |
|---|---|---|
| unbound | `brew services` (sudo only) | `sudo brew services restart unbound` |
| prometheus | hand-rolled LaunchDaemon | `sudo launchctl kickstart -k system/local.unbound.prometheus` |
| grafana | hand-rolled LaunchDaemon | `sudo launchctl kickstart -k system/local.unbound.grafana` |
| node_exporter | hand-rolled LaunchDaemon | `sudo launchctl kickstart -k system/local.unbound.nodeexporter` |
| unbound_exporter | hand-rolled LaunchDaemon | `sudo launchctl kickstart -k system/local.unbound.exporter` |
| blackbox_exporter | hand-rolled LaunchDaemon | `sudo launchctl kickstart -k system/local.unbound.blackbox` |
| alertnotify | hand-rolled LaunchDaemon (periodic) | `sudo launchctl kickstart system/local.unbound.alertnotify` |
| contentcache | hand-rolled LaunchDaemon (periodic) | `sudo launchctl kickstart system/local.unbound.contentcache` |

### Ports
| Port | Bind | Service |
|---|---|---|
| 53 | loopback + LAN | unbound |
| 3000 | LAN | Grafana |
| 9090 | loopback + LAN (explicit dual bind, not `0.0.0.0`) | Prometheus |
| 9100 | loopback only | node_exporter |
| 9115 | loopback only | blackbox_exporter |
| 9167 | loopback only | unbound_exporter |
| 22 | LAN + tailnet | SSH (key-only) |
| 5900 | off by default | Screen Sharing (on-demand) |

**Closed on purpose** (disabled 2026-07-07, don't reopen without reason): SMB `:445`,
local Kerberos KDC `:88`.

### Key file locations on the live box
| Purpose | Path |
|---|---|
| Unbound config | `/opt/homebrew/etc/unbound/unbound.conf` |
| Blocklists | `/opt/homebrew/etc/unbound/blocklists/` |
| Prometheus config | `/opt/homebrew/etc/prometheus.{yml,args,rules.yml}` |
| Grafana config | `/opt/homebrew/etc/grafana/` |
| LaunchDaemons | `/Library/LaunchDaemons/local.unbound.*.plist` |
| Scheduled-job logs | `/opt/homebrew/var/log/unbound-*.log` |
| node_exporter textfile dir | `/opt/homebrew/var/lib/node_exporter/textfile/` |
| ntfy secret | `~localadmin/monitoring/ntfy.topic` (600, never in git) |
| Prometheus TSDB | `/opt/homebrew/var/prometheus` |

### Validation command reference
```sh
unbound-checkconf                                        # before any unbound reload
promtool check config /opt/homebrew/etc/prometheus.yml    # before any prometheus restart
promtool check rules  /opt/homebrew/etc/prometheus.rules.yml
plutil -lint /Library/LaunchDaemons/local.unbound.<name>.plist
python3 -m json.tool grafana-etc/dashboards/<name>.json    # before deploying a dashboard edit
sudo sshd -t                                               # before trusting an sshd_config.d change
```

---

*This document is generated from the live state of `mini` as observed 2026-07-07.
Regenerate/re-verify facts (package versions, firewall entries, plist inventory) against
the actual box before relying on this during a real recovery if significant time has
passed since this date.*
