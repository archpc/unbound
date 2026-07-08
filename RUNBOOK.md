# Mini DNS Server — Operator Runbook

**Plain-language, top-to-bottom guide to the Mac mini that runs your home DNS.**
If you only read one file, read this one. It assumes you can open Terminal and
copy-paste — nothing else. For the deep "why it's built this way" detail, see
[CLAUDE.md](CLAUDE.md); this file is "how do I actually do the thing."

---

## 0. The 20-second orientation

- **What it is:** a Mac mini named `mini` at **192.168.0.100** on your home network.
- **Its main job:** it's the DNS resolver for every device in the house — it turns
  names like `google.com` into IP addresses, blocks ads/trackers, and validates
  that answers are genuine (DNSSEC).
- **Its other jobs:** caches Apple software downloads for the LAN, and runs a
  monitoring stack (Grafana dashboards + phone alerts) that watches all of the above.
- **If DNS breaks, the whole house loses the internet** (names stop resolving). That's
  why this box matters and why there are so many guardrails.

| I want to… | Go to |
|---|---|
| Log into the box | [§1](#1-how-to-get-in) |
| Check everything is healthy | [§2](#2-is-it-healthy-the-60-second-check) |
| Fix a wrongly-blocked website | [§4.1](#41-a-website-is-wrongly-blocked-unblock-it) |
| The internet is down | [§5.1](#51-the-internet-is-down-for-the-whole-house) |
| A phone alert fired | [§5.4](#54-i-got-a-phone-alert-ntfy) |
| See the dashboards | [§1](#1-how-to-get-in) |

---

## 1. How to get in

### SSH (the main way — a terminal on the box)
From your Mac (**Honk**, which holds the login key):
```sh
ssh localadmin@mini.tail3fd0f4.ts.net      # works from anywhere on your tailnet
ssh localadmin@192.168.0.100               # works on the home Wi-Fi/ethernet
```
- Login is **key-only** — no password is asked, and no password will work. The key
  lives on Honk. If you're on a different computer, it won't let you in (by design).
- You land in a normal shell as user `localadmin`.

### Dashboards (the pretty way — read-only health)
On the home network, open in a browser:
- **Grafana (the dashboards):** http://192.168.0.100:3000
- **Prometheus (raw metrics/alerts):** http://192.168.0.100:9090/alerts

**On a fresh rebuild only**, first Grafana login is `admin` / `admin` and it forces a
password change immediately. On the live box, use the password you already set — it's
not stored here (see [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md) §4 for why).

### Screen Sharing (rare — a full desktop)
Kept **off** for security. Turn it on only when you need a desktop, then off again:
```sh
sudo /usr/local/sbin/screenshare-on        # opens it, prints "listening on :5900"
# ...connect with Screen Sharing.app to 192.168.0.100, do your thing...
sudo /usr/local/sbin/screenshare-off       # closes it again
```
(Full path is required — see [§7](#7-security-what-is-locked-down).)

---

## 2. Is it healthy? The 60-second check

### Fastest possible check (from ANY device on the home network)
```sh
dig @192.168.0.100 cloudflare.com +short
```
**Good:** it prints one or more IP addresses. That means DNS is alive and answering.
**Bad:** it hangs or says "no servers could be reached" → jump to [§5.1](#51-the-internet-is-down-for-the-whole-house).

### Fuller check (SSH in first, then paste these)
```sh
# 1. Is the resolver answering, blocking, and validating? (the three canaries)
dig @127.0.0.1 cloudflare.com          | grep -E 'status:|flags:'   # NOERROR, has 'ad' = DNSSEC ok
dig @127.0.0.1 google-analytics.com    | grep -E 'status:|flags:'   # NXDOMAIN + 'aa' = ad-blocking on
dig @127.0.0.1 dnssec-failed.org       | grep status:               # SERVFAIL = validation working

# 2. Are all the monitoring pieces up? (expect 8)
curl -s 'http://127.0.0.1:9090/api/v1/targets' | grep -o '"health":"up"' | wc -l

# 3. Any alerts firing right now? (expect nothing / empty list)
curl -s 'http://127.0.0.1:9090/api/v1/alerts'

# 4. Any broken metric files? (expect 0)
curl -s http://127.0.0.1:9100/metrics | grep node_textfile_scrape_error
```
If all four look as annotated, the box is healthy. If #2 is under 8 or #3 shows a
firing alert, see [§5](#5-when-something-is-wrong).

> **Memorize one fact:** `google-analytics.com` must come back **NXDOMAIN**. That's the
> "is ad-blocking working" canary. If it resolves to a real IP, blocking has broken.

---

## 3. What's running (in plain language)

The box runs a chain of small services. You rarely touch them individually, but here's
what each does and how to restart it if a dashboard says it's down.

| Service | What it does | Restart command (on the box) |
|---|---|---|
| **unbound** | The DNS resolver itself. The important one. | `sudo brew services restart unbound` *(drops the cache — see §4.4)* |
| **prometheus** | Collects and stores all the metrics | `sudo launchctl kickstart -k system/local.unbound.prometheus` |
| **grafana** | Draws the dashboards | `sudo launchctl kickstart -k system/local.unbound.grafana` |
| **node_exporter** | Reports CPU/memory/disk/network | `sudo launchctl kickstart -k system/local.unbound.nodeexporter` |
| **unbound_exporter** | Reports unbound's internal stats | `sudo launchctl kickstart -k system/local.unbound.exporter` |
| **blackbox_exporter** | Actively test-queries DNS from outside | `sudo launchctl kickstart -k system/local.unbound.blackbox` |
| **alertnotify** | Pushes alerts to your phone (ntfy) | `sudo launchctl kickstart system/local.unbound.alertnotify` |

Scheduled background jobs (they run themselves; you don't start them):
- **blocklist** — downloads the fresh ad-blocklist nightly (03:30).
- **warmlist / warmboot** — keep the DNS cache warm after reboots.
- **roothints** — refreshes the DNS root-server list monthly.
- **contentcache** — reports Apple content-cache stats every 5 min.

To see if a specific job is alive: `launchctl print system/local.unbound.<name> | grep -E 'state|last exit'`
(no sudo needed to read).

---

## 4. Everyday tasks

### 4.1. A website is wrongly blocked — unblock it
First, **confirm it's actually the blocklist** (not just a dead site). On the box:
```sh
dig @127.0.0.1 thesite.com | grep -E 'status:|flags:'
```
- `NXDOMAIN` **with `aa` in the flags** = the blocklist is blocking it → continue below.
- `NXDOMAIN` **without `aa`** = the site genuinely doesn't exist. Not our doing. Stop.

To unblock, edit the **allowlist** (never the blocklist — it's overwritten nightly):
```sh
sudo nano /opt/homebrew/etc/unbound/blocklists/hagezi-allow.rpz
```
Copy the existing `rpz-passthru` pattern for both the bare name and the wildcard, and
**bump the SOA serial number** at the top of the file (increase it by 1). Save, then:
```sh
sudo unbound-control auth_zone_reload hagezi.allow      # reload just the allowlist
dig @127.0.0.1 thesite.com | grep status:               # now NOERROR = unblocked
```
Finally, copy the edited file back into this repo on Honk and commit it (so it survives
a rebuild). See [CLAUDE.md](CLAUDE.md) "Adding / removing / testing block+allow rules".

### 4.2. Block a site the list misses
Add one line to unbound's config (the blocklist file gets clobbered nightly, so don't
edit that):
```sh
sudo nano /opt/homebrew/etc/unbound/unbound.conf      # add: local-zone: "badsite.com." always_nxdomain
unbound-checkconf                                      # MUST say "no errors" before reloading
sudo unbound-control reload
```

### 4.3. Update the ad-blocklist manually (it also runs nightly)
```sh
sudo bash /opt/homebrew/etc/unbound/update-blocklist.sh
```
It downloads the list, sanity-checks it (>100k lines, has CNAME entries), swaps it in,
and automatically rolls back if the new file is bad. Safe to run anytime.

### 4.4. Warm the cache (after a restart)
Restarting unbound empties its cache, so the first lookups are slow. Refill it:
```sh
sudo /Users/localadmin/warmup/warm-unbound-cache.sh
```

### 4.5. Screen Sharing on/off
See [§1](#1-how-to-get-in). On by demand, off when done.

---

## 5. When something is wrong

### 5.1. The internet is down for the whole house
Work down this list **in order** — each step rules out one layer. All commands run on
the box (SSH in; if you can't even SSH, the box may be off/rebooting — check power).

```sh
# STEP 1 — is unbound answering at all?
dig @127.0.0.1 +time=2 +tries=1 cloudflare.com | grep status:
#   NOERROR -> DNS is FINE; the problem is the client or the router's DHCP. Stop here.
#   times out -> go to STEP 2.
#   SERVFAIL -> go to STEP 3.

# STEP 2 — is the unbound process even running?
ps aux | grep '[u]nbound'
#   nothing -> restart it:  sudo brew services restart unbound
#              then warm it: sudo /Users/localadmin/warmup/warm-unbound-cache.sh
#              (warm-at-boot.sh is the launchd boot-time wrapper — it waits out the
#              opendirectoryd race on every boot. You just restarted it yourself, so
#              go straight to warm-unbound-cache.sh, not the boot wrapper.)

# STEP 3 — is the upstream internet path dead, or is it just DNSSEC?
dig @1.1.1.1 +time=2 +tries=1 cloudflare.com | grep status:
#   ALSO fails -> it's your internet/router/ISP, NOT this box. Do NOT edit unbound.
#                 Reboot the modem/router. (This was the real cause on 2026-07-01.)
#   works, but unbound SERVFAILs one site -> that one site's DNSSEC is genuinely broken.
```

> **Known non-issue:** right after a reboot, `unbound` (and grafana/prometheus) show
> "last exit code = 1" for a few seconds — a harmless boot-timing race that self-heals
> in ~10s. Don't chase it. Only worry if it keeps happening.

### 5.2. A single website won't load
```sh
dig @127.0.0.1 thesite.com | grep -E 'status:|flags:'
```
- `NXDOMAIN` + `aa` = blocklist is blocking it → unblock per [§4.1](#41-a-website-is-wrongly-blocked-unblock-it).
- `NXDOMAIN` without `aa` = the site really doesn't exist.
- `NOERROR` but still won't load = **not a DNS problem** — it's the site or your browser.

### 5.3. The dashboard (Grafana) won't open
- Confirm you're on the home network — Grafana is **only** reachable at
  http://192.168.0.100:3000 on the LAN (not over the internet/tailnet, by design).
- SSH in and restart it: `sudo launchctl kickstart -k system/local.unbound.grafana`
- If it broke right after a `brew upgrade`, the firewall allow was lost — see
  [CLAUDE.md](CLAUDE.md) "macOS application firewall gotcha".

### 5.4. I got a phone alert (ntfy)
The alert title tells you which tripwire fired. Common ones and what they mean:
- **UpstreamDNSPathDown** — the internet path to public DNS is dead. Check modem/router.
- **RPZCanaryNotBlocking** — ad-blocking silently stopped. Re-run the blocklist update ([§4.3](#43-update-the-ad-blocklist-manually-it-also-runs-nightly)).
- **UnboundDown / UnboundExporterDown** — the resolver or its stats reporter is down. [§5.1](#51-the-internet-is-down-for-the-whole-house) step 2.
- **DiskAlmostFull / RootDiskFreePredicted…** — the box is running low on disk. Investigate what's filling it.
- **…Stale** (BlocklistUpdateStale, WarmbootDidNotRun, AlertNotifierStale, etc.) — a
  background job hasn't run when it should have. Check its log under
  `/opt/homebrew/var/log/`.

A **resolved** alert also pushes to your phone (lower priority), so you'll know when it clears.

---

## 6. Backups

The **repo on Honk is the backup** — every config and script on the box is committed
there. To restore the box, you re-deploy from the repo.

Make a point-in-time tarball of the live config (run on the box) before big changes:
```sh
tar czf ~/mini-backup-$(date +%Y%m%d-%H%M%S).tar.gz \
  /opt/homebrew/etc/unbound /opt/homebrew/etc/{prometheus,blackbox}* \
  /opt/homebrew/etc/grafana /Library/LaunchDaemons/local.unbound.*.plist \
  /etc/ssh/sshd_config.d/500-keyonly.conf 2>/dev/null
```
To undo a bad change: on Honk, `git log` → `git revert <the-bad-commit>` → re-deploy that
one file to the box → validate → restart just that service.

---

## 7. Security — what is locked down

This box is **LAN + your private tailnet only**. Nothing here is meant to face the
public internet. Current posture (as of 2026-07-07):

- **SSH is key-only.** Passwords are refused; the only key is on Honk. (Set in
  `/etc/ssh/sshd_config.d/500-keyonly.conf`.)
- **The macOS firewall is on.**
- **Unused services are off:** file sharing (SMB), Screen Sharing, and the local
  Kerberos service were all shut off. Screen Sharing is on-demand only ([§1](#1-how-to-get-in)).
- **DNS, Prometheus, and Grafana bind only to loopback + the LAN IP** — never the
  tailnet or Wi-Fi interfaces.
- To audit what's listening at any time: `sudo lsof -nP -iTCP -sTCP:LISTEN`.

**Gotcha:** commands in `/usr/local/sbin` (like the screenshare helpers) must be called
by **full path** under sudo — `sudo /usr/local/sbin/screenshare-on` — because sudo's
`secure_path` doesn't include that directory.

---

## 8. Never do these (they have bitten before)

- **Never reload/restart unbound on a config you haven't validated** with
  `unbound-checkconf` — it's the only DNS resolver the whole house has.
- **Never edit `hagezi-normal.rpz`** (the blocklist) — it's overwritten nightly. Put
  exceptions in `hagezi-allow.rpz`.
- **Never run `brew services start` on prometheus/grafana/node_exporter** — they're
  managed by launchd; starting them that way creates a fighting duplicate.
- **Never drag files into the repo with Finder** — use `scp`/`rsync`. Finder has
  deleted tracked files here before.
- **Never run `tailscale funnel`** — nothing here should be exposed to the internet.

---

## 9. Going deeper

[CLAUDE.md](CLAUDE.md) has the full technical reference: every file's purpose, the
monitoring internals, all the launchd jobs, the alert-rule reasoning, and the historical
"why" behind each decision. This runbook is deliberately the shallow, do-the-task layer
on top of it.
