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
          python3 ~/monitoring/prometheus_alert_notifier.py --test-send
          python3 ~/monitoring/prometheus_alert_notifier.py --selftest
Needs:    no privileges. Reads the ntfy topic from ~/monitoring/ntfy.topic
          (one line, chmod 600 - the topic name is the only auth ntfy.sh has,
          so treat it like a password and keep it out of the repo). The topic
          is never printed anywhere, including error paths - diagnostics show
          a redacted form (first 4 chars).
State:    ~/monitoring/state/notified-alerts.json - fingerprints of alerts
          already announced, plus persisted delivery counters (__metrics__
          key). If it's ever lost/corrupted, still-firing alerts re-announce
          once and counters reset - increase()-based rules tolerate that.
Logs:     /opt/homebrew/var/log/unbound-alertnotify.log (launchd).
Exit:     0 = ok (including "nothing to send"); 1 = Prometheus unreachable,
          topic file missing, or an ntfy POST failed (state is NOT updated
          for failed sends, so they retry next run).
Prometheus down: after UNREACHABLE_THRESHOLD consecutive failed polls
          (~6 min at the 120s cadence), pushes "Prometheus unreachable"
          DIRECTLY to ntfy - Prometheus can't report its own death, but this
          script doesn't need it to. One push per outage (suppressed after
          the first); a RESOLVED push and a state reset when it comes back.
