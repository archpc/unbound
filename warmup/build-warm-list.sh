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
#   Defaults: dns-warm-domains.personal.txt next to this script, top 250
#   cache-derived domains; the curated dns-warm-domains.txt (if present next
#   to the script) is merged in after them, deduplicated, so the output stays
#   strong even right after a reboot empties the cache.
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
nshosts="$(mktemp)"; cnhosts="$(mktemp)"; exclude="$(mktemp)"
trap 'rm -f "$blocked" "$candidates" "$nshosts" "$cnhosts" "$exclude"' EXIT

# Blocked set: registrable domains HaGeZi blocks wholesale (so we don't warm a
# domain the RPZ would just nxdomain). Subdomain-only blocks don't drop the
# parent, which is correct — the parent still resolves.
if [ -r "$RPZ" ]; then
  awk '$2 == "CNAME" { s = $1; sub(/^\*\./, "", s); sub(/\.$/, "", s); print tolower(s) }' "$RPZ" \
    | collapse | sort -u > "$blocked"
fi

# Grab the cache once, surfacing the REAL error if the control call fails
# (usual cause: run without sudo — the control socket is root-owned). Don't
# swallow it into a misleading "empty cache" message.
dump="$(unbound-control dump_cache 2>&1)"; status=$?
if [ "$status" -ne 0 ]; then
  printf 'unbound-control dump_cache failed (exit %d):\n%s\n' "$status" "$dump" >&2
  case "$dump" in *[Pp]ermission*) echo '-> run this script with sudo (control socket is root-owned).' >&2 ;; esac
  exit 1
fi

# Resolver-internal noise filter: the msg cache also holds unbound's OWN
# lookups, not just client questions. Three structural classes, all
# identifiable from the same dump:
#   E1  NS-record targets   — nameserver hosts unbound resolved while chasing
#                             delegations (ns-*.awsdns-NN.org, a1-64.akam.net)
#   E2  CNAME-record targets — chain hosts resolved while following aliases
#                             (e####.dsca.akamaiedge.net and friends)
#   E3  apexes of E1 domains — qname-minimisation probes: walking down to
#                             ns-80.awsdns-10.com caches a bare
#                             "awsdns-10.com A" lookup no client ever made
# Exclusions remove individual cache VOTES, not final domains: google.com
# survives losing its ns1.google.com vote via real subdomain traffic, while
# pure infrastructure loses its only votes and drops out. Clients never ask
# for any of these names, and warming a client-facing name re-resolves its
# delegation/alias chain anyway — so they are redundant by definition.
printf '%s\n' "$dump" \
  | awk '$3 == "IN" && $4 == "NS"    { t = tolower($5); sub(/\.$/, "", t); print t }' \
  | sort -u > "$nshosts"
printf '%s\n' "$dump" \
  | awk '$3 == "IN" && $4 == "CNAME" { t = tolower($5); sub(/\.$/, "", t); print t }' \
  | sort -u > "$cnhosts"
{ cat "$nshosts" "$cnhosts"; collapse < "$nshosts"; } | sort -u > "$exclude"

# Tail-sweep for what the structural filters CANNOT catch: msg entries
# outlive the rrset evidence (msg TTLs run to ~21h; NS rrsets expire in
# minutes), so stale infrastructure lookups have no NS/CNAME record left to
# prove what they are. Bounded list of pure managed-DNS/CDN-internal domains —
# names no client ever queries directly. (Deliberately NOT listed: things
# clients DO query, e.g. cloudfront.net, akamaized.net, cloudflare-dns.com.)
NSPROVIDERS='^awsdns-[0-9]+\.|^(nsone|twtrdns|dynect|akam|akadns|akamai|akamaiedge|edgekey|edgesuite|aka-ns|gtld-servers|root-servers)\.(com|net|org)$|^(azure-dns|ultradns)\.|^(akagtm|aka-ns)\.org$|^(googledomains|domaincontrol)\.com$'

