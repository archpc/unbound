#!/bin/zsh
#
# warm-unbound-cache.sh — pre-populate ("warm") a local Unbound resolver's
# cache by querying a list of domains, then report the cache hit-rate
# delta the warmup produced.
#
# Usage:
#   sudo ./warm-unbound-cache.sh [domain-file]
#
#   sudo is required: unbound-control connects to the root-owned control
#   socket (/opt/homebrew/var/run/unbound-control.sock). Without it the
#   script dies at the first stats snapshot, before sending any queries.
#
#   domain-file   One domain per line. Blank lines and lines starting with
#                 '#' are ignored. Defaults to dns-warm-domains.txt next to
#                 this script, falling back to $HOME/dns-warm-domains.txt.
#
# Env vars:
#   DNS_SERVER    Resolver to query. Defaults to 127.0.0.1.

DNS_SERVER="${DNS_SERVER:-127.0.0.1}"

# Prefer the domain list shipped alongside this script; keep the old $HOME
# location as a fallback so existing setups don't break.
script_dir="${0:A:h}"
if [[ -n "${1:-}" ]]; then
  DOMAIN_FILE="$1"
elif [[ -f "$script_dir/dns-warm-domains.txt" ]]; then
  DOMAIN_FILE="$script_dir/dns-warm-domains.txt"
else
  DOMAIN_FILE="$HOME/dns-warm-domains.txt"
fi

# Per-query timeout/retry for dig. Without this, an unreachable resolver
# makes every single query wait out dig's default ~15s (5s timeout x 3
# tries) — across a couple thousand queries in one batch, that's hours.
DIG_OPTS=(+time=2 +tries=1)

# Query types requested for every domain (some domains won't have all of
# these records — that's fine, dig just gets an empty answer for those).
RECORD_TYPES=(A AAAA HTTPS)

# ---- output helpers ---------------------------------------------------
# Color section banners when writing to a terminal; stay plain otherwise
# (piped output, NO_COLOR set) so logs/redirected output aren't full of
# escape codes.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  bold=$'\e[1m'; cyan=$'\e[36m'; reset=$'\e[0m'
else
  bold=''; cyan=''; reset=''
fi

section() {
  printf '\n%s%s==> %s%s\n' "$bold" "$cyan" "$1" "$reset"
}

die() {
  echo "error: $1" >&2
  exit 1
}

# ---- stat helpers -------------------------------------------------------

# get_stat <unbound-control-output> <key>  -> numeric value, or 0 if absent.
# Exact key match (not a substring/regex match) so it can't be tricked by
# unrelated lines that happen to contain the key as a substring.
get_stat() {
  awk -F= -v key="$2" '$1 == key { print $2; found = 1 } END { if (!found) print 0 }' <<< "$1"
}

# pct <numerator> <denominator> -> "NN.NN", or "n/a" if denominator is 0.
pct() {
  local n="$1" d="$2"
  if (( d > 0 )); then
    awk -v n="$n" -v d="$d" 'BEGIN { printf "%.2f", n/d*100 }'
  else
    printf 'n/a'
  fi
}

# unbound_stats -> current `unbound-control stats_noreset` output on stdout.
# Just passes through unbound-control's exit status; callers must check it
# (calling die() from in here wouldn't work — command substitution runs
# this in a subshell, so exit would only kill the subshell, not the script).
unbound_stats() {
  unbound-control stats_noreset 2>&1
}

# ---- preflight checks ---------------------------------------------------

for bin in dig unbound-control; do
  command -v "$bin" >/dev/null 2>&1 || die "'$bin' not found in PATH"
done

[[ -f "$DOMAIN_FILE" ]] || die "domain file not found: $DOMAIN_FILE"

# ---- load domain list ----------------------------------------------------

# Strip Windows line endings up front so a CRLF domain file (e.g. one
# built from a CSV export) can't turn "example.com" into "example.com\r"
# and silently break every lookup.
domains=()
while IFS= read -r line; do
  [[ -z "$line" || "$line" = '#'* ]] && continue
  domains+=("$line")
