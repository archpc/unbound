#!/usr/bin/env bash
#
# manage-blocklist.sh — interactive source manager for the generated RPZ.
# Read-only state display lives here; downloads, validation, replacement, reload,
# and metric writes are delegated to update-blocklist.sh.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ETC="/opt/homebrew/etc/unbound"
ZONEFILE="$ETC/blocklists/hagezi-normal.rpz"
SOURCE_FILE="$ETC/blocklist-source"
UPDATE_SCRIPT="/usr/local/libexec/unbound-stack/dns/update-blocklist.sh"
PROM="/opt/homebrew/var/lib/node_exporter/textfile/unbound-blocklist.prom"
SOURCE_LIST="hagezi-normal hagezi-pro 1hosts-lite 1hosts-xtra oisd-small oisd-big"

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root: sudo $0" >&2
    exit 1
fi
if [ ! -f "$UPDATE_SCRIPT" ]; then
    echo "can't find blocklist updater at $UPDATE_SCRIPT" >&2
    exit 1
fi

source_label() {
    case "$1" in
        hagezi-normal) echo "HaGeZi Multi NORMAL   ~316k   balanced" ;;
        hagezi-pro)    echo "HaGeZi Multi PRO      ~464k   more aggressive" ;;
        1hosts-lite)   echo "1Hosts Lite           ~199k   light, low false-positive" ;;
        1hosts-xtra)   echo "1Hosts Xtra          ~1423k   broad" ;;
        oisd-small)    echo "OISD Small            ~111k   light" ;;
        oisd-big)      echo "OISD Big              ~987k   broad" ;;
        *) echo "" ;;
    esac
}

current_source() {
    if [ -s "$SOURCE_FILE" ]; then
        head -n1 "$SOURCE_FILE" | tr -d '[:space:]'
    else
        echo "hagezi-normal"
    fi
}

line_count() {
    if [ -f "$ZONEFILE" ] && grep -q 'CNAME' "$ZONEFILE" 2>/dev/null; then
        wc -l < "$ZONEFILE" | tr -d ' '
    else
        echo 0
    fi
}

breadcrumb_age() {
    local ts now
    ts="$(grep -h 'task="blocklist"' "$PROM" 2>/dev/null | awk '{print $NF}' | head -n1)"
    if [ -z "${ts:-}" ]; then
        echo "never"
        return
    fi
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
    echo "=== Unbound Blocklist ==="
    printf '  source:       %s\n' "$(current_source)"
    printf '  lines:        %s\n' "$(line_count)"
    printf '  last success: %s\n' "$(breadcrumb_age)"
    echo
}

change_source() {
    local i=1 name choice n=1 picked="" persist=0
    echo
    echo "Sources:"
    for name in $SOURCE_LIST; do
        printf '  [%d] %-14s %s\n' "$i" "$name" "$(source_label "$name")"
        i=$((i + 1))
    done
    echo "  [0] cancel"
    read -r -p "choice: " choice
    [ "$choice" = "0" ] && return

    for name in $SOURCE_LIST; do
        [ "$choice" = "$n" ] && picked="$name"
        n=$((n + 1))
    done
    if [ -z "$picked" ]; then
        echo "invalid choice"
        return
    fi

    if confirm "make '$picked' the permanent nightly source?"; then
        persist=1
    fi
    echo "will run: $UPDATE_SCRIPT $picked"
    confirm "proceed?" || { echo "cancelled"; return; }

    if bash "$UPDATE_SCRIPT" "$picked"; then
        if [ "$persist" -eq 1 ]; then
            tmp="${SOURCE_FILE}.tmp.$$"
            trap 'rm -f "$tmp"' RETURN
            printf '%s\n' "$picked" > "$tmp"
            chmod 0644 "$tmp"
            chown root:wheel "$tmp"
            mv "$tmp" "$SOURCE_FILE"
            echo "permanent source updated: $picked"
        fi
    else
        echo "update failed; permanent source unchanged" >&2
    fi
}

refresh_current() {
    echo "will run: $UPDATE_SCRIPT   (refresh $(current_source))"
    confirm "proceed?" || { echo "cancelled"; return; }
    bash "$UPDATE_SCRIPT"
    echo "(exit $?)"
}

show_details() {
    local name
    echo
    for name in $SOURCE_LIST; do
        printf '  %-14s %s\n' "$name" "$(source_label "$name")"
    done
    echo
    echo "A non-permanent choice applies once. The 03:30 run uses $SOURCE_FILE."
}

while true; do
    draw_state
    echo "  [1] change blocklist source"
    echo "  [2] refresh current source now"
    echo "  [3] show source details"
    echo "  [q] quit"
    read -r -p "> " opt
    case "$opt" in
        1) change_source ;;
        2) refresh_current ;;
        3) show_details ;;
        q|Q) exit 0 ;;
        *) echo "unknown option" ;;
    esac
done
