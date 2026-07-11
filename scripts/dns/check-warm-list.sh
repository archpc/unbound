#!/bin/bash
#
# check-warm-list.sh — verify that none of the domains in a warmup list are
# blocked by the live RPZ. Asks the resolver itself, so the personal allowlist
# (hagezi.allow passthru), the blocklist, and wildcard rules are all applied
# exactly — this is ground truth, unlike grepping the zone file.
#
# Detection: on this resolver an RPZ/local *synthesized* answer carries the
# 'aa' (authoritative) flag, while a genuine recursive answer — NOERROR or a
# real NXDOMAIN — does not. So an external name returning NXDOMAIN **with aa**
# is RPZ-blocked (the blocklist forces rpz-action-override: nxdomain). A real
# non-existent domain returns NXDOMAIN without aa and is not flagged.
#
# Usage:
#   ./check-warm-list.sh [domain-file]           # report blocked domains
#   ./check-warm-list.sh --prune [domain-file]   # ...and rewrite without them
#   Default file: /Users/localadmin/warmup/dns-warm-domains.personal.txt.
#   DNS_SERVER env overrides the resolver (default 127.0.0.1).

set -euo pipefail

prune=0
if [ "${1:-}" = "--prune" ]; then prune=1; shift; fi

FILE="${1:-/Users/localadmin/warmup/dns-warm-domains.personal.txt}"
server="${DNS_SERVER:-127.0.0.1}"

[ -r "$FILE" ] || { echo "domain file not found: $FILE" >&2; exit 1; }
command -v dig >/dev/null 2>&1 || { echo "dig not found in PATH" >&2; exit 1; }

blocked="$(mktemp)"; trap 'rm -f "$blocked"' EXIT
n=0

while IFS= read -r d; do
  case "$d" in ''|\#*) continue ;; esac
  n=$((n + 1))
  out="$(dig @"$server" +tries=1 +time=2 "$d" A 2>/dev/null)"
  if grep -qE 'flags:[^;]* aa' <<<"$out" && grep -q 'status: NXDOMAIN' <<<"$out"; then
    echo "BLOCKED  $d"
    printf '%s\n' "$d" >> "$blocked"
  fi
done < <(tr -d '\r' < "$FILE")

b=$(wc -l < "$blocked" | tr -d ' ')
echo "checked $n domains: $b blocked by RPZ" >&2

if [ "$b" -gt 0 ] && [ "$prune" -eq 1 ]; then
  tmp="${FILE}.tmp.$$"
  trap 'rm -f "$blocked" "$tmp"' EXIT
  grep -Fxvf "$blocked" "$FILE" > "$tmp"
  owner="$(/usr/bin/stat -f '%u:%g' "$FILE")"
  mode="$(/usr/bin/stat -f '%Lp' "$FILE")"
  /usr/sbin/chown "$owner" "$tmp"
  /bin/chmod "$mode" "$tmp"
  mv "$tmp" "$FILE"
  echo "pruned $b blocked domain(s) from $FILE" >&2
fi
