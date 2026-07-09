#!/usr/bin/env bash
#
# update-blocklist.sh — download an RPZ blocklist, sanity-check it, atomically
# swap it in, and reload just that zone. Source is pluggable (see SOURCE below)
# so you can try out different lists without touching anything else - the
# destination file/zone name stay "hagezi-normal.rpz" / "hagezi.rpz" regardless
# of which source is actually loaded (see unbound.conf's comment on this).
#
# Run by:    local.unbound.blocklist LaunchDaemon, daily 03:30, as root, with
#            NO argument - so every nightly run re-fetches hagezi-normal
#            (the default) NO MATTER WHAT you last ran by hand. A manual run
#            with a source argument is a ONE-TIME override: it's live until
#            03:30 tomorrow, then silently reverts to hagezi-normal. To make
#            a different source the permanent nightly choice, add it as a
#            third <string> in local.unbound.blocklist.plist's
#            ProgramArguments and redeploy - there is no state file.
# Manual:    sudo /opt/homebrew/etc/unbound/update-blocklist.sh [source]
#            (safe anytime; omit [source] for hagezi-normal)
#            Known sources: hagezi-normal (default) hagezi-pro
#              1hosts-lite 1hosts-xtra oisd-small oisd-big
# Needs:     root (writes unbound-owned zonefile + talks to the control socket).
# Writes:    /opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz (+ .bak of
#            the previous version), success breadcrumb .prom for node_exporter.
# Logs:      /opt/homebrew/var/log/unbound-blocklist.log (launchd) or stdout.
# Exit:      0 = updated + reloaded; 1 = unknown source name, bad download, or
#            checkconf failure (auto-rolled-back) — DNS keeps serving the
#            previous list either way.
# Verify:    dig @127.0.0.1 google-analytics.com  -> NXDOMAIN with 'aa' flag
#            (present in all six known sources - safe canary regardless of
#            which one is active)
#            curl -s localhost:9100/metrics | grep unbound_blocklist_lines
set -euo pipefail

# SOURCE picks which upstream RPZ feed to use; NOT persisted anywhere (no
# state file) - $1 defaults to hagezi-normal, which is exactly what the
# LaunchDaemon gets every night since its ProgramArguments never pass one.
# See the header for how to make a different source the permanent default.
# Bash 3.2 (macOS system /bin/bash, which is what the LaunchDaemon hardcodes -
# no associative arrays), so a case statement, not a lookup table. MIN_LINES
# floors are ~65% of the live count observed 2026-07-09 - loose enough to
# absorb normal day-to-day list churn, tight enough to catch a truncated
# download or an HTML error page in place of data.
SOURCE="${1:-hagezi-normal}"
case "$SOURCE" in
    hagezi-normal)
        SRC="https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/rpz/multi.txt"
        MIN_LINES=100000   # observed ~316k
        ;;
    hagezi-pro)
        SRC="https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/rpz/pro.txt"
        MIN_LINES=300000   # observed 463994
        ;;
    1hosts-lite)
        SRC="https://raw.githubusercontent.com/badmojr/1Hosts/master/Lite/rpz.txt"
        MIN_LINES=130000   # observed 199328
        ;;
    1hosts-xtra)
        SRC="https://raw.githubusercontent.com/badmojr/1Hosts/master/Xtra/rpz.txt"
        MIN_LINES=900000   # observed 1423228
        ;;
    oisd-small)
        SRC="https://small.oisd.nl/rpz"
        MIN_LINES=70000    # observed 111024
        ;;
    oisd-big)
        SRC="https://big.oisd.nl/rpz"
        MIN_LINES=600000   # observed 987218
        ;;
    *)
        echo "unknown source '$SOURCE'; known: hagezi-normal hagezi-pro 1hosts-lite 1hosts-xtra oisd-small oisd-big" >&2
        exit 1
        ;;
esac
DEST="/opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz"
TMP="$(mktemp)"
NEW="${DEST}.new"   # staged on the same filesystem as DEST so mv(1) is atomic
BAK="${DEST}.bak"
CTRL="/opt/homebrew/sbin/unbound-control"
CHECK="/opt/homebrew/sbin/unbound-checkconf"
trap 'rm -f "$TMP" "$NEW"' EXIT

curl -fsS --max-time 90 "$SRC" -o "$TMP"

if ! grep -q 'CNAME' "$TMP" || [ "$(wc -l < "$TMP")" -lt "$MIN_LINES" ]; then
    echo "downloaded file looks wrong for source '$SOURCE' (want >= $MIN_LINES lines); aborting" >&2
    exit 1
fi

# Plain `if` (not `[ ... ] && ...`): under set -e, a failing AND-list at top
# level aborts the script, which would kill a first run when DEST doesn't exist.
if [ -f "$DEST" ]; then
    cp -p "$DEST" "$BAK"
fi
cp "$TMP" "$NEW"
chown unbound:unbound "$NEW"
chmod 644 "$NEW"
mv "$NEW" "$DEST"   # atomic: a racing reload never sees a half-written zone

if ! "$CHECK" >/dev/null 2>&1; then
    echo "checkconf failed; rolling back" >&2
    [ -f "$BAK" ] && mv "$BAK" "$DEST" || echo "no backup present; $DEST left as downloaded" >&2
    exit 1
fi

# Don't discard stderr: a failed zone reload must show up in cron/launchd logs.
"$CTRL" auth_zone_reload hagezi.rpz || "$CTRL" reload
echo "updated: $(wc -l < "$DEST") lines"

# Success breadcrumb for the monitoring stack: node_exporter's textfile
# collector serves these gauges, and prometheus.rules.yml alarms if the
# timestamp goes stale (i.e. this job silently stopped running/succeeding).
METRICS_DIR="/opt/homebrew/var/lib/node_exporter/textfile"
mkdir -p "$METRICS_DIR" 2>/dev/null || true
if [ -w "$METRICS_DIR" ]; then
    {
        echo '# TYPE unbound_job_last_success_timestamp_seconds gauge'
        printf 'unbound_job_last_success_timestamp_seconds{task="blocklist"} %s\n' "$(date +%s)"
        echo '# TYPE unbound_blocklist_lines gauge'
        printf 'unbound_blocklist_lines %s\n' "$(wc -l < "$DEST" | tr -d ' ')"
    } > "$METRICS_DIR/unbound-blocklist.prom.$$" \
      && mv "$METRICS_DIR/unbound-blocklist.prom.$$" "$METRICS_DIR/unbound-blocklist.prom"
fi
