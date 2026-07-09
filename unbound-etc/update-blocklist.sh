#!/usr/bin/env bash
#
# update-blocklist.sh — download RPZ blocklist(s), sanity-check, atomically
# swap in, and reload just the affected zone(s). Two INDEPENDENT "slots" load
# side by side (a domain blocked by EITHER slot is blocked); each slot's source
# is pluggable and independently swappable.
#
#   Slots:
#     primary   -> zonefile hagezi-normal.rpz, zone "hagezi.rpz",
#                  source file blocklist-source
#     secondary -> zonefile blocklist-secondary.rpz, zone "secondary.rpz",
#                  source file blocklist-source-secondary
#   (primary keeps the legacy hagezi* names for backward-compat with the
#   existing unbound.conf / .bak / muscle memory; secondary is clean-named.
#   The allowlist zone hagezi.allow is evaluated before BOTH, so a passthru
#   entry still overrides either blocklist - see unbound.conf.)
#
# Run by:  local.unbound.blocklist LaunchDaemon, daily 03:30, as root, with NO
#          argument -> refreshes EVERY slot from its own source file (whatever
#          you last made permanent). The plist itself never changes for this.
# Manual:  sudo update-blocklist.sh                 # all slots, from source files
#          sudo update-blocklist.sh <source>         # one-time, primary slot only
#          sudo update-blocklist.sh <source> <slot>  # one-time, that slot only
#          sudo update-blocklist.sh off secondary    # empty (disable) secondary
#          A [source] argument is a ONE-TIME override: it does NOT touch the
#          source file, so the next nightly run reverts to whatever the file
#          says. To make a source permanent for a slot, write it to that file:
#            echo oisd-big | sudo tee /opt/homebrew/etc/unbound/blocklist-source-secondary
#   Known sources: hagezi-normal hagezi-pro 1hosts-lite 1hosts-xtra
#                  oisd-small oisd-big     (plus "off", secondary only)
# Needs:   root (writes unbound-owned zonefiles + talks to the control socket).
# Exit:    0 = every attempted slot updated + reloaded; 1 = at least one slot
#          failed (unknown source, bad download, checkconf). A failed slot keeps
#          its PREVIOUS zonefile (rolled back) and DNS keeps serving; other slots
#          are unaffected.
# Metrics: single textfile unbound-blocklist.prom, rewritten each run -
#            unbound_job_last_success_timestamp_seconds{task="blocklist"}
#              = now, but ONLY if every attempted slot succeeded; on a partial
#              failure the PRIOR value is preserved, so BlocklistUpdateStale
#              still fires if a slot stays broken past its window.
#            unbound_blocklist_lines{slot="primary|secondary"}
#              = current line count per ON slot (an "off" secondary emits none,
#              so its ~3-line empty zone never trips BlocklistSuspiciouslySmall).
# KNOWN GAP: freshness is job-level (one timestamp), not per-slot - a partial
#          failure is caught (breadcrumb withheld) but the alert can't say WHICH
#          slot. Fine for two slots; revisit if this ever grows more.
# Verify:  dig @127.0.0.1 google-analytics.com -> NXDOMAIN + 'aa' (in every source)
#          curl -s localhost:9100/metrics | grep unbound_blocklist_lines
#
# NB: set -u + pipefail but NOT -e: per-slot failures are handled explicitly and
# the script must continue to the other slot AND still write metrics.
set -uo pipefail

ETC="/opt/homebrew/etc/unbound"
BL="$ETC/blocklists"
CTRL="/opt/homebrew/sbin/unbound-control"
CHECK="/opt/homebrew/sbin/unbound-checkconf"
METRICS_DIR="/opt/homebrew/var/lib/node_exporter/textfile"
PROM="$METRICS_DIR/unbound-blocklist.prom"
SLOTS="primary secondary"

# resolve_source NAME -> sets SRC, MIN_LINES; returns 1 on unknown.
# MIN_LINES floors are ~65% of the live count observed 2026-07-09 - loose
# enough to absorb normal list churn, tight enough to catch a truncated
# download or an HTML error page served in place of data.
resolve_source() {
    case "$1" in
        hagezi-normal) SRC="https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/rpz/multi.txt"; MIN_LINES=100000 ;;  # ~316k
        hagezi-pro)    SRC="https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/rpz/pro.txt";   MIN_LINES=300000 ;;  # 463994
        1hosts-lite)   SRC="https://raw.githubusercontent.com/badmojr/1Hosts/master/Lite/rpz.txt";   MIN_LINES=130000 ;;  # 199328
        1hosts-xtra)   SRC="https://raw.githubusercontent.com/badmojr/1Hosts/master/Xtra/rpz.txt";   MIN_LINES=900000 ;;  # 1423228
        oisd-small)    SRC="https://small.oisd.nl/rpz"; MIN_LINES=70000 ;;   # 111024
        oisd-big)      SRC="https://big.oisd.nl/rpz";   MIN_LINES=600000 ;;  # 987218
        *) echo "unknown source '$1'; known: hagezi-normal hagezi-pro 1hosts-lite 1hosts-xtra oisd-small oisd-big off" >&2; return 1 ;;
    esac
}

# resolve_slot NAME -> sets DEST, ZONE, SRCFILE; returns 1 on unknown.
resolve_slot() {
    case "$1" in
        primary)   DEST="$BL/hagezi-normal.rpz";       ZONE="hagezi.rpz";    SRCFILE="$ETC/blocklist-source" ;;
        secondary) DEST="$BL/blocklist-secondary.rpz"; ZONE="secondary.rpz"; SRCFILE="$ETC/blocklist-source-secondary" ;;
        *) echo "unknown slot '$1'; known: primary secondary" >&2; return 1 ;;
    esac
}

