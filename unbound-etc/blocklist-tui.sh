#!/usr/bin/env bash
#
# blocklist-tui.sh — interactive slot manager for the two-slot RPZ blocklist
# system (see update-blocklist.sh). Deliberately does none of the actual
# work itself: it's read-only for state, and every real change (download,
# validate, atomic swap, reload, metrics) is delegated to update-blocklist.sh
# so there's exactly one place that knows how to do that safely.
#
# Run:   sudo /opt/homebrew/etc/unbound/blocklist-tui.sh
# Needs: root — reads/writes the source-of-truth files under
#        /opt/homebrew/etc/unbound and shells out to update-blocklist.sh,
#        which needs root itself (unbound-owned zonefiles, control socket).
# Bash:  targets /bin/bash 3.2 (macOS system bash) — no arrays, no [[ ]],
#        same constraint as update-blocklist.sh.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ETC="/opt/homebrew/etc/unbound"
BL="$ETC/blocklists"
UPDATE_SCRIPT="$SELF_DIR/update-blocklist.sh"
PROM="/opt/homebrew/var/lib/node_exporter/textfile/unbound-blocklist.prom"

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root: sudo $0" >&2
    exit 1
fi
if [ ! -f "$UPDATE_SCRIPT" ]; then
    echo "can't find update-blocklist.sh next to this script at $UPDATE_SCRIPT" >&2
    exit 1
fi

# Known sources — kept in sync BY HAND with resolve_source() in
# update-blocklist.sh. Approx line counts are the same 2026-07-09 samples
# recorded there.
SOURCE_LIST="hagezi-normal hagezi-pro 1hosts-lite 1hosts-xtra oisd-small oisd-big"
source_label() {
    case "$1" in
        hagezi-normal) echo "HaGeZi Multi NORMAL   ~316k   balanced default" ;;
        hagezi-pro)    echo "HaGeZi Multi PRO      ~464k   more aggressive" ;;
        1hosts-lite)   echo "1Hosts Lite           ~199k   light, low false-positive" ;;
        1hosts-xtra)   echo "1Hosts Xtra          ~1423k   everything 1Hosts has" ;;
        oisd-small)    echo "OISD Small            ~111k   light" ;;
        oisd-big)      echo "OISD Big              ~987k   broad coverage" ;;
        *) echo "" ;;
    esac
}

slot_srcfile()  { case "$1" in primary) echo "$ETC/blocklist-source" ;; secondary) echo "$ETC/blocklist-source-secondary" ;; esac; }
slot_zonefile() { case "$1" in primary) echo "$BL/hagezi-normal.rpz" ;; secondary) echo "$BL/blocklist-secondary.rpz" ;; esac; }

# current_source SLOT -> mirrors slot_source() in update-blocklist.sh, read-only.
current_source() {
    local f; f="$(slot_srcfile "$1")"
    if [ -s "$f" ]; then head -n1 "$f" | tr -d '[:space:]'
    elif [ "$1" = "primary" ]; then echo "hagezi-normal"
    else echo "off"
    fi
}

line_count() {
    local zf; zf="$(slot_zonefile "$1")"
    if [ -f "$zf" ] && grep -q 'CNAME' "$zf" 2>/dev/null; then wc -l < "$zf" | tr -d ' '
    else echo 0
    fi
}

breadcrumb_age() {
    local ts now
    ts="$(grep -h 'task="blocklist"' "$PROM" 2>/dev/null | awk '{print $NF}' | head -n1)"
    if [ -z "${ts:-}" ]; then echo "never"; return; fi
    now="$(date +%s)"
    echo "$(( (now - ts) / 60 ))m ago ($(date -r "$ts" '+%Y-%m-%d %H:%M'))"
}

confirm() {
    local reply
    read -r -p "$1 [y/N] " reply
    case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

draw_state() {
    echo
    echo "=== Unbound Blocklist Slots ==="
    printf '  %-10s %-16s %s\n' "SLOT" "SOURCE" "LINES"
    for slot in primary secondary; do
        printf '  %-10s %-16s %s\n' "$slot" "$(current_source "$slot")" "$(line_count "$slot")"
    done
    echo "  last full success: $(breadcrumb_age)"
    echo
}

change_source() {
    local slot="$1" i=1 name choice n picked persist
    echo
    echo "Sources for $slot:"
    for name in $SOURCE_LIST; do
        printf '  [%d] %-14s %s\n' "$i" "$name" "$(source_label "$name")"
        i=$((i+1))
    done
    if [ "$slot" = "secondary" ]; then
        printf '  [%d] %-14s %s\n' "$i" "off" "disable this slot"
    fi
    echo "  [0] cancel"
    read -r -p "choice: " choice
    [ "$choice" = "0" ] && return

    n=1; picked=""
    for name in $SOURCE_LIST; do
        [ "$choice" = "$n" ] && picked="$name"
        n=$((n+1))
    done
    if [ "$slot" = "secondary" ] && [ "$choice" = "$n" ]; then picked="off"; fi
    if [ -z "$picked" ]; then echo "invalid choice"; return; fi

    persist=0
    if confirm "make '$picked' the permanent source for $slot (survives the 03:30 nightly run)?"; then
        persist=1
    fi

    echo "will run: $UPDATE_SCRIPT $picked $slot"
    confirm "proceed?" || { echo "cancelled"; return; }

    if [ "$persist" -eq 1 ]; then
        echo "$picked" > "$(slot_srcfile "$slot")"
    fi
    bash "$UPDATE_SCRIPT" "$picked" "$slot"
    echo "(exit $?)"
}

refresh_all() {
    echo "will run: $UPDATE_SCRIPT   (nightly-equivalent: every slot from its own source file)"
    confirm "proceed?" || { echo "cancelled"; return; }
    bash "$UPDATE_SCRIPT"
    echo "(exit $?)"
}

show_details() {
    echo
    echo "Known sources (name / approx lines / note):"
    for name in $SOURCE_LIST; do
        printf '  %-14s %s\n' "$name" "$(source_label "$name")"
    done
    echo "  off             secondary only — writes a valid empty RPZ zone"
    echo
    echo "A pick you don't make permanent applies once; the 03:30 nightly run"
    echo "reverts to whatever that slot's source file says. Primary can't be"
    echo "turned off (update-blocklist.sh refuses it)."
}

while true; do
    draw_state
    cat <<'MENU'
  [1] change primary source
  [2] change secondary source
  [3] refresh all slots now (from current source files)
  [4] show source details
  [q] quit
MENU
    read -r -p "> " opt
    case "$opt" in
        1) change_source primary ;;
        2) change_source secondary ;;
        3) refresh_all ;;
        4) show_details ;;
        q|Q) exit 0 ;;
        *) echo "unknown option" ;;
    esac
done
