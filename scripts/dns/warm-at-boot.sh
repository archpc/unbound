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

set -euo pipefail
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

LIST="/Users/localadmin/warmup/dns-warm-domains.personal.txt"
[ -r "$LIST" ] || LIST="/usr/local/share/unbound-stack/dns-warm-domains.txt"

echo "boot warmup starting $(date '+%Y-%m-%d %H:%M:%S') with $LIST"
status=0
"$script_dir/warm-cache.zsh" "$LIST" || status=$?

# Success breadcrumb for the monitoring stack (see update-blocklist.sh for
# the pattern). The matching alert compares this against node_boot_time_
# seconds: breadcrumb older than the current boot = warmboot didn't run.
if [ "$status" -eq 0 ]; then
    METRICS_DIR="/opt/homebrew/var/lib/node_exporter/textfile"
    mkdir -p "$METRICS_DIR" 2>/dev/null || true
    if [ -w "$METRICS_DIR" ]; then
        tmp="$METRICS_DIR/unbound-warmboot.prom.$$"
        trap 'rm -f "$tmp"' EXIT
        {
            echo '# TYPE unbound_job_last_success_timestamp_seconds gauge'
            printf 'unbound_job_last_success_timestamp_seconds{task="warmboot"} %s\n' "$(date +%s)"
        } > "$tmp" && mv "$tmp" "$METRICS_DIR/unbound-warmboot.prom"
    fi
fi
exit "$status"
