#!/usr/bin/env python3
"""
prometheus_alert_notifier.py - push Prometheus alerts to a phone via ntfy.

Why this instead of Alertmanager: at 16 rules on one box, Alertmanager plus a
webhook->ntfy bridge is two more daemons and a config surface for grouping/
silencing features this setup doesn't need. This is one stdlib-only script:
poll /api/v1/alerts, push NEW firing alerts (and their resolutions) to an
ntfy topic, remember what was already sent.

Run by:   local.unbound.alertnotify LaunchDaemon, every 120s, as localadmin.
Manual:   python3 ~/monitoring/prometheus_alert_notifier.py --dry-run
Needs:    no privileges. Reads the ntfy topic from ~/monitoring/ntfy.topic
          (one line, chmod 600 - the topic name is the only auth ntfy.sh has,
          so treat it like a password and keep it out of the repo).
State:    ~/monitoring/state/notified-alerts.json - fingerprints of alerts
          already announced. Lost on reboot -> still-firing alerts re-announce
          once after boot, which is a feature, not a bug.
Logs:     /opt/homebrew/var/log/unbound-alertnotify.log (launchd).
Exit:     0 = ok (including "nothing to send"); 1 = Prometheus unreachable,
          topic file missing, or an ntfy POST failed (state is NOT updated
          for failed sends, so they retry next run).
Prometheus down: after UNREACHABLE_THRESHOLD consecutive failed polls
          (~6 min at the 120s cadence), pushes "Prometheus unreachable"
          DIRECTLY to ntfy - Prometheus can't report its own death, but this
          script doesn't need it to. One push per outage (suppressed after
          the first); a RESOLVED push and a state reset when it comes back.
Breadcrumb: writes task="alertnotify" into node_exporter's textfile dir ONLY
          when the run fully succeeded (Prometheus reachable AND every push
          delivered) - so AlertNotifierStale genuinely means "phone pushes
          are dead", not merely "the script crashed". Remaining circular gap:
          a dead notifier still can't notify about itself; that staleness
          alert shows on the dashboard/alerts page only.

Priorities: firing -> ntfy "high" (buzzes), resolved -> "default".
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PROMETHEUS_ALERTS_URL = "http://127.0.0.1:9090/api/v1/alerts"
NTFY_SERVER = "https://ntfy.sh"
HOME = os.path.expanduser("~")
TOPIC_FILE = os.path.join(HOME, "monitoring", "ntfy.topic")
STATE_FILE = os.path.join(HOME, "monitoring", "state", "notified-alerts.json")
METRICS_DIR = "/opt/homebrew/var/lib/node_exporter/textfile"
TIMEOUT = 10
# Reserved state-file key for the Prometheus-unreachable tracker. Cannot
# collide with alert fingerprints (those are "k=v|..." label joins).
UNREACHABLE_KEY = "__prometheus_unreachable__"
# Consecutive failed polls before pushing "Prometheus unreachable" directly
# (3 x 120s StartInterval ~= 6 min - same spirit as the rules' `for:` windows).
UNREACHABLE_THRESHOLD = 3


def read_topic():
    try:
        with open(TOPIC_FILE) as fh:
            topic = fh.read().strip()
    except OSError as exc:
        print(f"cannot read ntfy topic file {TOPIC_FILE}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not topic or "/" in topic or " " in topic:
        print(f"ntfy topic in {TOPIC_FILE} looks invalid: {topic!r}", file=sys.stderr)
        sys.exit(1)
    return topic


def fetch_firing_alerts():
    """Returns the list of firing alerts, or None if Prometheus is unreachable
    (the caller handles that case - it must NOT be a silent exit, because a
    dead Prometheus means ALL alerting is dark and this script is the only
    component positioned to say so)."""
    try:
        with urllib.request.urlopen(PROMETHEUS_ALERTS_URL, timeout=TIMEOUT) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"cannot query Prometheus alerts API: {exc}", file=sys.stderr)
        return None
    alerts = payload.get("data", {}).get("alerts", [])
    # 'pending' alerts are still inside their `for:` window - not actionable yet.
    return [a for a in alerts if a.get("state") == "firing"]


def fingerprint(alert):
    labels = alert.get("labels", {})
    return "|".join(f"{k}={labels[k]}" for k in sorted(labels))


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = f"{STATE_FILE}.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_FILE)


def push_ntfy(topic, title, message, priority, dry_run):
    if dry_run:
        print(f"[dry-run] would push: [{priority}] {title}: {message}")
        return True
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{topic}",
        data=message.encode(),
        headers={"Title": title, "Priority": priority, "Tags": "rotating_light"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        print(f"ntfy push failed for {title!r}: {exc}", file=sys.stderr)
        return False


def describe(alert):
    labels = alert.get("labels", {})
    name = labels.get("alertname", "UnknownAlert")
    summary = alert.get("annotations", {}).get("summary", "")
    extra = ", ".join(f"{k}={v}" for k, v in sorted(labels.items())
                      if k not in ("alertname", "__name__"))
    message = summary or "(no summary annotation)"
    if extra:
        message += f"\n{extra}"
    return name, message


def write_breadcrumb():
    try:
        os.makedirs(METRICS_DIR, exist_ok=True)
        path = os.path.join(METRICS_DIR, "unbound-alertnotify.prom")
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as fh:
            fh.write("# TYPE unbound_job_last_success_timestamp_seconds gauge\n")
            fh.write(f'unbound_job_last_success_timestamp_seconds{{task="alertnotify"}} {int(time.time())}\n')
        os.replace(tmp, path)
    except OSError as exc:
        # Non-fatal: notification delivery matters more than the breadcrumb.
        print(f"warning: could not write breadcrumb: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be pushed; do not POST or update state")
    args = parser.parse_args()

    topic = read_topic() if not args.dry_run else "(dry-run)"
    firing = fetch_firing_alerts()
    state = load_state()

    if firing is None:
        # Prometheus unreachable: every rule-based alert is dark. Track
        # consecutive failures in the state file; past the threshold, push
        # ONCE directly (no Prometheus needed for that), then stay quiet
        # until it recovers.
        entry = state.get(UNREACHABLE_KEY) or {"failures": 0, "notified": False}
        entry["failures"] += 1
        if entry["failures"] >= UNREACHABLE_THRESHOLD and not entry["notified"]:
            if push_ntfy(topic, "FIRING: PrometheusUnreachable",
                         f"alert polling has failed {entry['failures']} consecutive times "
                         f"(~{entry['failures'] * 2} min) - Prometheus is down or unreachable; "
                         "ALL rule-based alerting is dark until this recovers",
                         "high", args.dry_run):
                entry["notified"] = True
        state[UNREACHABLE_KEY] = entry
        if not args.dry_run:
            save_state(state)
        sys.exit(1)

    failures = 0

    # Prometheus is back: clear the unreachable tracker. If we had announced
    # the outage, announce the recovery too; a failed recovery push keeps the
    # tracker so it retries next run.
    prev_outage = state.pop(UNREACHABLE_KEY, None)
    if prev_outage and prev_outage.get("notified"):
        if not push_ntfy(topic, "RESOLVED: PrometheusUnreachable",
                         "Prometheus is reachable again", "default", args.dry_run):
            failures += 1
            state[UNREACHABLE_KEY] = prev_outage

    current = {fingerprint(a): a for a in firing}
    known = set(state.keys()) - {UNREACHABLE_KEY}
    now_firing = set(current.keys())

    for fp in sorted(now_firing - known):
        name, message = describe(current[fp])
        if push_ntfy(topic, f"FIRING: {name}", message, "high", args.dry_run):
            state[fp] = {"alertname": name, "since": int(time.time())}
        else:
            failures += 1

    for fp in sorted(known - now_firing):
        name = state[fp].get("alertname", "UnknownAlert")
        if push_ntfy(topic, f"RESOLVED: {name}", "no longer firing", "default", args.dry_run):
            del state[fp]
        else:
            failures += 1

    if not args.dry_run:
        save_state(state)
        # Breadcrumb = "last fully-successful run": Prometheus answered AND
        # every push was delivered. Writing it on failed-delivery runs would
        # keep AlertNotifierStale green while the phone channel is dead.
        if failures == 0:
            write_breadcrumb()

    if args.dry_run:
        print(f"[dry-run] firing now: {len(now_firing)}, previously known: {len(known)}")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
