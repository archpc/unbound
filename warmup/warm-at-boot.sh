#!/bin/bash
#
# warm-at-boot.sh — run once per boot by the local.unbound.warmboot
# LaunchDaemon: wait for unbound to come up, then warm the cache.
#
# The wait loop matters: on this box unbound's first launch attempt reliably
# dies in an opendirectoryd race and KeepAlive retries ~10s later (documented
# in CLAUDE.md), and this job starts in parallel with all of that.
#
# Uses the personal (cache-mined + populars) list when one exists, falling
# back to the curated default shipped next to the scripts.

set -u
export PATH="/opt/homebrew/sbin:/opt/homebrew/bin:$PATH"
script_dir="$(cd "$(dirname "$0")" && pwd)"

for _ in $(seq 1 30); do
  unbound-control status >/dev/null 2>&1 && break
  sleep 3
done
if ! unbound-control status >/dev/null 2>&1; then
  echo "unbound not answering after ~90s; skipping boot warmup" >&2
  exit 1
fi

LIST="$script_dir/dns-warm-domains.personal.txt"
[ -r "$LIST" ] || LIST="$script_dir/dns-warm-domains.txt"

echo "boot warmup starting $(date '+%Y-%m-%d %H:%M:%S') with $LIST"
exec "$script_dir/warm-unbound-cache.sh" "$LIST"
