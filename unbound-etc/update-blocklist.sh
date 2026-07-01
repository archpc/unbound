#!/usr/bin/env bash
set -euo pipefail

# HaGeZi "Multi NORMAL" list (upstream file rpz/multi.txt; the Pro tier was rpz/pro.txt)
SRC="https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/rpz/multi.txt"
DEST="/opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz"
TMP="$(mktemp)"
NEW="${DEST}.new"   # staged on the same filesystem as DEST so mv(1) is atomic
BAK="${DEST}.bak"
CTRL="/opt/homebrew/sbin/unbound-control"
CHECK="/opt/homebrew/sbin/unbound-checkconf"
trap 'rm -f "$TMP" "$NEW"' EXIT

curl -fsS --max-time 90 "$SRC" -o "$TMP"

if ! grep -q 'CNAME' "$TMP" || [ "$(wc -l < "$TMP")" -lt 100000 ]; then
    echo "downloaded file looks wrong; aborting" >&2
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
