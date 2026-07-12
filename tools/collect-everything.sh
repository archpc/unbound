#!/usr/bin/env bash

set -u
set -o pipefail

REMOTE_HOST="${REMOTE_HOST:-localadmin@mini.magpie-bushmaster.ts.net}"
REMOTE_SUDO="${REMOTE_SUDO:-1}"
INCLUDE_UNTRACKED="${INCLUDE_UNTRACKED:-1}"
INCLUDE_GIT_DIFF="${INCLUDE_GIT_DIFF:-1}"
MAX_FILE_BYTES="${MAX_FILE_BYTES:-5242880}"
MAX_CACHE_BYTES="${MAX_CACHE_BYTES:-157286400}"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
OUTPUT_DIR="${1:-$HOME/Desktop/unbound-notebooklm-$TIMESTAMP}"

SSH_OPTIONS=(
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=2
)

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/unbound-notebooklm.XXXXXX")" || exit 1
FILE_LIST="$TMP_DIR/repo-files.txt"
REMOTE_SCRIPT="$TMP_DIR/remote-collector.sh"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT HUP INT TERM

have() {
  command -v "$1" >/dev/null 2>&1
}

file_size() {
  if stat -f '%z' "$1" >/dev/null 2>&1; then
    stat -f '%z' "$1"
  elif stat -c '%s' "$1" >/dev/null 2>&1; then
    stat -c '%s' "$1"
  else
    wc -c < "$1" | tr -d ' '
  fi
}

sha256_file() {
  if have shasum; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif have sha256sum; then
    sha256sum "$1" | awk '{print $1}'
  else
    printf 'unavailable\n'
  fi
}

section() {
  printf '\n\n================================================================================\n'
  printf '## %s\n' "$1"
  printf '================================================================================\n'
}

subsection() {
  printf '\n--------------------------------------------------------------------------------\n'
  printf '### %s\n' "$1"
  printf '%s\n' '--------------------------------------------------------------------------------'
}

# This is a defense-in-depth filter for captured command output. Secret-bearing files
# are excluded separately below; neither control should be treated as a substitute for
# reviewing the completed bundle before sharing it.
redact_stream() {
  sed -E \
    -e 's/^([[:space:]]*(password|passwd|secret|token|api[_-]?key|auth[_-]?token|ntfy[_-]?topic|webhook)[[:space:]]*[:=][[:space:]]*).*/\1[REDACTED]/I' \
    -e 's/(Authorization:[[:space:]]*(Bearer|Basic)[[:space:]]+).*/\1[REDACTED]/I' \
    -e 's#(https://ntfy\.sh/)[A-Za-z0-9._-]+#\1[REDACTED]#gI' \
    -e 's#(https?://)[^/@[:space:]]+:[^/@[:space:]]+@#\1[REDACTED]@#g'
}

is_secret_path() {
  local lower
  lower="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"

  # These two runtime files have innocuous-looking names but contain the ntfy channel
  # credential and private browsing-derived warm-list data, respectively.
  case "$lower" in
    .env|*/.env|*/.env.*|\
    ntfy.topic|*/ntfy.topic|\
    dns-warm-domains.personal.txt|*/dns-warm-domains.personal.txt|\
    *.pem|*.key|*.p12|*.pfx|*.jks|\
    */id_rsa|*/id_rsa.*|\
    */id_ed25519|*/id_ed25519.*|\
    */credentials|*/credentials.*|\
    */secret|*/secret.*|\
    */secrets|*/secrets.*|\
    */token|*/token.*|\
    */auth-token|*/auth_token)
      return 0
      ;;
  esac

  return 1
}