Metrics:  writes unbound-alertnotify.prom into node_exporter's textfile dir
          EVERY non-dry run (atomic replace):
            unbound_job_last_success_timestamp_seconds{task="alertnotify"}
              = last fully-successful run (Prometheus answered AND every push
              delivered) - the value persists across failed runs via the
              state file, so AlertNotifierStale genuinely means "phone pushes
              are dead", not merely "the script crashed". TYPE-only, no HELP:
              five other jobs share this family and node_exporter REJECTS
              samples whose HELP conflicts across textfiles (proven live
              2026-07-09 on the clientstats breadcrumb).
            unbound_ntfy_last_attempt_timestamp_seconds  (every run)
            unbound_ntfy_last_failure_timestamp_seconds  (persisted)
            unbound_ntfy_prometheus_query_success        (this run, 0/1)
            unbound_ntfy_firing_alerts                   (this poll's count)
            unbound_ntfy_suppressed_duplicates           (already-known firing)
            unbound_ntfy_notifications_sent_total        (persisted counter)
            unbound_ntfy_delivery_failures_total         (persisted counter)
            unbound_ntfy_state_errors_total              (persisted counter)
          Remaining circular gap, unchanged: a dead notifier still can't
          notify about itself; staleness/delivery-failure alerts show on the
          dashboard/alerts page only.

Priorities: severity=critical -> "urgent", severity=warning or unset ->
          "high" (buzzes), resolved -> "default".
"""
import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

PROMETHEUS_ALERTS_URL = "http://127.0.0.1:9090/api/v1/alerts"
NTFY_SERVER = "https://ntfy.sh"
HOME = os.path.expanduser("~")
TOPIC_FILE = os.path.join(HOME, "monitoring", "ntfy.topic")
STATE_FILE = os.path.join(HOME, "monitoring", "state", "notified-alerts.json")
METRICS_DIR = "/opt/homebrew/var/lib/node_exporter/textfile"
TIMEOUT = 10
# Reserved state-file keys. Cannot collide with alert fingerprints (those are
# "k=v|..." label joins).
UNREACHABLE_KEY = "__prometheus_unreachable__"
METRICS_KEY = "__metrics__"
RESERVED_KEYS = {UNREACHABLE_KEY, METRICS_KEY}
# Consecutive failed polls before pushing "Prometheus unreachable" directly
# (3 x 120s StartInterval ~= 6 min - same spirit as the rules' `for:` windows).
UNREACHABLE_THRESHOLD = 3

PRIORITY_BY_SEVERITY = {"critical": "urgent", "warning": "high"}
TOPIC_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class TopicError(Exception):
    pass


def redact_topic(topic):
    """Never print the topic (it's the only auth ntfy has). First 4 chars is
    enough to recognize which topic file was read without disclosing it."""
    if topic is None:
        return "(unreadable)"
    if not topic:
        return "(empty)"
    prefix = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "._-") else "?"
        for ch in topic[:4]
    )
    return f"{prefix}***({len(topic)} chars)"


def read_topic():
    try:
        with open(TOPIC_FILE) as fh:
            topic = fh.read().rstrip("\r\n")
    except OSError as exc:
        raise TopicError(f"cannot read ntfy topic file {TOPIC_FILE}: {exc}")
    if not TOPIC_RE.fullmatch(topic):
        raise TopicError(
            f"ntfy topic in {TOPIC_FILE} is invalid: {redact_topic(topic)} "
            "(allowed: 1-128 ASCII letters, digits, dot, underscore, dash)"
        )
    return topic


def fetch_firing_alerts():
    """Returns the list of firing alerts, or None if Prometheus is unreachable
    or answered garbage (the caller handles that case - it must NOT be a
    silent exit, because a dead Prometheus means ALL alerting is dark and this
    script is the only component positioned to say so)."""
    try:
        with urllib.request.urlopen(PROMETHEUS_ALERTS_URL, timeout=TIMEOUT) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"cannot query Prometheus alerts API: {exc}", file=sys.stderr)
        return None
    # Shape-validate before trusting: valid JSON that isn't the expected
    # {"data": {"alerts": [ {...}, ... ]}} must not traceback the poller.
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        print("malformed Prometheus alerts API response (unexpected shape)", file=sys.stderr)
        return None
    if payload.get("status") != "success":
        status = payload.get("status", "(missing)")
        print(f"Prometheus alerts API response status was not success: {status!r}",
              file=sys.stderr)
        return None
    alerts = payload["data"].get("alerts")
    if not isinstance(alerts, list):
        print("malformed Prometheus alerts API response (alerts not a list)", file=sys.stderr)
        return None
    alerts = [a for a in alerts
              if isinstance(a, dict) and isinstance(a.get("labels"), dict)]
    # 'pending' alerts are still inside their `for:` window - not actionable yet.
    return [a for a in alerts if a.get("state") == "firing"]


def fingerprint(alert):
    labels = alert.get("labels", {})
    return "|".join(f"{k}={labels[k]}" for k in sorted(labels))


def load_state():
    """Returns (state_dict, had_error). Corrupt/unreadable/wrong-type state is
    an empty dict plus a flag (counted in unbound_ntfy_state_errors_total) -
    still-firing alerts then re-announce once, same as after a lost file."""
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return {}, False  # first run - expected, not an error
    except (OSError, json.JSONDecodeError):
        return {}, True
    if not isinstance(state, dict):
        return {}, True
    return state, False


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
    except http.client.InvalidURL:
        print(f"ntfy push failed for {title!r}: invalid topic/URL configuration "
              f"({redact_topic(topic)})", file=sys.stderr)
        return False
    except (urllib.error.URLError, OSError) as exc:
        # NB: exc messages from urllib carry no URL, so no topic leak here.
        print(f"ntfy push failed for {title!r}: {exc}", file=sys.stderr)
        return False


def parse_active_at(raw):
    """Prometheus activeAt: RFC3339 with 'Z' and up-to-nanosecond fractions,
    neither of which Python 3.9's fromisoformat accepts. Returns epoch or None."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.replace("Z", "+00:00")
    # clamp fractional seconds to 6 digits (nanoseconds -> microseconds)
    if "." in text:
        head, _, rest = text.partition(".")
        frac = ""
        while rest and rest[0].isdigit():
            frac += rest[0]
            rest = rest[1:]
        text = f"{head}.{frac[:6]}{rest}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def describe(alert):
    """Returns (name, message, priority) for a firing alert. Enough context to
    know what broke without opening Grafana: summary, description (when it
    adds anything), non-noise labels, how long it's been active, and the
    offending value."""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {}) if isinstance(alert.get("annotations"), dict) else {}
    name = labels.get("alertname", "UnknownAlert")
    priority = PRIORITY_BY_SEVERITY.get(labels.get("severity", ""), "high")

    parts = [annotations.get("summary") or "(no summary annotation)"]
    description = annotations.get("description", "")
    if description and description != parts[0]:
        parts.append(description)

    extra = ", ".join(f"{k}={v}" for k, v in sorted(labels.items())
                      if k not in ("alertname", "__name__"))
    if extra:
        parts.append(extra)

    active_epoch = parse_active_at(alert.get("activeAt"))
    if active_epoch is not None and active_epoch <= time.time():
        parts.append(f"active for {format_duration(time.time() - active_epoch)}")

    value = alert.get("value")
    if isinstance(value, str) and value:
        parts.append(f"value: {value}")

    return name, "\n".join(parts), priority


def default_metrics():
    return {"last_success": 0, "last_failure": 0,
            "sent_total": 0, "fail_total": 0, "state_errors_total": 0}


def get_metrics(state, state_had_error):
    m = state.get(METRICS_KEY)
    metrics = default_metrics()
    if isinstance(m, dict):
        for key in metrics:
            if isinstance(m.get(key), (int, float)):
                metrics[key] = m[key]
    if state_had_error:
        metrics["state_errors_total"] += 1
    return metrics


def write_metrics(metrics, query_ok, firing_count, suppressed, now=None):
    """Atomic full rewrite of unbound-alertnotify.prom, every non-dry run.
    The shared breadcrumb family is TYPE-only (see module docstring for the
    HELP-conflict trap); the unbound_ntfy_* families are unique to this file
    and get full HELP/TYPE. Non-fatal on error: notification delivery matters
    more than the metrics."""
    if now is None:
        now = int(time.time())
    lines = []
    if metrics["last_success"] > 0:
        lines.append("# TYPE unbound_job_last_success_timestamp_seconds gauge")
        lines.append(f'unbound_job_last_success_timestamp_seconds{{task="alertnotify"}} {int(metrics["last_success"])}')
    lines.append("# HELP unbound_ntfy_last_attempt_timestamp_seconds Unix timestamp of the notifier's most recent run, successful or not.")
    lines.append("# TYPE unbound_ntfy_last_attempt_timestamp_seconds gauge")
    lines.append(f"unbound_ntfy_last_attempt_timestamp_seconds {now}")
    if metrics["last_failure"] > 0:
        lines.append("# HELP unbound_ntfy_last_failure_timestamp_seconds Unix timestamp of the most recent run with a failed push or unreachable Prometheus.")
        lines.append("# TYPE unbound_ntfy_last_failure_timestamp_seconds gauge")
        lines.append(f'unbound_ntfy_last_failure_timestamp_seconds {int(metrics["last_failure"])}')
    lines.append("# HELP unbound_ntfy_prometheus_query_success 1 if this run's poll of the Prometheus alerts API succeeded and parsed, else 0.")
    lines.append("# TYPE unbound_ntfy_prometheus_query_success gauge")
    lines.append(f"unbound_ntfy_prometheus_query_success {1 if query_ok else 0}")
    if query_ok:
        lines.append("# HELP unbound_ntfy_firing_alerts Alerts firing in this run's poll (before dedupe).")
        lines.append("# TYPE unbound_ntfy_firing_alerts gauge")
        lines.append(f"unbound_ntfy_firing_alerts {firing_count}")
        lines.append("# HELP unbound_ntfy_suppressed_duplicates Firing alerts in this run's poll already announced earlier (deduped, not re-sent).")
        lines.append("# TYPE unbound_ntfy_suppressed_duplicates gauge")
        lines.append(f"unbound_ntfy_suppressed_duplicates {suppressed}")
    lines.append("# HELP unbound_ntfy_notifications_sent_total ntfy pushes delivered (persisted in the notifier state file; resets only if that file is lost).")
    lines.append("# TYPE unbound_ntfy_notifications_sent_total counter")
    lines.append(f'unbound_ntfy_notifications_sent_total {int(metrics["sent_total"])}')
    lines.append("# HELP unbound_ntfy_delivery_failures_total ntfy pushes that failed (persisted in the notifier state file; resets only if that file is lost).")
    lines.append("# TYPE unbound_ntfy_delivery_failures_total counter")
    lines.append(f'unbound_ntfy_delivery_failures_total {int(metrics["fail_total"])}')
    lines.append("# HELP unbound_ntfy_state_errors_total Runs that found the state file corrupt or unreadable (persisted counter).")
    lines.append("# TYPE unbound_ntfy_state_errors_total counter")
    lines.append(f'unbound_ntfy_state_errors_total {int(metrics["state_errors_total"])}')
    try:
        os.makedirs(METRICS_DIR, exist_ok=True)
        path = os.path.join(METRICS_DIR, "unbound-alertnotify.prom")
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as fh:
            fh.write("\n".join(lines))
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"warning: could not write metrics: {exc}", file=sys.stderr)


