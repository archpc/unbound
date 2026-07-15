#!/bin/bash
# Operator-only helper for the reviewed notifier-hardening change.
# This file is never called by offline repository validation.  The deploy,
# rollback, and acceptance modes change live state and must be invoked
# deliberately on the mini by the operator.
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROMTOOL="/opt/homebrew/opt/prometheus/bin/promtool"
RUNTIME_SCRIPT="/usr/local/libexec/unbound-stack/monitoring/prometheus_alert_notifier.py"
RUNTIME_RULES="/opt/homebrew/etc/prometheus.rules.yml"
RUNTIME_CONFIG="/opt/homebrew/etc/prometheus.yml"
RUNTIME_DASHBOARDS="/opt/homebrew/etc/grafana/dashboards"
STATE_DIR="/Users/localadmin/monitoring/state"
STATE_FILE="$STATE_DIR/notified-alerts.json"
NOTIFIER_JOB="system/local.unbound.alertnotify"
NOTIFIER_PLIST="/Library/LaunchDaemons/local.unbound.alertnotify.plist"
NOTIFIER_DISABLED=0
NOTIFIER_STOPPED=0
RECOVERY_BACKUP=""

usage() {
  echo "usage: $0 acceptance | deploy BACKUP_DIR | rollback BACKUP_DIR | verify" >&2
  exit 64
}

require_mini() {
  [ "$(/usr/bin/uname -s)" = "Darwin" ] || {
    echo "this helper must run on the target mini" >&2
    exit 1
  }
  [ "$(/usr/bin/id -un)" = "localadmin" ] || {
    echo "run this helper as localadmin; sudo is requested only for controlled installs" >&2
    exit 1
  }
  [ -x "$PROMTOOL" ] || {
    echo "target promtool is unavailable" >&2
    exit 1
  }
}

require_clean_checkout() {
  /usr/bin/git -C "$ROOT" rev-parse --verify HEAD >/dev/null
  if [ -n "$(/usr/bin/git -C "$ROOT" status --porcelain=v1 --untracked-files=normal)" ]; then
    echo "candidate checkout must be clean and fully committed" >&2
    exit 1
  fi
}