is_excluded_path() {
  case "$1" in
    .git/*|\
    .venv/*|venv/*|env/*|\
    node_modules/*|\
    __pycache__/*|*/__pycache__/*|\
    .pytest_cache/*|*/.pytest_cache/*|\
    .mypy_cache/*|*/.mypy_cache/*|\
    .ruff_cache/*|*/.ruff_cache/*|\
    build/*|dist/*|coverage/*|htmlcov/*|\
    *.pyc|*.pyo|*.DS_Store|\
    unbound-notebooklm-*|\
    unbound-everything-*|\
    unbound-repo-dump-*)
      return 0
      ;;
  esac

  return 1
}

is_documentation_path() {
  local path base upper

  path="$1"
  base="${path##*/}"
  upper="$(printf '%s' "$base" | tr '[:lower:]' '[:upper:]')"

  case "$path" in
    docs/*|documentation/*)
      return 0
      ;;
    *.md|*.markdown|*.rst|*.adoc|*.asciidoc)
      return 0
      ;;
  esac

  case "$upper" in
    README*|\
    ARCHITECTURE*|\
    RUNBOOK*|\
    DEPLOYMENT*|\
    TROUBLESHOOTING*|\
    REFERENCE*|\
    DISASTER-RECOVERY*|\
    DISASTER_RECOVERY*|\
    DASHBOARDS*|\
    REPO_LAYOUT*|\
    AGENTS*|\
    CLAUDE*|\
    CHANGELOG*|\
    CONTRIBUTING*|\
    LICENSE*)
      return 0
      ;;
  esac

  return 1
}

is_text_file() {
  local mime

  [ ! -s "$1" ] && return 0

  if have file; then
    mime="$(file -b --mime-type "$1" 2>/dev/null || true)"

    case "$mime" in
      text/*|\
      application/json|\
      application/xml|\
      application/yaml|\
      application/x-yaml|\
      application/toml|\
      application/javascript|\
      application/sql|\
      application/x-shellscript|\
      application/x-python|\
      inode/x-empty)
        return 0
        ;;
    esac
  fi

  LC_ALL=C grep -Iq '' "$1" 2>/dev/null
}

collect_file_list() {
  cd "$REPO_ROOT" || exit 1

  if [ -d .git ] && have git; then
    if [ "$INCLUDE_UNTRACKED" -eq 1 ]; then
      git ls-files -c -o --exclude-standard
    else
      git ls-files -c
    fi
  else
    find . -type f -print | sed 's#^\./##'
  fi |
    LC_ALL=C sort -u |
    while IFS= read -r path; do
      [ -n "$path" ] || continue
      is_excluded_path "$path" || printf '%s\n' "$path"
    done > "$FILE_LIST"
}

append_file_record() {
  local relative="$1"
  local absolute="$REPO_ROOT/$relative"
  local size type

  subsection "$relative"

  if [ ! -f "$absolute" ]; then
    printf '[SKIPPED: missing or not a regular file]\n'
    return
  fi

  if is_secret_path "$relative"; then
    printf '[SKIPPED: likely credential or key material]\n'
    return
  fi

  size="$(file_size "$absolute" 2>/dev/null || printf 0)"
  type="$(file -b "$absolute" 2>/dev/null || printf unknown)"

  printf 'Path: %s\n' "$absolute"
  printf 'Size: %s bytes\n' "$size"
  printf 'Type: %s\n' "$type"

  if [ "$MAX_FILE_BYTES" -gt 0 ] &&
     [ "$size" -gt "$MAX_FILE_BYTES" ]; then
    printf '\n[SKIPPED CONTENT: exceeds MAX_FILE_BYTES=%s]\n' \
      "$MAX_FILE_BYTES"
    printf 'SHA-256: %s\n' "$(sha256_file "$absolute")"
    return
  fi

  if ! is_text_file "$absolute"; then
    printf '\n[SKIPPED CONTENT: binary or unsupported type]\n'
    printf 'SHA-256: %s\n' "$(sha256_file "$absolute")"
    return
  fi

  printf '\n----- BEGIN FILE: %s -----\n' "$relative"
  redact_stream < "$absolute"
  printf '\n----- END FILE: %s -----\n' "$relative"
}

write_inventory() {
  local out="$OUTPUT_DIR/01-repository-inventory.txt"

  {
    section 'Repository inventory'

    printf 'Repository: %s\n' "$REPO_ROOT"
    printf 'Generated: %s\n\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"

    printf '%-16s %-12s %s\n' 'KIND' 'BYTES' 'PATH'
    printf '%-16s %-12s %s\n' '----------------' '------------' '----'

    while IFS= read -r relative; do
      local absolute="$REPO_ROOT/$relative"
      local kind='missing'
      local size='0'

      if [ -f "$absolute" ]; then
        size="$(file_size "$absolute" 2>/dev/null || printf 0)"

        if is_secret_path "$relative"; then
          kind='secret-skip'
        elif is_documentation_path "$relative"; then
          kind='documentation'
        elif is_text_file "$absolute"; then
          kind='text'
        else
          kind='binary'
        fi
      fi

      printf '%-16s %-12s %s\n' "$kind" "$size" "$relative"
    done < "$FILE_LIST"
  } > "$out"
}

write_documentation() {
  local out="$OUTPUT_DIR/02-documentation.md"

  {
    printf '# Repository documentation\n\n'
    printf 'Source repository: `%s`  \n' "$REPO_ROOT"
    printf 'Generated: `%s`\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"

    while IFS= read -r relative; do
      is_documentation_path "$relative" || continue
      append_file_record "$relative"
    done < "$FILE_LIST"
  } > "$out"
}

write_repository_files() {
  local out="$OUTPUT_DIR/03-config-scripts-tests-and-other-files.txt"

  {
    section 'Repository configuration, scripts, tests, and other files'

    printf 'Documentation-classified files are in 02-documentation.md.\n'

    while IFS= read -r relative; do
      is_documentation_path "$relative" && continue
      append_file_record "$relative"
    done < "$FILE_LIST"
  } > "$out"
}

write_git_state() {
  local out="$OUTPUT_DIR/04-git-and-deployment-state.txt"

  {
    section 'Git and deployment state'

    if [ ! -d "$REPO_ROOT/.git" ]; then
      printf 'Not a Git repository: %s\n' "$REPO_ROOT"
      return
    fi

    subsection 'Status'
    (
      cd "$REPO_ROOT" &&
      git status --short --branch
    ) 2>&1

    subsection 'Current commit'
    (
      cd "$REPO_ROOT" &&
      git rev-parse HEAD
    ) 2>&1

    subsection 'Recent history'
    (
      cd "$REPO_ROOT" &&
      git log --oneline --decorate --graph -50
    ) 2>&1

    subsection 'Branches'
    (
      cd "$REPO_ROOT" &&
      git branch -avv
    ) 2>&1

    subsection 'Remotes'
    (
      cd "$REPO_ROOT" &&
      git remote -v
    ) 2>&1 | redact_stream

    subsection 'Tracked files'
    (
      cd "$REPO_ROOT" &&
      git ls-files | LC_ALL=C sort
    ) 2>&1

    subsection 'Ignored files'
    (
      cd "$REPO_ROOT" &&
      git status --ignored --short
    ) 2>&1

    if [ "$INCLUDE_GIT_DIFF" -eq 1 ]; then
      subsection 'Working tree diff'
      (
        cd "$REPO_ROOT" &&
        git diff --no-ext-diff --text
      ) 2>&1 | redact_stream

      subsection 'Staged diff'
      (
        cd "$REPO_ROOT" &&
        git diff --cached --no-ext-diff --text
      ) 2>&1 | redact_stream
    fi

    if [ -f "$REPO_ROOT/deployment-manifest.json" ]; then
      subsection 'Deployment manifest'
      redact_stream < "$REPO_ROOT/deployment-manifest.json"
    fi

    if [ -f "$REPO_ROOT/tools/verify-deployment.py" ]; then
      subsection 'Read-only deployment verifier output'
      (
        cd "$REPO_ROOT" &&
        python3 tools/verify-deployment.py
      ) 2>&1
    fi
  } > "$out"
}

write_remote_script() {
  cat > "$REMOTE_SCRIPT" <<'REMOTE_EOF'
#!/usr/bin/env bash

set -u
set -o pipefail

MODE="${1:-runtime}"
REMOTE_SUDO="${REMOTE_SUDO:-1}"
MAX_CACHE_BYTES="${MAX_CACHE_BYTES:-157286400}"
MAX_FILE_BYTES="${MAX_FILE_BYTES:-5242880}"

PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export PATH

have() {
  command -v "$1" >/dev/null 2>&1
}

section() {
  printf '\n\n================================================================================\n'
  printf '## %s\n' "$1"
  printf '================================================================================\n'
}

subsection() {
  printf '\n--------------------------------------------------------------------------------\n'
  printf '### %s\n' "$1"
  printf '%s\n' '--------------------------------------------------------------------------------'
}

# Keep this embedded remote redactor aligned with the local redact_stream above. The
# generated script is self-contained and cannot call functions from the parent process.
redact_stream() {
  sed -E \
    -e 's/^([[:space:]]*(password|passwd|secret|token|api[_-]?key|auth[_-]?token|ntfy[_-]?topic|webhook)[[:space:]]*[:=][[:space:]]*).*/\1[REDACTED]/I' \
    -e 's/(Authorization:[[:space:]]*(Bearer|Basic)[[:space:]]+).*/\1[REDACTED]/I' \
    -e 's#(https://ntfy\.sh/)[A-Za-z0-9._-]+#\1[REDACTED]#gI' \
    -e 's#(https?://)[^/@[:space:]]+:[^/@[:space:]]+@#\1[REDACTED]@#g'
}