def test_send(topic):
    """Manual end-to-end channel test: real POST to the real topic, redacted
    output, nonzero exit on failure. Does not touch state or metrics."""
    print(f"test-send to topic {redact_topic(topic)} via {NTFY_SERVER} ...")
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{topic}",
        data=(f"Manual test from the mini's alert notifier "
              f"({time.strftime('%Y-%m-%d %H:%M:%S')}). "
              "If you can read this, the ntfy channel works end to end.").encode(),
        headers={"Title": "TEST: mini alert notifier", "Priority": "default",
                 "Tags": "white_check_mark"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            ok = 200 <= resp.status < 300
            print(f"HTTP {resp.status} -> {'delivered' if ok else 'NOT delivered'}")
            return 0 if ok else 1
    except http.client.InvalidURL:
        print(f"test-send failed: invalid topic/URL configuration "
              f"({redact_topic(topic)})", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"test-send failed: {exc}", file=sys.stderr)
        return 1


def selftest():
    """Offline checks of the pure helpers plus the malformed-input handling.
    Returns True on success. No network, no file writes outside a temp dir."""
    import tempfile
    ok = True

    def fail(msg):
        nonlocal ok
        print(f"FAIL {msg}", file=sys.stderr)
        ok = False

    if fingerprint({"labels": {"b": "2", "a": "1"}}) != "a=1|b=2":
        fail("fingerprint ordering")
    if redact_topic("fixture-topic-value00") != "fixt***(21 chars)":
        fail(f"redact_topic -> {redact_topic('fixture-topic-value00')!r}")
    if "secretsecret" in redact_topic("fixture-topic-value00"):
        fail("redact_topic leaks")

    global TOPIC_FILE
    orig_topic = TOPIC_FILE
    valid_topics = ("abcXYZ012._-", "topic.under_score-dash", "A")
    invalid_topics = (
        "", "abc\ndef", "abc?def", "abc#def", "abc/def", r"abc\def",
        "abc def", "abc%def", "abc:def", "ábcd", "abc\x1fdef",
    )
    with tempfile.TemporaryDirectory() as td:
        TOPIC_FILE = os.path.join(td, "topic")
        for topic in valid_topics:
            with open(TOPIC_FILE, "w") as fh:
                fh.write(topic + "\n")
            if read_topic() != topic:
                fail("read_topic valid topic")
        for topic in invalid_topics:
            with open(TOPIC_FILE, "w") as fh:
                fh.write(topic)
            try:
                read_topic()
                fail("read_topic accepted invalid topic")
            except TopicError as exc:
                if topic and topic in str(exc):
                    fail("read_topic leaked invalid topic")
    TOPIC_FILE = orig_topic

    if parse_active_at("2026-07-09T21:00:00.123456789Z") is None:
        fail("parse_active_at nanoseconds")
    if parse_active_at("2026-07-09T21:00:00Z") is None:
        fail("parse_active_at no fraction")
    if parse_active_at("garbage") is not None or parse_active_at(None) is not None:
        fail("parse_active_at garbage should be None")

    if format_duration(45) != "45s" or format_duration(600) != "10m" or format_duration(7260) != "2h01m":
        fail("format_duration")

    name, message, priority = describe({
        "labels": {"alertname": "DiskAlmostFull", "severity": "critical", "mountpoint": "/"},
        "annotations": {"summary": "disk over 90% full", "description": "act now"},
        "activeAt": "2026-07-09T00:00:00Z", "value": "0.05",
    })
    if name != "DiskAlmostFull" or priority != "urgent":
        fail(f"describe name/priority -> {name}, {priority}")
    for expect in ("disk over 90% full", "act now", "mountpoint=/", "value: 0.05"):
        if expect not in message:
            fail(f"describe message missing {expect!r}")
    _, _, priority = describe({"labels": {"alertname": "X"}})
    if priority != "high":
        fail("describe default priority should be high")

    # malformed-state handling (valid JSON, wrong type)
    global STATE_FILE
    orig_state = STATE_FILE
    with tempfile.TemporaryDirectory() as td:
        STATE_FILE = os.path.join(td, "state.json")
        state, err = load_state()
        if state != {} or err:
            fail("load_state missing file should be ({}, False)")
        with open(STATE_FILE, "w") as fh:
            fh.write("[1, 2, 3]")
        state, err = load_state()
        if state != {} or not err:
            fail("load_state non-dict JSON should be ({}, True)")
        with open(STATE_FILE, "w") as fh:
            fh.write("{not json")
        state, err = load_state()
        if state != {} or not err:
            fail("load_state corrupt JSON should be ({}, True)")
    STATE_FILE = orig_state

    m = get_metrics({METRICS_KEY: {"sent_total": 7, "last_success": "bogus"}}, True)
    if m["sent_total"] != 7 or m["last_success"] != 0 or m["state_errors_total"] != 1:
        fail(f"get_metrics merge -> {m}")

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return json.dumps(self.payload).encode()

    orig_urlopen = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda *_args, **_kwargs: FakeResponse(
            {"status": "success", "data": {"alerts": []}}
        )
        if fetch_firing_alerts() != []:
            fail("fetch_firing_alerts valid empty response")
        urllib.request.urlopen = lambda *_args, **_kwargs: FakeResponse(
            {"status": "error", "data": {"alerts": []}}
        )
        if fetch_firing_alerts() is not None:
            fail("fetch_firing_alerts must reject status != success")
        urllib.request.urlopen = lambda *_args, **_kwargs: FakeResponse(
            {"status": "success", "data": {"alerts": {}}}
        )
        if fetch_firing_alerts() is not None:
            fail("fetch_firing_alerts must reject malformed alerts")

        def raise_invalid_url(*_args, **_kwargs):
            raise http.client.InvalidURL(
                "URL can't contain control characters. '/fixture-hidden-topic\\n'"
            )
        urllib.request.urlopen = raise_invalid_url
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            delivered = push_ntfy("fixture-hidden-topic", "TEST", "message", "high", False)
        if delivered:
            fail("push_ntfy InvalidURL should fail")
        if "fixture-hidden-topic" in err.getvalue() or "/fixture-hidden-topic" in err.getvalue():
            fail("push_ntfy InvalidURL leaked topic")
    finally:
        urllib.request.urlopen = orig_urlopen

    global METRICS_DIR
    orig_metrics_dir = METRICS_DIR
    with tempfile.TemporaryDirectory() as td:
        METRICS_DIR = td
        write_metrics({"last_success": 123, "last_failure": 456,
                       "sent_total": 2, "fail_total": 3,
                       "state_errors_total": 1},
                      query_ok=True, firing_count=4, suppressed=5, now=789)
        rendered = open(os.path.join(td, "unbound-alertnotify.prom")).read()
        for expect in (
            "# TYPE unbound_job_last_success_timestamp_seconds gauge",
            'unbound_job_last_success_timestamp_seconds{task="alertnotify"} 123',
            "# HELP unbound_ntfy_delivery_failures_total",
            "# TYPE unbound_ntfy_delivery_failures_total counter",
            "unbound_ntfy_prometheus_query_success 1",
        ):
            if expect not in rendered:
                fail(f"write_metrics missing {expect!r}")
        if "# HELP unbound_job_last_success_timestamp_seconds" in rendered:
            fail("write_metrics must not emit HELP for shared breadcrumb")
        if "fixture-hidden-topic" in rendered:
            fail("write_metrics leaked topic material")
    METRICS_DIR = orig_metrics_dir

    if ok:
        print("selftest: OK")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be pushed; do not POST or update state")
    parser.add_argument("--test-send", action="store_true",
                        help="send one clearly-labeled test message to the real topic and exit")
    parser.add_argument("--selftest", action="store_true",
                        help="run offline self-tests and exit")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)
    if args.test_send:
        try:
            sys.exit(test_send(read_topic()))
        except TopicError as exc:
            print(exc, file=sys.stderr)
            sys.exit(1)

    state, state_had_error = load_state()
    metrics = get_metrics(state, state_had_error)
    if state_had_error:
        print("warning: state file was corrupt/unreadable - starting fresh "
              "(still-firing alerts will re-announce once)", file=sys.stderr)
    now = int(time.time())
    if args.dry_run:
        topic = "(dry-run)"
    else:
        try:
            topic = read_topic()
        except TopicError as exc:
            print(exc, file=sys.stderr)
            metrics["fail_total"] += 1
            metrics["last_failure"] = now
            state[METRICS_KEY] = metrics
            save_state(state)
            write_metrics(metrics, query_ok=False, firing_count=0,
                          suppressed=0, now=now)
            sys.exit(1)
    firing = fetch_firing_alerts()

    if firing is None:
        # Prometheus unreachable: every rule-based alert is dark. Track
        # consecutive failures in the state file; past the threshold, push
        # ONCE directly (no Prometheus needed for that), then stay quiet
        # until it recovers.
        entry = state.get(UNREACHABLE_KEY) or {"failures": 0, "notified": False}
        if not isinstance(entry, dict):
            entry = {"failures": 0, "notified": False}
        entry["failures"] = entry.get("failures", 0) + 1
        if entry["failures"] >= UNREACHABLE_THRESHOLD and not entry.get("notified"):
            if push_ntfy(topic, "FIRING: PrometheusUnreachable",
                         f"alert polling has failed {entry['failures']} consecutive times "
                         f"(~{entry['failures'] * 2} min) - Prometheus is down or unreachable; "
                         "ALL rule-based alerting is dark until this recovers",
                         "high", args.dry_run):
                entry["notified"] = True
                metrics["sent_total"] += 1
            else:
                metrics["fail_total"] += 1
        state[UNREACHABLE_KEY] = entry
        metrics["last_failure"] = now
        if not args.dry_run:
            state[METRICS_KEY] = metrics
            save_state(state)
            write_metrics(metrics, query_ok=False, firing_count=0, suppressed=0, now=now)
        sys.exit(1)

    failures = 0
    sent = 0

    # Prometheus is back: clear the unreachable tracker. If we had announced
    # the outage, announce the recovery too; a failed recovery push keeps the
    # tracker so it retries next run.
    prev_outage = state.pop(UNREACHABLE_KEY, None)
    if isinstance(prev_outage, dict) and prev_outage.get("notified"):
        if push_ntfy(topic, "RESOLVED: PrometheusUnreachable",
                     "Prometheus is reachable again", "default", args.dry_run):
            sent += 1
        else:
            failures += 1
            state[UNREACHABLE_KEY] = prev_outage

    current = {fingerprint(a): a for a in firing}
    known = set(state.keys()) - RESERVED_KEYS
    now_firing = set(current.keys())
    suppressed = len(now_firing & known)

    for fp in sorted(now_firing - known):
        name, message, priority = describe(current[fp])
        if push_ntfy(topic, f"FIRING: {name}", message, priority, args.dry_run):
            state[fp] = {"alertname": name, "since": now}
            sent += 1
        else:
            failures += 1

    for fp in sorted(known - now_firing):
        entry = state[fp]
        name = entry.get("alertname", "UnknownAlert") if isinstance(entry, dict) else "UnknownAlert"
        if push_ntfy(topic, f"RESOLVED: {name}", "no longer firing", "default", args.dry_run):
            del state[fp]
            sent += 1
        else:
            failures += 1

    metrics["sent_total"] += sent
    if failures:
        metrics["fail_total"] += failures
        metrics["last_failure"] = now
    else:
        # "Last fully-successful run": Prometheus answered AND every push was
        # delivered. Advancing this on failed-delivery runs would keep
        # AlertNotifierStale green while the phone channel is dead.
        metrics["last_success"] = now

    if not args.dry_run:
        state[METRICS_KEY] = metrics
        save_state(state)
        write_metrics(metrics, query_ok=True,
                      firing_count=len(now_firing), suppressed=suppressed, now=now)

    if args.dry_run:
        print(f"[dry-run] firing now: {len(now_firing)}, previously known: {len(known)}")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
