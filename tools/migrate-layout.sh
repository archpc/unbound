#!/bin/bash
# Controlled one-time migration from user-writable runtime scripts to the
# root-owned unbound-stack layout. Run only from a reviewed staged commit.

set -Eeuo pipefail

usage() {
    echo "usage: sudo migrate-layout.sh STAGE_DIR ROLLBACK_DIR" >&2
    exit 2
}

[ "$#" -eq 2 ] || usage
[ "$(id -u)" -eq 0 ] || { echo "migration must run as root" >&2; exit 1; }

STAGE="${1%/}"
ROLLBACK="${2%/}"
EXPECTED_COMMIT="dcb19cf"
LIBEXEC="/usr/local/libexec/unbound-stack"
SHARE="/usr/local/share/unbound-stack"
TEXTFILES="/opt/homebrew/var/lib/node_exporter/textfile"

LABELS=(
    local.unbound.alertnotify
    local.unbound.blocklist
    local.unbound.clientstats
    local.unbound.contentcache
    local.unbound.roothints
    local.unbound.warmboot
    local.unbound.warmlist
)
RUN_AT_LOAD=(
    local.unbound.alertnotify
    local.unbound.clientstats
    local.unbound.contentcache
    local.unbound.warmboot
)

log() { printf '[migration] %s\n' "$*"; }

require_file() {
    [ -f "$1" ] || { echo "required file missing: $1" >&2; return 1; }
}