run_shell() {
  subsection "$1"
  printf '$ %s\n\n' "$2"

  /bin/sh -c "$2" 2>&1
  status=$?

  printf '\n[exit status: %s]\n' "$status"
  return 0
}

find_cmd() {
  command -v "$1" 2>/dev/null && return 0
  shift

  for path in "$@"; do
    if [ -x "$path" ]; then
      printf '%s\n' "$path"
      return 0
    fi
  done

  return 1
}

run_control() {
  local control="$1"
  shift

  "$control" "$@" 2>&1
  status=$?

  if [ "$status" -ne 0 ] &&
     [ "$REMOTE_SUDO" -eq 1 ] &&
     have sudo; then
    printf '\n[retrying with sudo -n]\n\n'
    sudo -n "$control" "$@" 2>&1
    status=$?
  fi

  return "$status"
}

remote_file_size() {
  if stat -f '%z' "$1" >/dev/null 2>&1; then
    stat -f '%z' "$1"
  elif stat -c '%s' "$1" >/dev/null 2>&1; then
    stat -c '%s' "$1"
  else
    wc -c < "$1" | tr -d ' '
  fi
}

dump_remote_file() {
  local path="$1"
  local bytes

  [ -f "$path" ] || return 0

  subsection "$path"

  bytes="$(remote_file_size "$path" 2>/dev/null || printf 0)"

  printf 'Path: %s\n' "$path"
  printf 'Size: %s bytes\n' "$bytes"

  if [ "$MAX_FILE_BYTES" -gt 0 ] &&
     [ "$bytes" -gt "$MAX_FILE_BYTES" ]; then
    printf '[SKIPPED CONTENT: exceeds MAX_FILE_BYTES=%s]\n' \
      "$MAX_FILE_BYTES"
    return 0
  fi

  printf '\n----- BEGIN REMOTE FILE: %s -----\n' "$path"
  redact_stream < "$path"
  printf '\n----- END REMOTE FILE: %s -----\n' "$path"
}

