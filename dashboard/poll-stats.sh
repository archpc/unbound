#!/bin/bash
#
# poll-stats.sh — sample unbound's live counters into an append-only JSONL
# file for the dashboard. Generic passthrough: every key=value line from
# `unbound-control stats_noreset` becomes a JSON field, so this doesn't need
# to know or hardcode which counters this unbound version reports (e.g.
# whether extended-statistics exposes a per-action RPZ counter) — whatever
# comes out flows straight into the file. stats_noreset does not reset the
# counters, so values are cumulative since the last unbound restart; the
# dashboard computes rates from deltas between samples.
#
# Run every 5 min by local.unbound.dashboardpoll, as root (needs the
# root-owned control socket). Self-prunes to the last RETENTION_DAYS so the
# file never grows unbounded.

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$script_dir/data"
OUT="$DATA_DIR/stats.jsonl"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
CTRL="/opt/homebrew/sbin/unbound-control"

mkdir -p "$DATA_DIR"

ts="$(date +%s)"
stats="$("$CTRL" stats_noreset)"

{
  printf '{"ts":%s' "$ts"
  printf '%s\n' "$stats" | awk -F= '
    NF==2 {
      key=$1; val=$2
      gsub(/"/, "\\\"", key)
      if (val ~ /^-?[0-9]+(\.[0-9]+)?$/) {
        printf ",\"%s\":%s", key, val
      } else {
        gsub(/"/, "\\\"", val)
        printf ",\"%s\":\"%s\"", key, val
      }
    }'
  printf '}\n'
} >> "$OUT"

# Prune anything older than the retention window (embedded ts field, no
# external timestamp needed).
cutoff=$(( ts - RETENTION_DAYS * 86400 ))
tmp="$(mktemp "$DATA_DIR/.stats.XXXXXX")"
awk -F'"ts":' -v c="$cutoff" '{split($2, a, ","); if ((a[1] + 0) >= c) print}' "$OUT" > "$tmp"
mv "$tmp" "$OUT"
chmod 644 "$OUT"