# Candidates: query names from the MESSAGE cache (msg lines = actual lookups,
# not incidental glue rrsets). Keep A/AAAA/HTTPS **that returned answers** —
# field 9 of a msg line is the answer count, and 0 means nodata/negative:
# either a qname-minimisation probe or a name not worth warming anyway.
# Drop local/reverse names and the exclusion set, collapse to registrable
# domains, sweep NS-provider patterns, order by frequency.
printf '%s\n' "$dump" \
  | awk '/^msg /{ if (($4=="A"||$4=="AAAA"||$4=="HTTPS") && ($9+0) > 0) { name=$2; sub(/\.$/,"",name); print tolower(name) } }' \
  | grep -vE '(\.lan$|\.arpa$|^$)' \
  | { if [ -s "$exclude" ]; then grep -Fxvf "$exclude"; else cat; fi } \
  | collapse \
  | grep -vE "$NSPROVIDERS" \
  | sort | uniq -c | sort -rn | awk '{ print $2 }' > "$candidates"

if [ ! -s "$candidates" ]; then
  echo "cache dumped OK but held no A/AAAA/HTTPS query names to warm." >&2
  exit 1
fi

# Drop blocked domains (grep -v tolerates the 'no matches' exit code), cap to MAX.
if [ -s "$blocked" ]; then
  grep -Fxvf "$blocked" "$candidates" > "$candidates.f" || true
  mv "$candidates.f" "$candidates"
fi
head -n "$MAX" "$candidates" > "$candidates.top"

# Merge in the curated popular list shipped next to this script (if present):
# household traffic ranks first, then popular domains not already covered.
# This keeps warmups strong right after a reboot, when the cache — and thus
# the personal signal — is thin or empty.
GENERIC="$script_dir/dns-warm-domains.txt"
{
  cat "$candidates.top"
  if [ -r "$GENERIC" ]; then
    tr -d '\r' < "$GENERIC" | grep -vE '^[[:space:]]*(#|$)'
  fi
} | awk '!seen[$0]++' > "$candidates.merged"

personal="$(wc -l < "$candidates.top" | tr -d ' ')"
count="$(wc -l < "$candidates.merged" | tr -d ' ')"
{
  echo "# Personalized DNS warmup list — generated $(date '+%Y-%m-%d %H:%M')"
  echo "# $personal household domains from the unbound message cache (by cache"
  echo "# activity; RPZ-blocked and resolver-internal names removed), then"
  echo "# $((count - personal)) curated popular domains not already covered."
  echo "# Regenerate with build-warm-list.sh."
  echo "# PRIVACY: household profile — keep local, do NOT commit."
  cat "$candidates.merged"
} > "$OUT"
rm -f "$candidates.top" "$candidates.merged"

echo "wrote $count domains to $OUT ($personal personal + $((count - personal)) popular)"
echo "use it:  sudo $script_dir/warm-unbound-cache.sh \"$OUT\""

# Success breadcrumb for the monitoring stack (see update-blocklist.sh for
# the pattern; prometheus.rules.yml alarms when the timestamp goes stale).
METRICS_DIR="/opt/homebrew/var/lib/node_exporter/textfile"
mkdir -p "$METRICS_DIR" 2>/dev/null || true
if [ -w "$METRICS_DIR" ]; then
    {
        echo '# TYPE unbound_job_last_success_timestamp_seconds gauge'
        printf 'unbound_job_last_success_timestamp_seconds{task="warmlist"} %s\n' "$(date +%s)"
        echo '# TYPE unbound_warmlist_domains gauge'
        printf 'unbound_warmlist_domains{kind="personal"} %s\n' "$personal"
        printf 'unbound_warmlist_domains{kind="popular"} %s\n' "$((count - personal))"
    } > "$METRICS_DIR/unbound-warmlist.prom.$$" \
      && mv "$METRICS_DIR/unbound-warmlist.prom.$$" "$METRICS_DIR/unbound-warmlist.prom"
fi