UNBOUND_CONTROL="$(
  find_cmd \
    unbound-control \
    /opt/homebrew/sbin/unbound-control \
    /usr/local/sbin/unbound-control \
    /usr/sbin/unbound-control \
    || true
)"

UNBOUND_CHECKCONF="$(
  find_cmd \
    unbound-checkconf \
    /opt/homebrew/sbin/unbound-checkconf \
    /usr/local/sbin/unbound-checkconf \
    /usr/sbin/unbound-checkconf \
    || true
)"

case "$MODE" in
  config)
    section 'Mini deployed configuration'

    printf 'Host: %s\n' "$(hostname)"
    printf 'User: %s\n' "$(id -un)"
    printf 'Generated: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"

    if [ -d /opt/homebrew/etc/unbound ]; then
      while IFS= read -r path; do
        dump_remote_file "$path"
      done < <(
        find /opt/homebrew/etc/unbound \
          -type f \
          \( \
            -name '*.conf' -o \
            -name '*.list' -o \
            -name '*.txt' -o \
            -name '*.zone' -o \
            -name '*.rpz' \
          \) \
          2>/dev/null |
        LC_ALL=C sort
      )
    fi

    if [ -d /opt/homebrew/etc/prometheus ]; then
      while IFS= read -r path; do
        dump_remote_file "$path"
      done < <(
        find /opt/homebrew/etc/prometheus \
          -type f \
          2>/dev/null |
        LC_ALL=C sort
      )
    fi

    if [ -d /opt/homebrew/etc/grafana ]; then
      while IFS= read -r path; do
        dump_remote_file "$path"
      done < <(
        find /opt/homebrew/etc/grafana \
          -maxdepth 3 \
          -type f \
          2>/dev/null |
        LC_ALL=C sort
      )
    fi

    for path in \
      /Library/LaunchDaemons/*unbound*.plist \
      /Library/LaunchDaemons/*prometheus*.plist \
      /Library/LaunchDaemons/*grafana*.plist \
      /Library/LaunchDaemons/*blackbox*.plist \
      /Library/LaunchDaemons/*exporter*.plist
    do
      [ -f "$path" ] && dump_remote_file "$path"
    done

    if [ -n "$UNBOUND_CHECKCONF" ]; then
      for cfg in \
        /opt/homebrew/etc/unbound/unbound.conf \
        /usr/local/etc/unbound/unbound.conf \
        /etc/unbound/unbound.conf
      do
        [ -f "$cfg" ] || continue

        subsection "Validate $cfg"
        "$UNBOUND_CHECKCONF" "$cfg" 2>&1
        printf '\n[exit status: %s]\n' "$?"
      done
    fi
    ;;

  runtime)
    section 'Mini Unbound runtime diagnostics'

    printf 'Host: %s\n' "$(hostname)"
    printf 'User: %s\n' "$(id -un)"
    printf 'Generated: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"

    run_shell \
      'System identity' \
      'uname -a; sw_vers; uptime; id'

    run_shell \
      'Unbound processes' \
      "pgrep -alf 'unbound|unbound_exporter' 2>/dev/null || true"

    run_shell \
      'DNS sockets' \
      "lsof -nP -iTCP:53 -sTCP:LISTEN 2>/dev/null || true
       lsof -nP -iUDP:53 2>/dev/null || true"

    run_shell \
      'Homebrew service state' \
      "brew services list 2>/dev/null || true
       brew services info unbound 2>/dev/null || true"

    run_shell \
      'Launchd jobs containing Unbound' \
      "launchctl list 2>/dev/null | grep -i unbound || true"

    if [ -n "$UNBOUND_CONTROL" ]; then
      for cmd in \
        status \
        stats_noreset \
        list_local_zones \
        list_local_data \
        list_forwards \
        list_stubs \
        list_insecure
      do
        subsection "unbound-control $cmd"
        run_control "$UNBOUND_CONTROL" "$cmd" || true
      done

      subsection 'Selected live Unbound options'

      for opt in \
        verbosity \
        num-threads \
        msg-cache-size \
        rrset-cache-size \
        cache-min-ttl \
        cache-max-ttl \
        serve-expired \
        module-config \
        interface \
        access-control
      do
        printf '\n[%s]\n' "$opt"
        run_control "$UNBOUND_CONTROL" get_option "$opt" || true
      done
    else
      printf '\nERROR: unbound-control not found.\n'
    fi

    run_shell \
      'Localhost resolver check' \
      'dig @127.0.0.1 example.com A +dnssec +stats 2>&1 || true'

    run_shell \
      'LAN resolver check' \
      'dig @192.168.0.100 example.com A +dnssec +stats 2>&1 || true'

    run_shell \
      'DNSSEC failure check' \
      'dig @127.0.0.1 dnssec-failed.org A +dnssec +stats 2>&1 || true'

    run_shell \
      'RPZ canary check' \
      'dig @127.0.0.1 google-analytics.com A +stats 2>&1 || true'
    ;;

  monitoring)
    section 'Mini network and monitoring diagnostics'

    printf 'Host: %s\n' "$(hostname)"
    printf 'Generated: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"

    run_shell \
      'Interfaces' \
      'ifconfig'

    run_shell \
      'Routes' \
      'netstat -rn'

    run_shell \
      'Resolver configuration' \
      'scutil --dns'

    run_shell \
      'Tailscale' \
      'tailscale status 2>/dev/null || true
       tailscale ip -4 2>/dev/null || true
       tailscale netcheck 2>/dev/null || true'

    run_shell \
      'Disk and memory' \
      'df -h
       memory_pressure 2>/dev/null || true'

    run_shell \
      'Monitoring processes' \
      "pgrep -alf 'prometheus|grafana|node_exporter|blackbox_exporter|unbound_exporter|alert_notifier' 2>/dev/null || true"

    run_shell \
      'Monitoring listeners' \
      "lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null |
       grep -E ':(3000|9090|9100|9115|9167)[[:space:]]' ||
       true"

    run_shell \
      'Prometheus targets' \
      'curl -fsS --max-time 10 http://127.0.0.1:9090/api/v1/targets 2>/dev/null || true'

    run_shell \
      'Prometheus alerts' \
      'curl -fsS --max-time 10 http://127.0.0.1:9090/api/v1/alerts 2>/dev/null || true'

    run_shell \
      'Prometheus runtime info' \
      'curl -fsS --max-time 10 http://127.0.0.1:9090/api/v1/status/runtimeinfo 2>/dev/null || true'

    run_shell \
      'Unbound exporter metrics' \
      'curl -fsS --max-time 10 http://127.0.0.1:9167/metrics 2>/dev/null || true'

    run_shell \
      'Blackbox exporter metrics' \
      'curl -fsS --max-time 10 http://127.0.0.1:9115/metrics 2>/dev/null || true'

    run_shell \
      'Node exporter selected metrics' \
      "curl -fsS --max-time 10 http://127.0.0.1:9100/metrics 2>/dev/null |
       grep -E '^(node_textfile_|node_load|node_memory_|node_filesystem_|node_time_)' ||
       true"
    ;;

  logs)
    section 'Mini recent logs'

    printf 'Host: %s\n' "$(hostname)"
    printf 'Generated: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"

    run_shell \
      'Unified Unbound logs, last 24 hours' \
      "log show \
         --last 24h \
         --style compact \
         --predicate \
         '(process == \"unbound\") OR
          (eventMessage CONTAINS[c] \"unbound\")' \
         2>/dev/null || true"

    run_shell \
      'Unified monitoring and launchd logs, last 24 hours' \
      "log show \
         --last 24h \
         --style compact \
         --predicate \
         '(eventMessage CONTAINS[c] \"prometheus\") OR
          (eventMessage CONTAINS[c] \"grafana\") OR
          (eventMessage CONTAINS[c] \"blackbox\") OR
          (eventMessage CONTAINS[c] \"exporter\")' \
         2>/dev/null || true"

    for logfile in \
      /opt/homebrew/var/log/unbound.log \
      /opt/homebrew/var/log/prometheus.log \
      /opt/homebrew/var/log/grafana/grafana.log
    do
      [ -f "$logfile" ] || continue

      subsection "Tail of $logfile"
      tail -n 2000 "$logfile" 2>&1
    done
    ;;

  cache)
    section 'Mini Unbound cache dump'

    printf 'Host: %s\n' "$(hostname)"
    printf 'Generated: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"

    if [ -z "$UNBOUND_CONTROL" ]; then
      printf 'ERROR: unbound-control not found.\n'
      exit 1
    fi

    cache_tmp="$(mktemp "${TMPDIR:-/tmp}/unbound-cache.XXXXXX")" || exit 1
    trap 'rm -f "$cache_tmp"' EXIT HUP INT TERM

    "$UNBOUND_CONTROL" dump_cache > "$cache_tmp" 2>&1
    status=$?

    if [ "$status" -ne 0 ] &&
       [ "$REMOTE_SUDO" -eq 1 ] &&
       have sudo; then
      sudo -n "$UNBOUND_CONTROL" dump_cache > "$cache_tmp" 2>&1
      status=$?
    fi

    bytes="$(wc -c < "$cache_tmp" | tr -d ' ')"

    printf 'Exit status: %s\n' "$status"
    printf 'Bytes captured: %s\n' "$bytes"

    if [ "$MAX_CACHE_BYTES" -gt 0 ] &&
       [ "$bytes" -gt "$MAX_CACHE_BYTES" ]; then
      printf 'Truncated to %s bytes.\n\n' "$MAX_CACHE_BYTES"
      head -c "$MAX_CACHE_BYTES" "$cache_tmp"
    else
      printf '\n'
      cat "$cache_tmp"
    fi
    ;;

  *)
    printf 'Unknown mode: %s\n' "$MODE" >&2
    exit 2
    ;;
esac
REMOTE_EOF

  chmod +x "$REMOTE_SCRIPT"
}

ssh_collect() {
  local mode="$1"
  local out="$2"

  {
    printf 'Remote target: %s\n' "$REMOTE_HOST"
    printf 'Mode: %s\n\n' "$mode"

    ssh "${SSH_OPTIONS[@]}" "$REMOTE_HOST" \
      "REMOTE_SUDO='$REMOTE_SUDO' \
       MAX_CACHE_BYTES='$MAX_CACHE_BYTES' \
       MAX_FILE_BYTES='$MAX_FILE_BYTES' \
       /bin/bash -s -- '$mode'" \
      < "$REMOTE_SCRIPT"

    printf '\n[ssh exit status: %s]\n' "$?"
  } > "$out" 2>&1
}

write_index() {
  local out="$OUTPUT_DIR/00-INDEX.md"

  {
    printf '# Unbound stack collection\n\n'

    printf -- '- Generated: `%s`\n' \
      "$(date '+%Y-%m-%d %H:%M:%S %z')"

    printf -- '- MacBook repository: `%s`\n' \
      "$REPO_ROOT"

    printf -- '- Mini target: `%s`\n' \
      "$REMOTE_HOST"

    printf -- '- Output folder: `%s`\n\n' \
      "$OUTPUT_DIR"

    printf '## NotebookLM source guide\n\n'

    printf '1. `02-documentation.md` for architecture, runbooks, recovery, and written intent.\n'
    printf '2. `03-config-scripts-tests-and-other-files.txt` for implementation details.\n'
    printf '3. `04-git-and-deployment-state.txt` for history, drift, and current repository state.\n'
    printf '4. `05-mini-deployed-config.txt` for what is actually installed on the Mini.\n'
    printf '5. `06-mini-unbound-runtime.txt` for resolver health and live state.\n'
    printf '6. `07-mini-monitoring-and-network.txt` for Prometheus, exporters, routes, and Tailscale.\n'
    printf '7. `08-mini-logs.txt` for recent failures and operational history.\n'
    printf '8. `09-unbound-cache.txt` only for cache-specific analysis. Leave it deselected otherwise because it is noisy and potentially private.\n\n'

    printf '## Files\n\n'

    for path in "$OUTPUT_DIR"/*; do
      [ -f "$path" ] || continue
      [ "$(basename "$path")" = '00-INDEX.md' ] && continue

      printf -- '- `%s`: %s bytes\n' \
        "$(basename "$path")" \
        "$(file_size "$path")"
    done

    printf '\n## Privacy warning\n\n'
    printf 'The cache, logs, internal hostnames, IP addresses, paths, and metrics may reveal device or browsing activity. Automated redaction is incomplete because regex remains a poor substitute for judgment, despite decades of humans trying anyway.\n'
  } > "$out"
}

write_checksums() {
  local out="$OUTPUT_DIR/SHA256SUMS.txt"

  : > "$out"

  for path in "$OUTPUT_DIR"/*; do
    [ -f "$path" ] || continue
    [ "$(basename "$path")" = 'SHA256SUMS.txt' ] && continue

    printf '%s  %s\n' \
      "$(sha256_file "$path")" \
      "$(basename "$path")" \
      >> "$out"
  done
}

main() {
  if [ ! -d "$REPO_ROOT" ]; then
    printf 'ERROR: repository not found: %s\n' "$REPO_ROOT" >&2
    exit 1
  fi

  if [ ! -d "$HOME/Desktop" ]; then
    printf 'ERROR: Desktop directory not found: %s\n' \
      "$HOME/Desktop" >&2
    exit 1
  fi

  if ! have ssh; then
    printf 'ERROR: ssh not found.\n' >&2
    exit 1
  fi

  mkdir -p "$OUTPUT_DIR" || exit 1

  collect_file_list
  write_remote_script

  write_inventory
  write_documentation
  write_repository_files
  write_git_state

  if ssh "${SSH_OPTIONS[@]}" "$REMOTE_HOST" \
    'printf SSH_OK' >/dev/null 2>&1; then

    ssh_collect \
      config \
      "$OUTPUT_DIR/05-mini-deployed-config.txt"

    ssh_collect \
      runtime \
      "$OUTPUT_DIR/06-mini-unbound-runtime.txt"

    ssh_collect \
      monitoring \
      "$OUTPUT_DIR/07-mini-monitoring-and-network.txt"

    ssh_collect \
      logs \
      "$OUTPUT_DIR/08-mini-logs.txt"

    ssh_collect \
      cache \
      "$OUTPUT_DIR/09-unbound-cache.txt"
  else
    printf 'SSH connection failed: %s\n' \
      "$REMOTE_HOST" \
      > "$OUTPUT_DIR/05-09-REMOTE-COLLECTION-FAILED.txt"
  fi

  write_index
  write_checksums

  printf 'Created NotebookLM collection:\n'
  printf '  %s\n\n' "$OUTPUT_DIR"

  for path in "$OUTPUT_DIR"/*; do
    [ -f "$path" ] &&
      printf '%s\n' "$path"
  done | LC_ALL=C sort
}

main "$@"
