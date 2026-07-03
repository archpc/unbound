#!/bin/bash
#
# build-warm-list.sh — derive a PERSONALIZED cache-warmup list from the
# resolver's own message cache (the domains this network actually resolves),
# ordered by cache activity, with RPZ-blocked names removed.
#
# Why the cache instead of query logs: `unbound-control dump_cache` is already
# a frequency-biased snapshot — LRU eviction + prefetch keep popular names hot
# — so it's a good zero-config, stays-on-the-box proxy for "what's worth
# pre-warming". It gives PRESENCE, not true query counts. For real frequency
# weights, temporarily set `log-queries: yes`, collect for a few days, and mine
# that log instead (then turn it back off).
#
# Usage:  sudo ./build-warm-list.sh [output-file] [max-domains]
#   Defaults: dns-warm-domains.personal.txt next to this script, top 250.
#   Needs sudo: dump_cache talks to the root-owned control socket.
#
# PRIVACY: the output is a household behaviour profile — far more revealing
# than the generic Tranco list. It is gitignored on purpose. Keep it local;
# do not commit it to a repo that travels.

set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$script_dir/dns-warm-domains.personal.txt}"
MAX="${2:-250}"
RPZ="/opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz"

command -v unbound-control >/dev/null 2>&1 || { echo "unbound-control not in PATH" >&2; exit 1; }

# A short list of multi-label public suffixes so we collapse to the REGISTRABLE
# domain (foo.co.uk, not co.uk). Not the full PSL — just ones a home network
# hits; an unlisted suffix just yields a slightly broader name, which warms fine.
MULTI='co.uk|com.au|co.jp|co.nz|co.za|com.br|co.in|com.mx|co.kr|com.tr|com.sg|com.hk|org.uk|net.au'

# collapse: stdin = one lowercase host per line (no trailing dot) -> registrable domain
collapse() {
  awk -v ms="$MULTI" '
    { n = split($0, l, "."); if (n < 2) next;
      d = l[n-1] "." l[n];
      if (d ~ ("^(" ms ")$") && n >= 3) d = l[n-2] "." l[n-1] "." l[n];
      print d }'
}

blocked="$(mktemp)"; candidates="$(mktemp)"
trap 'rm -f "$blocked" "$candidates"' EXIT

# Blocked set: registrable domains HaGeZi blocks wholesale (so we don't warm a
# domain the RPZ would just nxdomain). Subdomain-only blocks don't drop the
# parent, which is correct — the parent still resolves.
if [ -r "$RPZ" ]; then
  awk '$2 == "CNAME" { s = $1; sub(/^\*\./, "", s); sub(/\.$/, "", s); print tolower(s) }' "$RPZ" \
    | collapse | sort -u > "$blocked"
fi

# Candidates: query names from the MESSAGE cache (msg lines = actual lookups,
# not incidental glue rrsets). Keep A/AAAA/HTTPS, collapse, order by frequency.
unbound-control dump_cache 2>/dev/null \
  | awk '/^msg /{ t = $4; if (t=="A"||t=="AAAA"||t=="HTTPS") { name=$2; sub(/\.$/,"",name); print tolower(name) } }' \
  | grep -vE '(\.lan$|\.arpa$|^$)' \
  | collapse \
  | sort | uniq -c | sort -rn | awk '{ print $2 }' > "$candidates"

if [ ! -s "$candidates" ]; then
  echo "no usable domains in the cache — is the resolver actually serving traffic?" >&2
  exit 1
fi

# Drop blocked domains (grep -v tolerates the 'no matches' exit code), cap to MAX.
if [ -s "$blocked" ]; then
  grep -Fxvf "$blocked" "$candidates" > "$candidates.f" || true
  mv "$candidates.f" "$candidates"
fi
head -n "$MAX" "$candidates" > "$candidates.top"

count="$(wc -l < "$candidates.top" | tr -d ' ')"
{
  echo "# Personalized DNS warmup list — generated $(date '+%Y-%m-%d %H:%M')"
  echo "# Source: unbound message cache (top $count registrable domains by cache"
  echo "# activity; RPZ-blocked names removed). Regenerate with build-warm-list.sh."
  echo "# PRIVACY: household profile — keep local, do NOT commit."
  cat "$candidates.top"
} > "$OUT"
rm -f "$candidates.top"

echo "wrote $count domains to $OUT"
echo "use it:  sudo $script_dir/warm-unbound-cache.sh \"$OUT\""