validate_candidate() {
  require_clean_checkout
  /usr/bin/python3 -B "$ROOT/scripts/monitoring/prometheus_alert_notifier.py" \
    --selftest
  "$PROMTOOL" check config "$ROOT/config/prometheus/prometheus.yml"
  "$PROMTOOL" check rules "$ROOT/config/prometheus/prometheus.rules.yml"
  /usr/bin/python3 "$ROOT/tools/generate-grafana-dashboards.py" --check
  for file in "$ROOT"/config/grafana/dashboards/*.json; do
    /usr/bin/python3 -m json.tool "$file" >/dev/null
  done
  require_clean_checkout
}

backup_live() {
  backup=$1
  [ ! -e "$backup" ] || {
    echo "backup destination already exists: $backup" >&2
    exit 1
  }
  /bin/mkdir -p -m 700 "$(/usr/bin/dirname "$backup")"
  /bin/mkdir -m 700 "$backup"
  /bin/cp "$RUNTIME_SCRIPT" "$backup/prometheus_alert_notifier.py"
  /bin/cp "$RUNTIME_RULES" "$backup/prometheus.rules.yml"
  /bin/mkdir -m 700 "$backup/dashboards"
  /bin/cp "$RUNTIME_DASHBOARDS/host-services.json" "$backup/dashboards/"
  /bin/cp "$RUNTIME_DASHBOARDS/operations-overview.json" "$backup/dashboards/"
  if [ -f "$STATE_FILE" ]; then
    /bin/cp "$STATE_FILE" "$backup/notified-alerts.json"
    /bin/chmod 600 "$backup/notified-alerts.json"
    /usr/bin/touch "$backup/state-present"
  else
    /usr/bin/touch "$backup/state-absent"
  fi
  /usr/bin/git -C "$ROOT" rev-parse HEAD >"$backup/candidate-commit.txt"
  /bin/chmod -R go-rwx "$backup"
  echo "rollback backup prepared at $backup"
}

install_atomic_user() {
  source=$1
  destination=$2
  mode=$3
  stage="${destination}.stage.$$"
  /usr/bin/install -m "$mode" "$source" "$stage"
  /bin/mv "$stage" "$destination"
}

install_atomic() {
  source=$1
  destination=$2
  owner=$3
  group=$4
  mode=$5
  stage="${destination}.stage.$$"
  /usr/bin/sudo /usr/bin/install -o "$owner" -g "$group" -m "$mode" \
    "$source" "$stage"
  /usr/bin/sudo /bin/mv "$stage" "$destination"
}

restore_backup_files() {
  backup=$1
  install_atomic "$backup/prometheus_alert_notifier.py" \
    "$RUNTIME_SCRIPT" root wheel 0755
  install_atomic "$backup/prometheus.rules.yml" \
    "$RUNTIME_RULES" localadmin admin 0644
  install_atomic "$backup/dashboards/host-services.json" \
    "$RUNTIME_DASHBOARDS/host-services.json" localadmin admin 0644
  install_atomic "$backup/dashboards/operations-overview.json" \
    "$RUNTIME_DASHBOARDS/operations-overview.json" localadmin admin 0644
  if [ -f "$backup/state-present" ]; then
    [ -f "$backup/notified-alerts.json" ]
    install_atomic_user "$backup/notified-alerts.json" "$STATE_FILE" 0600
  elif [ -f "$backup/state-absent" ]; then
    /bin/rm -f -- "$STATE_FILE"
  else
    echo "backup does not record whether notifier state existed" >&2
    return 1
  fi
}

quiesce_notifier() {
  /usr/bin/sudo /bin/launchctl disable "$NOTIFIER_JOB"
  NOTIFIER_DISABLED=1
  for _attempt in $(/usr/bin/seq 1 30); do
    if ! /usr/bin/pgrep -f '/prometheus_alert_notifier[.]py' >/dev/null; then
      break
    fi
    /bin/sleep 1
  done
  if /usr/bin/pgrep -f '/prometheus_alert_notifier[.]py' >/dev/null; then
    echo "notifier did not become idle within 30 seconds" >&2
    return 1
  fi
  /usr/bin/sudo /bin/launchctl bootout "$NOTIFIER_JOB"
  NOTIFIER_STOPPED=1
}

resume_notifier() {
  if [ "$NOTIFIER_DISABLED" -eq 1 ]; then
    /usr/bin/sudo /bin/launchctl enable "$NOTIFIER_JOB"
    NOTIFIER_DISABLED=0
  fi
  if [ "$NOTIFIER_STOPPED" -eq 1 ]; then
    /usr/bin/sudo /bin/launchctl bootstrap system "$NOTIFIER_PLIST"
    NOTIFIER_STOPPED=0
  fi
}

recover_failed_operation() {
  rc=$1
  trap - ERR INT TERM
  set +e
  echo "operation failed; restoring the exact pre-operation backup" >&2
  if [ -n "$RECOVERY_BACKUP" ]; then
    restore_backup_files "$RECOVERY_BACKUP"
    "$PROMTOOL" check config "$RUNTIME_CONFIG"
    "$PROMTOOL" check rules "$RUNTIME_RULES"
    /usr/bin/pkill -HUP -x prometheus
  fi
  resume_notifier
  exit "$rc"
}

arm_recovery_trap() {
  trap 'recover_failed_operation $?' ERR
  trap 'recover_failed_operation 130' INT
  trap 'recover_failed_operation 143' TERM
}

clear_recovery_trap() {
  trap - ERR INT TERM
  RECOVERY_BACKUP=""
}

deploy() {
  backup=$1
  validate_candidate
  backup_live "$backup"
  RECOVERY_BACKUP=$backup
  arm_recovery_trap
  quiesce_notifier

  install_atomic \
    "$ROOT/scripts/monitoring/prometheus_alert_notifier.py" \
    "$RUNTIME_SCRIPT" root wheel 0755
  install_atomic "$ROOT/config/prometheus/prometheus.rules.yml" \
    "$RUNTIME_RULES" localadmin admin 0644
  install_atomic "$ROOT/config/grafana/dashboards/host-services.json" \
    "$RUNTIME_DASHBOARDS/host-services.json" localadmin admin 0644
  install_atomic "$ROOT/config/grafana/dashboards/operations-overview.json" \
    "$RUNTIME_DASHBOARDS/operations-overview.json" localadmin admin 0644

  /bin/chmod 700 "$STATE_DIR"
  if [ -f "$STATE_FILE" ]; then
    /bin/chmod 600 "$STATE_FILE"
  fi
  "$PROMTOOL" check config "$RUNTIME_CONFIG"
  "$PROMTOOL" check rules "$RUNTIME_RULES"
  /usr/bin/pkill -HUP -x prometheus
  resume_notifier
  clear_recovery_trap
  echo "deployment files installed and Prometheus reloaded"
  echo "wait at least 130 seconds, then run: $0 verify"
}

rollback() {
  backup=$1
  require_clean_checkout
  [ -f "$backup/prometheus_alert_notifier.py" ]
  [ -f "$backup/prometheus.rules.yml" ]
  [ -f "$backup/dashboards/host-services.json" ]
  [ -f "$backup/dashboards/operations-overview.json" ]
  /usr/bin/python3 "$backup/prometheus_alert_notifier.py" --selftest
  "$PROMTOOL" check rules "$backup/prometheus.rules.yml"
  /usr/bin/python3 -m json.tool \
    "$backup/dashboards/host-services.json" >/dev/null
  /usr/bin/python3 -m json.tool \
    "$backup/dashboards/operations-overview.json" >/dev/null
  [ -f "$backup/state-present" ] || [ -f "$backup/state-absent" ]

  guard="${backup}.rollback-guard-$(/bin/date +%Y%m%d-%H%M%S)"
  backup_live "$guard"
  RECOVERY_BACKUP=$guard
  arm_recovery_trap
  quiesce_notifier
  restore_backup_files "$backup"
  "$PROMTOOL" check config "$RUNTIME_CONFIG"
  "$PROMTOOL" check rules "$RUNTIME_RULES"
  /usr/bin/pkill -HUP -x prometheus
  resume_notifier
  clear_recovery_trap
  echo "rollback restored and Prometheus reloaded"
  echo "pre-rollback recovery backup retained at $guard"
}

verify() {
  validate_candidate
  /usr/bin/cmp -s \
    "$ROOT/scripts/monitoring/prometheus_alert_notifier.py" "$RUNTIME_SCRIPT"
  /usr/bin/cmp -s "$ROOT/config/prometheus/prometheus.rules.yml" "$RUNTIME_RULES"
  /usr/bin/cmp -s "$ROOT/config/grafana/dashboards/host-services.json" \
    "$RUNTIME_DASHBOARDS/host-services.json"
  /usr/bin/cmp -s "$ROOT/config/grafana/dashboards/operations-overview.json" \
    "$RUNTIME_DASHBOARDS/operations-overview.json"
  /usr/bin/stat -f 'state-dir mode=%Sp owner=%Su group=%Sg' "$STATE_DIR"
  /usr/bin/stat -f 'state mode=%Sp owner=%Su group=%Sg' "$STATE_FILE"
  /usr/bin/stat -f 'topic mode=%Sp owner=%Su group=%Sg' \
    /Users/localadmin/monitoring/ntfy.topic
  /bin/launchctl print system/local.unbound.alertnotify | \
    /usr/bin/grep -E 'state =|runs =|last exit code|run interval'
  /usr/bin/curl -fsS http://127.0.0.1:9100/metrics | \
    /usr/bin/grep -E '^unbound_ntfy_(prometheus_query_success|alert_state_processed|resolutions_enabled|state_persistence_success|topic_ready|phone_receipt_known|consecutive_failures) '
  /usr/bin/curl -fsSG \
    --data-urlencode 'query=monitoring:notifier_local_path_healthy' \
    http://127.0.0.1:9090/api/v1/query | \
    /usr/bin/python3 -c 'import json,sys; p=json.load(sys.stdin); r=p["data"]["result"]; assert len(r)==1 and r[0]["value"][1]=="1"'
  /usr/bin/python3 "$ROOT/tools/validate-dashboard-promql.py" \
    --dashboard-dir "$ROOT/config/grafana/dashboards" \
    --rules "$ROOT/config/prometheus/prometheus.rules.yml" \
    --prometheus-url http://127.0.0.1:9090
  echo "post-deployment verification passed; phone receipt remains unverified"
}

acceptance() {
  [ "${CONFIRM_REAL_NTFY_SEND:-}" = "YES" ] || {
    echo "set CONFIRM_REAL_NTFY_SEND=YES to authorize one real test notification" >&2
    exit 64
  }
  /usr/bin/python3 "$RUNTIME_SCRIPT" --test-send
  echo "confirm the clearly labeled test on the subscribed device"
}

[ "$#" -ge 1 ] || usage
command=$1
shift
require_mini
case "$command" in
  acceptance)
    [ "$#" -eq 0 ] || usage
    acceptance
    ;;
  deploy)
    [ "$#" -eq 1 ] || usage
    deploy "$1"
    ;;
  rollback)
    [ "$#" -eq 1 ] || usage
    rollback "$1"
    ;;
  verify)
    [ "$#" -eq 0 ] || usage
    verify
    ;;
  *)
    usage
    ;;
esac
