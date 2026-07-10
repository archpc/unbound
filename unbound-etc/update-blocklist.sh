#!/usr/bin/env bash
#
# update-blocklist.sh — download one RPZ blocklist, validate it, atomically
# replace the live zonefile, and reload only the hagezi.rpz zone.
#
# The upstream source is pluggable, but the runtime zone and file remain stable:
#   zone:      hagezi.rpz
#   zonefile:  /opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz
#   selector:  /opt/homebrew/etc/unbound/blocklist-source
#
# Run by:  local.unbound.blocklist daily at 03:30, as root, with no arguments.
# Manual:  sudo update-blocklist.sh [source]
#          An explicit source is a one-time override. It does not rewrite the
#          selector, so the next scheduled run returns to the configured source.
# Sources: hagezi-normal hagezi-pro 1hosts-lite 1hosts-xtra oisd-small oisd-big
# Exit:    0 = updated, validated, reloaded, and metrics written.
#          1 = invalid source, download/validation failure, or reload failure;
#              the prior zone and success breadcrumb remain in place where possible.
# Verify:  dig @127.0.0.1 google-analytics.com -> NXDOMAIN with aa
#          curl -s localhost:9100/metrics | grep unbound_blocklist_lines
set -euo pipefail

ETC="/opt/homebrew/etc/unbound"
BL="$ETC/blocklists"
DEST="$BL/hagezi-normal.rpz"
SOURCE_FILE="$ETC/blocklist-source"
CTRL="/opt/homebrew/sbin/unbound-control"
CHECK="/opt/homebrew/sbin/unbound-checkconf"
METRICS_DIR="/opt/homebrew/var/lib/node_exporter/textfile"
PROM="$METRICS_DIR/unbound-blocklist.prom"

resolve_source() {
    case "$1" in
        hagezi-normal) SRC="https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/rpz/multi.txt"; MIN_LINES=100000 ;;
        hagezi-pro)    SRC="https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/rpz/pro.txt";   MIN_LINES=300000 ;;
        1hosts-lite)   SRC="https://raw.githubusercontent.com/badmojr/1Hosts/master/Lite/rpz.txt";   MIN_LINES=130000 ;;
        1hosts-xtra)   SRC="https://raw.githubusercontent.com/badmojr/1Hosts/master/Xtra/rpz.txt";   MIN_LINES=900000 ;;
        oisd-small)    SRC="https://small.oisd.nl/rpz"; MIN_LINES=70000 ;;
        oisd-big)      SRC="https://big.oisd.nl/rpz";   MIN_LINES=600000 ;;
        *)
            echo "unknown source '$1'; known: hagezi-normal hagezi-pro 1hosts-lite 1hosts-xtra oisd-small oisd-big" >&2
            return 1
            ;;
    esac
}

configured_source() {
    if [ -s "$SOURCE_FILE" ]; then
        head -n1 "$SOURCE_FILE" | tr -d '[:space:]'
    else
        echo "hagezi-normal"
    fi
}

if [ "$#" -gt 1 ]; then
    echo "usage: $0 [source]" >&2
    exit 1
fi

SOURCE="${1:-$(configured_source)}"
resolve_source "$SOURCE"

TMP="$(mktemp)"
NEW="${DEST}.new"
BAK="${DEST}.bak"
trap 'rm -f "$TMP" "$NEW"' EXIT

curl -fsS --max-time 90 "$SRC" -o "$TMP"

if ! grep -q 'CNAME' "$TMP" || [ "$(wc -l < "$TMP")" -lt "$MIN_LINES" ]; then
    echo "downloaded file looks wrong for source '$SOURCE' (want >= $MIN_LINES lines); aborting" >&2
    exit 1
fi

if [ -f "$DEST" ]; then
    cp -p "$DEST" "$BAK"
fi
cp "$TMP" "$NEW"
chown unbound:unbound "$NEW"
chmod 644 "$NEW"
mv "$NEW" "$DEST"

if ! "$CHECK" >/dev/null 2>&1; then
    echo "checkconf failed; rolling back" >&2
    if [ -f "$BAK" ]; then
        mv "$BAK" "$DEST"
    else
        echo "no backup present; $DEST left as downloaded" >&2
    fi
    exit 1
fi

"$CTRL" auth_zone_reload hagezi.rpz || "$CTRL" reload
echo "updated from $SOURCE: $(wc -l < "$DEST" | tr -d ' ') lines"

mkdir -p "$METRICS_DIR" 2>/dev/null || true
if [ -w "$METRICS_DIR" ]; then
    tmp="$PROM.$$"
    {
        echo '# TYPE unbound_job_last_success_timestamp_seconds gauge'
        printf 'unbound_job_last_success_timestamp_seconds{task="blocklist"} %s\n' "$(date +%s)"
        echo '# TYPE unbound_blocklist_lines gauge'
        printf 'unbound_blocklist_lines %s\n' "$(wc -l < "$DEST" | tr -d ' ')"
    } > "$tmp" && mv "$tmp" "$PROM"
fi