done < <(tr -d '\r' < "$DOMAIN_FILE")

(( ${#domains[@]} > 0 )) || die "no domains found in $DOMAIN_FILE"

# Fail fast if the resolver isn't answering at all — without this, a typo'd
# DNS_SERVER makes all ~1800 queries wait out their 2s timeout (~an hour of
# silence). One root-NS probe answers from cache/hints on any live resolver.
dig @"$DNS_SERVER" "${DIG_OPTS[@]}" +short . NS >/dev/null 2>&1 \
  || die "resolver $DNS_SERVER did not answer a probe query (. NS)"

# ---- warm the cache -------------------------------------------------------

# Build one dig(1) batch file covering two passes over every domain/type
# combo, then warm the cache with a single dig invocation instead of
# forking a process per query. The first pass should mostly miss (cold
# cache), the second should mostly hit — that gap is the "warmup hit rate"
# reported below.
queries_for_one_pass() {
  local d t
  for d in "${domains[@]}"; do
    for t in "${RECORD_TYPES[@]}"; do
      printf '%s %s\n' "$d" "$t"
    done
  done
}

batch_file="$(mktemp)"
trap 'rm -f "$batch_file"' EXIT

{ queries_for_one_pass; queries_for_one_pass; } > "$batch_file"

# Grab the "before" snapshot first, so a broken unbound-control connection
# (unbound not running, socket permissions, etc.) is caught before we
# announce anything or send a single query.
before_stats="$(unbound_stats)" || die "unbound-control stats_noreset failed:"$'\n'"$before_stats"

section "Warming ${#domains[@]} domains (2 passes x ${#RECORD_TYPES[@]} record types) via $DNS_SERVER"

dig @"$DNS_SERVER" -f "$batch_file" "${DIG_OPTS[@]}" +short >/dev/null 2>&1
after_stats="$(unbound_stats)" || die "unbound-control stats_noreset failed:"$'\n'"$after_stats"

# ---- report ---------------------------------------------------------------

before_hits=$(get_stat "$before_stats" total.num.cachehits)
before_misses=$(get_stat "$before_stats" total.num.cachemiss)
before_msg=$(get_stat "$before_stats" msg.cache.count)
before_rrset=$(get_stat "$before_stats" rrset.cache.count)

after_hits=$(get_stat "$after_stats" total.num.cachehits)
after_misses=$(get_stat "$after_stats" total.num.cachemiss)
after_msg=$(get_stat "$after_stats" msg.cache.count)
after_rrset=$(get_stat "$after_stats" rrset.cache.count)

delta_hits=$(( after_hits - before_hits ))
delta_misses=$(( after_misses - before_misses ))
delta_msg=$(( after_msg - before_msg ))
delta_rrset=$(( after_rrset - before_rrset ))

section "Cache stats"
printf "%-16s %10s %10s %10s\n" "" "before" "after" "delta"
printf "%-16s %10d %10d %+10d\n" "hits" "$before_hits" "$after_hits" "$delta_hits"
printf "%-16s %10d %10d %+10d\n" "misses" "$before_misses" "$after_misses" "$delta_misses"
printf "%-16s %10d %10d %+10d\n" "msg entries" "$before_msg" "$after_msg" "$delta_msg"
printf "%-16s %10d %10d %+10d\n" "rrset entries" "$before_rrset" "$after_rrset" "$delta_rrset"

echo
printf "Warmup hit rate:   %s%%  (%d hits / %d queries during this run)\n" \
  "$(pct "$delta_hits" "$((delta_hits + delta_misses))")" "$delta_hits" "$((delta_hits + delta_misses))"
printf "Total hit rate:    %s%%  (%d hits / %d queries overall)\n" \
  "$(pct "$after_hits" "$((after_hits + after_misses))")" "$after_hits" "$((after_hits + after_misses))"