# write_empty_zone PATH -> a valid RPZ that blocks nothing (for an "off" slot).
write_empty_zone() {
    {
        echo '$TTL 300'
        echo "@ SOA localhost. root.localhost. $(date +%s) 43200 3600 259200 300"
        echo '  NS  localhost.'
    } > "$1"
}

# install_zone DEST NEW ZONE -> back up DEST, atomically swap NEW into place,
# checkconf (roll back on fail), reload just that zone (full reload as fallback,
# which also covers a zone not yet in the running config on first activation).
# Returns 1 on failure.
install_zone() {
    local dest="$1" new="$2" zone="$3" bak="${1}.bak"
    [ -f "$dest" ] && cp -p "$dest" "$bak"
    chown unbound:unbound "$new" 2>/dev/null || true
    chmod 644 "$new" 2>/dev/null || true
    if ! mv "$new" "$dest"; then echo "mv into $dest failed" >&2; return 1; fi
    if ! "$CHECK" >/dev/null 2>&1; then
        echo "checkconf failed after updating $zone; rolling back" >&2
        [ -f "$bak" ] && mv "$bak" "$dest"
        return 1
    fi
    "$CTRL" auth_zone_reload "$zone" >/dev/null 2>&1 \
        || "$CTRL" reload >/dev/null 2>&1 \
        || { echo "reload of $zone failed" >&2; return 1; }
    return 0
}

# refresh_slot SLOT SOURCE -> full download/validate/swap/reload for one slot.
# SOURCE "off" (secondary only) installs an empty zone. Returns 1 on failure.
refresh_slot() {
    local slot="$1" source="$2"
    resolve_slot "$slot" || return 1
    local new="${DEST}.new"

    if [ "$source" = "off" ]; then
        if [ "$slot" = "primary" ]; then echo "refusing to turn off the primary blocklist" >&2; return 1; fi
        write_empty_zone "$new"
        install_zone "$DEST" "$new" "$ZONE" || { rm -f "$new"; return 1; }
        echo "$slot: off (empty zone)"
        return 0
    fi

    resolve_source "$source" || return 1
    local tmp; tmp="$(mktemp)"
    if ! curl -fsS --max-time 90 "$SRC" -o "$tmp"; then
        echo "$slot: download failed for source '$source'" >&2; rm -f "$tmp"; return 1
    fi
    if ! grep -q 'CNAME' "$tmp" || [ "$(wc -l < "$tmp")" -lt "$MIN_LINES" ]; then
        echo "$slot: source '$source' looks wrong (want >= $MIN_LINES lines); skipping" >&2
        rm -f "$tmp"; return 1
    fi
    cp "$tmp" "$new"; rm -f "$tmp"
    install_zone "$DEST" "$new" "$ZONE" || { rm -f "$new"; return 1; }
    echo "$slot: updated from $source ($(wc -l < "$DEST" | tr -d ' ') lines)"
    return 0
}

# slot_source SLOT -> the source name for a nightly (no-arg) refresh: the slot's
# source-file contents, else a per-slot default (primary must always have a
# list; secondary defaults to off).
slot_source() {
    resolve_slot "$1" || return 1
    if [ -s "$SRCFILE" ]; then
        head -n1 "$SRCFILE" | tr -d '[:space:]'
    elif [ "$1" = "primary" ]; then
        echo "hagezi-normal"
    else
        echo "off"
    fi
}

# ---------------------------------------------------------------- main --------
all_ok=1

if [ -n "${1:-}" ]; then
    # one-time mode: a single slot (default primary) with an explicit source
    refresh_slot "${2:-primary}" "$1" || all_ok=0
else
    # nightly mode: every slot, each from its own source file
    for slot in $SLOTS; do
        src="$(slot_source "$slot")" || { all_ok=0; continue; }
        refresh_slot "$slot" "$src" || all_ok=0
    done
fi

# ------------------------------------------------------------- metrics --------
# One textfile, both slots, recomputed from the on-disk zonefiles so a one-slot
# run never clobbers the other slot's line-count. A single # TYPE per metric
# (two files each declaring TYPE would trip node_exporter's duplicate check).
mkdir -p "$METRICS_DIR" 2>/dev/null || true
if [ -w "$METRICS_DIR" ]; then
    # preserve the prior success timestamp unless THIS run fully succeeded, so a
    # partial failure lets BlocklistUpdateStale go stale instead of resetting.
    prev_ts="$(grep -h 'task="blocklist"' "$PROM" 2>/dev/null | awk '{print $NF}' | head -n1)"
    if [ "$all_ok" -eq 1 ]; then ts="$(date +%s)"; else ts="${prev_ts:-}"; fi

    tmp="$PROM.$$"
    {
        if [ -n "$ts" ]; then
            echo '# TYPE unbound_job_last_success_timestamp_seconds gauge'
            printf 'unbound_job_last_success_timestamp_seconds{task="blocklist"} %s\n' "$ts"
        fi
        echo '# TYPE unbound_blocklist_lines gauge'
        for slot in $SLOTS; do
            resolve_slot "$slot"
            # only emit for a slot holding a real list (has CNAME rules); an
            # "off"/empty secondary is skipped so its ~3 lines can't false-alarm.
            if [ -f "$DEST" ] && grep -q 'CNAME' "$DEST" 2>/dev/null; then
                printf 'unbound_blocklist_lines{slot="%s"} %s\n' "$slot" "$(wc -l < "$DEST" | tr -d ' ')"
            fi
        done
    } > "$tmp" && mv "$tmp" "$PROM"
fi

if [ "$all_ok" -eq 1 ]; then exit 0; else exit 1; fi