validate_stage() {
    require_file "$STAGE/COMMIT"
    case "$(head -n1 "$STAGE/COMMIT")" in
        "$EXPECTED_COMMIT"*) ;;
        *) echo "unexpected staged commit" >&2; return 1 ;;
    esac
    require_file "$ROLLBACK/ROLLBACK.txt"
    require_file "$ROLLBACK/metadata.tsv"

    /bin/bash -n "$STAGE"/scripts/dns/*.sh \
        "$STAGE"/scripts/maintenance/*.sh
    /bin/zsh -n "$STAGE/scripts/dns/warm-cache.zsh"
    for plist in "$STAGE"/launchd/*.plist; do
        /usr/bin/plutil -lint "$plist" >/dev/null
    done
    /opt/homebrew/opt/prometheus/bin/promtool check rules \
        "$STAGE/config/prometheus/prometheus.rules.yml" >/dev/null
    /opt/homebrew/opt/prometheus/bin/promtool check config \
        "$STAGE/config/prometheus/prometheus.yml" >/dev/null
    /opt/homebrew/opt/unbound/sbin/unbound-checkconf \
        "$STAGE/config/unbound/unbound.conf" >/dev/null
    /usr/sbin/newsyslog -nv -f \
        "$STAGE/config/newsyslog/unbound-monitoring.conf" >/dev/null
    /usr/bin/python3 "$STAGE/scripts/monitoring/apple_content_cache_exporter.py" \
        --test >/dev/null
    /usr/bin/python3 "$STAGE/scripts/monitoring/unbound_client_rpz_exporter.py" \
        --test >/dev/null
    /usr/bin/python3 "$STAGE/scripts/monitoring/prometheus_alert_notifier.py" \
        --selftest >/dev/null
}

install_targets() {
    /usr/bin/install -d -o root -g wheel -m 0755 \
        "$LIBEXEC/dns" "$LIBEXEC/monitoring" "$SHARE"

    for source in "$STAGE"/scripts/dns/*; do
        /usr/bin/install -o root -g wheel -m 0755 "$source" \
            "$LIBEXEC/dns/$(basename "$source")"
    done
    for source in "$STAGE"/scripts/monitoring/*.py; do
        /usr/bin/install -o root -g wheel -m 0755 "$source" \
            "$LIBEXEC/monitoring/$(basename "$source")"
    done
    /usr/bin/install -o root -g wheel -m 0755 \
        "$STAGE/scripts/maintenance/screenshare.sh" /usr/local/sbin/screenshare
    /usr/bin/install -o root -g wheel -m 0644 \
        "$STAGE/config/unbound/dns-warm-domains.txt" \
        "$SHARE/dns-warm-domains.txt"
    /usr/bin/install -o root -g wheel -m 0644 \
        "$STAGE/config/unbound/blocklist-source" \
        /opt/homebrew/etc/unbound/blocklist-source
    /usr/bin/install -o root -g wheel -m 0644 \
        "$STAGE/config/newsyslog/unbound-monitoring.conf" \
        /etc/newsyslog.d/unbound-monitoring.conf
    /usr/bin/install -o localadmin -g admin -m 0644 \
        "$STAGE/config/grafana/provisioning/dashboards/unbound.yml" \
        /opt/homebrew/etc/grafana/provisioning/dashboards/unbound.yml

    if [ -e /Users/localadmin/warmup/dns-warm-domains.personal.txt ]; then
        /usr/sbin/chown localadmin:staff \
            /Users/localadmin/warmup/dns-warm-domains.personal.txt
        /bin/chmod 0600 /Users/localadmin/warmup/dns-warm-domains.personal.txt
    fi

    for label in "${LABELS[@]}"; do
        /usr/bin/install -o root -g wheel -m 0644 \
            "$STAGE/launchd/$label.plist" "/Library/LaunchDaemons/$label.plist"
    done
}

activate_plists() {
    for label in "${LABELS[@]}"; do
        /bin/launchctl bootout "system/$label" 2>/dev/null || true
        /bin/launchctl bootstrap system "/Library/LaunchDaemons/$label.plist"
    done
}

wait_for_success() {
    local label="$1" output attempt
    for attempt in $(/usr/bin/seq 1 40); do
        output="$(/bin/launchctl print "system/$label" 2>&1)"
        if grep -q 'state = running' <<<"$output"; then
            /bin/sleep 3
            continue
        fi
        grep -q 'last exit code = 0' <<<"$output" && return 0
        echo "$label did not complete successfully" >&2
        return 1
    done
    echo "$label did not finish within 120 seconds" >&2
    return 1
}

verify_installed_files() {
    local source
    for source in "$STAGE"/scripts/dns/*; do
        /usr/bin/cmp -s "$source" "$LIBEXEC/dns/$(basename "$source")"
    done
    for source in "$STAGE"/scripts/monitoring/*.py; do
        /usr/bin/cmp -s "$source" "$LIBEXEC/monitoring/$(basename "$source")"
    done
    /usr/bin/cmp -s "$STAGE/scripts/maintenance/screenshare.sh" \
        /usr/local/sbin/screenshare
    /usr/bin/cmp -s "$STAGE/config/unbound/dns-warm-domains.txt" \
        "$SHARE/dns-warm-domains.txt"
    for label in "${LABELS[@]}"; do
        /usr/bin/cmp -s "$STAGE/launchd/$label.plist" \
            "/Library/LaunchDaemons/$label.plist"
    done
}

acceptance() {
    local output
    /opt/homebrew/opt/unbound/sbin/unbound-checkconf \
        /opt/homebrew/etc/unbound/unbound.conf >/dev/null

    output="$(/usr/bin/dig @127.0.0.1 +time=2 +tries=1 cloudflare.com A)"
    grep -q 'status: NOERROR' <<<"$output"
    grep -q 'flags:.* ad' <<<"$output"
    output="$(/usr/bin/dig @127.0.0.1 +time=2 +tries=1 google-analytics.com A)"
    grep -q 'status: NXDOMAIN' <<<"$output"
    grep -q 'flags:.* aa' <<<"$output"
    output="$(/usr/bin/dig @127.0.0.1 +time=2 +tries=1 dnssec-failed.org A)"
    grep -q 'status: SERVFAIL' <<<"$output"
    [ "$(/usr/bin/dig @127.0.0.1 +short mini.lan A)" = "192.168.0.100" ]

    /usr/bin/python3 -c 'import json,urllib.request; a=json.load(urllib.request.urlopen("http://127.0.0.1:9090/api/v1/targets"))["data"]["activeTargets"]; assert len(a)==8 and all(x.get("health")=="up" for x in a)'
    output="$(/usr/bin/curl -fsS http://127.0.0.1:9100/metrics)"
    grep -q '^node_textfile_scrape_error 0$' <<<"$output"

    for label in "${RUN_AT_LOAD[@]}"; do
        wait_for_success "$label"
    done
    verify_installed_files

    if /usr/bin/grep -Eq '/Users/localadmin/(monitoring|warmup)/.*\.(py|sh)|/opt/homebrew/etc/unbound/(update-blocklist|blocklist-tui)|/usr/local/sbin/unbound-refresh-hints' \
        /Library/LaunchDaemons/local.unbound.*.plist; then
        echo "an installed plist still references an obsolete executable" >&2
        return 1
    fi
}

restore_rollback() {
    trap - ERR
    set +e
    log "failure detected; restoring rollback bundle"
    for label in "${LABELS[@]}"; do
        /bin/launchctl bootout "system/$label" 2>/dev/null || true
    done
    for label in "${LABELS[@]}"; do
        /usr/bin/install -o root -g wheel -m 0644 \
            "$ROLLBACK/files/Library/LaunchDaemons/$label.plist" \
            "/Library/LaunchDaemons/$label.plist"
    done
    /usr/bin/install -o localadmin -g admin -m 0755 \
        "$ROLLBACK/files/opt/homebrew/etc/unbound/update-blocklist.sh" \
        /opt/homebrew/etc/unbound/update-blocklist.sh
    /usr/bin/install -o localadmin -g admin -m 0755 \
        "$ROLLBACK/files/opt/homebrew/etc/unbound/blocklist-tui.sh" \
        /opt/homebrew/etc/unbound/blocklist-tui.sh
    /usr/bin/install -o localadmin -g admin -m 0644 \
        "$ROLLBACK/files/opt/homebrew/etc/unbound/blocklist-source" \
        /opt/homebrew/etc/unbound/blocklist-source
    for source in "$ROLLBACK"/files/Users/localadmin/monitoring/*.py; do
        /usr/bin/install -o localadmin -g staff -m 0755 "$source" \
            "/Users/localadmin/monitoring/$(basename "$source")"
    done
    for source in "$ROLLBACK"/files/Users/localadmin/warmup/*.sh; do
        /usr/bin/install -o localadmin -g staff -m 0755 "$source" \
            "/Users/localadmin/warmup/$(basename "$source")"
    done
    /usr/bin/install -o localadmin -g staff -m 0644 \
        "$ROLLBACK/files/Users/localadmin/warmup/dns-warm-domains.txt" \
        /Users/localadmin/warmup/dns-warm-domains.txt
    for source in "$ROLLBACK"/files/usr/local/sbin/*; do
        /usr/bin/install -o root -g wheel -m 0755 "$source" \
            "/usr/local/sbin/$(basename "$source")"
    done
    /usr/bin/install -o root -g wheel -m 0644 \
        "$ROLLBACK/files/etc/newsyslog.d/unbound-monitoring.conf" \
        /etc/newsyslog.d/unbound-monitoring.conf
    /usr/bin/install -o localadmin -g admin -m 0644 \
        "$ROLLBACK/files/opt/homebrew/etc/grafana/provisioning/dashboards/unbound.yml" \
        /opt/homebrew/etc/grafana/provisioning/dashboards/unbound.yml
    if [ -e /Users/localadmin/warmup/dns-warm-domains.personal.txt ]; then
        /usr/sbin/chown localadmin:staff \
            /Users/localadmin/warmup/dns-warm-domains.personal.txt
        /bin/chmod 0644 /Users/localadmin/warmup/dns-warm-domains.personal.txt
    fi
    for label in "${LABELS[@]}"; do
        /bin/launchctl bootstrap system "/Library/LaunchDaemons/$label.plist"
    done
    /bin/rm -f /usr/local/sbin/screenshare
    /bin/rm -rf "$LIBEXEC"
    /bin/rm -f "$SHARE/dns-warm-domains.txt"
    /bin/rmdir "$SHARE" 2>/dev/null || true
    log "rollback restored; verify DNS and monitoring before further action"
}

cleanup_obsolete() {
    /bin/rm -f \
        /opt/homebrew/etc/unbound/update-blocklist.sh \
        /opt/homebrew/etc/unbound/blocklist-tui.sh \
        /Users/localadmin/monitoring/apple_content_cache_exporter.py \
        /Users/localadmin/monitoring/unbound_client_rpz_exporter.py \
        /Users/localadmin/monitoring/prometheus_alert_notifier.py \
        /Users/localadmin/monitoring/prometheus.rules.yml.pre-degraded-alert \
        /Users/localadmin/monitoring/prometheus.rules.yml.codex-stage \
        /Users/localadmin/monitoring/unbound-dns.json.codex-stage \
        /Users/localadmin/monitoring/unbound-dns.json.pre-degraded-alert \
        /Users/localadmin/warmup/build-warm-list.sh \
        /Users/localadmin/warmup/check-blocked.sh \
        /Users/localadmin/warmup/warm-at-boot.sh \
        /Users/localadmin/warmup/warm-unbound-cache.sh \
        /Users/localadmin/warmup/dns-warm-domains.txt \
        /usr/local/sbin/unbound-refresh-hints \
        /usr/local/sbin/screenshare-on \
        /usr/local/sbin/screenshare-off
    /usr/bin/find "$TEXTFILES" -maxdepth 1 -type f -name '*.prom.[0-9]*' \
        -exec /bin/rm -f {} +
}

log "validating staged commit and rollback bundle"
validate_stage
trap restore_rollback ERR
log "installing inactive target files"
install_targets
log "activating seven affected LaunchDaemons"
activate_plists
log "running acceptance checks"
acceptance
log "acceptance passed; removing obsolete executable copies"
cleanup_obsolete
trap - ERR
log "migration complete; rollback retained at $ROLLBACK"
