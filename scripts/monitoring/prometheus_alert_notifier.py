#!/usr/bin/env python3
"""Poll Prometheus alert state and publish safe transitions through ntfy.

The notifier is deliberately small and stdlib-only.  Prometheus remains the
source of alert truth; this process adds transition detection, durable local
deduplication, bounded HTTP retries, and privacy filtering.  It does not claim
phone receipt: an HTTP 2xx proves only that the ntfy server accepted a request.

Run by ``local.unbound.alertnotify`` every 120 seconds as ``localadmin``.
``--selftest`` is offline. ``--test-send`` is the only mode that intentionally
publishes a real test notification.
"""

import argparse
import contextlib
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import socket
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:  # macOS runtime
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows repository tests
    fcntl = None

try:  # Windows repository tests
    import msvcrt
except ImportError:  # pragma: no cover - unavailable on macOS
    msvcrt = None


PROMETHEUS_BASE_URL = "http://127.0.0.1:9090"
PROMETHEUS_ALERTS_URL = f"{PROMETHEUS_BASE_URL}/api/v1/alerts"
PROMETHEUS_RULES_URL = f"{PROMETHEUS_BASE_URL}/api/v1/rules?type=alert"
PROMETHEUS_RUNTIME_URL = f"{PROMETHEUS_BASE_URL}/api/v1/status/runtimeinfo"
NTFY_SERVER = "https://ntfy.sh"
HOME = os.path.expanduser("~")
TOPIC_FILE = os.path.join(HOME, "monitoring", "ntfy.topic")
STATE_DIR = os.path.join(HOME, "monitoring", "state")
STATE_FILE = os.path.join(STATE_DIR, "notified-alerts.json")
LOCK_FILE = os.path.join(STATE_DIR, "notified-alerts.lock")
METRICS_DIR = "/opt/homebrew/var/lib/node_exporter/textfile"

TIMEOUT = 10
UNREACHABLE_THRESHOLD = 3
RULE_EVALUATION_MAX_AGE = 60
PROMETHEUS_RESTART_RESOLUTION_GRACE = 600
RESOLUTION_CONFIRM_POLLS = 2
MAX_EVENTS_PER_RUN = 10
MAX_DELIVERY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (1, 2)
MAX_INLINE_RETRY_AFTER = 5
MAX_RETRY_AFTER = 3600
MAX_STATE_BYTES = 1024 * 1024

SCHEMA_VERSION = 2
SCHEMA_KEY = "__schema_version__"
META_KEY = "__meta__"
METRICS_KEY = "__metrics__"
UNREACHABLE_KEY = "__prometheus_unreachable__"
ALERT_KEY_PREFIX = "alert:"
RESERVED_KEYS = {SCHEMA_KEY, META_KEY, METRICS_KEY, UNREACHABLE_KEY}

PRIORITY_BY_SEVERITY = {"critical": "urgent", "warning": "high"}
TOPIC_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
SEQUENCE_RE = re.compile(r"^unbound-alert-[0-9a-f]{40}$")
NOTIFICATION_LABEL_ALLOWLIST = (
    "job", "exported_job", "instance", "mountpoint", "task", "file",
    "rule_group", "fstype",
)
ALERT_ENTRY_STATUSES = {
    "pending_firing", "firing_notified", "observed_baseline",
    "blocked_firing", "pending_resolved", "blocked_resolution",
}

# Resolution blockers are derived from the current rule expressions.  They
# prevent an upstream collection failure from turning a dependent alert's
# deliberately empty expression into a false RESOLVED notification.
GLOBAL_RESOLUTION_BLOCKERS = {
    "PrometheusTargetInventoryMismatch",
    "PrometheusConfigReloadFailed",
    "PrometheusRuleEvaluationFailures",
}
NODE_DEPENDENT_ALERTS = {
    "BlocklistUpdateStale", "BlocklistBreadcrumbAbsent", "WarmlistMineStale",
    "WarmlistBreadcrumbAbsent", "RootHintsRefreshStale",
    "RootHintsBreadcrumbAbsent", "WarmbootDidNotRun",
    "WarmbootBreadcrumbAbsent", "BlocklistSuspiciouslySmall",
    "BlocklistMinimumMetricAbsent", "ContentCacheExporterStale",
    "ContentCacheTextfileMissing", "TextfileScrapeError", "AlertNotifierStale",
    "AlertNotifierBreadcrumbAbsent", "ClientStatsStale",
    "ClientStatsBreadcrumbAbsent", "NtfyDeliveryFailures",
    "TimeMachineMonitoringUnhealthy", "TimeMachineBackupStaleOrMissing",
    "DiskAlmostFull", "RootDiskFreePredictedLow", "RootDiskFreePredictedCritical",
}
BLACKBOX_DEPENDENT_ALERTS = {
    "UpstreamDNSPathDown", "UpstreamDNSPathDegraded", "ResolverNotAnswering",
    "RPZCanaryNotBlocking", "GrafanaDown",
}
UNBOUND_DEPENDENT_ALERTS = {
    "UnboundDown", "UnboundRequestListJammed", "UnboundCacheHitRateLow",
}
PAIR_RESOLUTION_BLOCKERS = {
    "BlocklistUpdateStale": {"BlocklistBreadcrumbAbsent"},
    "WarmlistMineStale": {"WarmlistBreadcrumbAbsent"},
    "RootHintsRefreshStale": {"RootHintsBreadcrumbAbsent"},
    "WarmbootDidNotRun": {"WarmbootBreadcrumbAbsent"},
    "BlocklistSuspiciouslySmall": {"BlocklistMinimumMetricAbsent"},
    "ContentCacheExporterStale": {"ContentCacheTextfileMissing"},
    "AlertNotifierStale": {"AlertNotifierBreadcrumbAbsent"},
    "ClientStatsStale": {"ClientStatsBreadcrumbAbsent"},
    "NtfyDeliveryFailures": {"AlertNotifierBreadcrumbAbsent"},
    "TimeMachineBackupStaleOrMissing": {"TimeMachineMonitoringUnhealthy"},
}


class TopicError(Exception):
    """The runtime-only topic file is absent, unsafe, or invalid."""


class StateError(Exception):
    """Durable notifier state could not be read or written safely."""


class OverlapError(Exception):
    """Another notifier process currently owns the run lock."""


@dataclass
class AlertSnapshot:
    firing: list
    pending: list
    invalid_alerts: int
    query_ok: bool
    evidence_fresh: bool
    process_uptime: float
    reason: str = ""

    @property
    def state_complete(self):
        return self.query_ok and self.evidence_fresh and self.invalid_alerts == 0

    @property
    def resolutions_enabled(self):
        return (
            self.state_complete
            and self.process_uptime >= PROMETHEUS_RESTART_RESOLUTION_GRACE
        )

    @property
    def active_alert_names(self):
        names = set()
        for alert in self.firing + self.pending:
            labels = alert.get("labels", {})
            name = labels.get("alertname")
            if isinstance(name, str):
                names.add(name)
        return names


@dataclass
class DeliveryResult:
    accepted: bool
    permanent: bool = False
    outcome_unknown: bool = False
    attempts: int = 0
    retry_after: int = 0
    status_code: int = 0

    def __bool__(self):
        return self.accepted


def redact_topic(topic):
    """Return no topic characters; even a prefix is credential material."""
    if topic is None:
        return "(unreadable topic)"
    return f"(redacted topic, {len(topic)} chars)"


def _regular_owned_file(path, *, repair_mode=False):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise StateError("required runtime file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StateError("runtime path is not a regular file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise StateError("runtime file owner is not the notifier user")
    if repair_mode and stat.S_IMODE(info.st_mode) != 0o600:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise StateError("could not secure runtime file mode") from exc
        info = os.lstat(path)
    return info


def _ensure_private_dir(path):
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise StateError("state path is not a private directory")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise StateError("state directory owner is not the notifier user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700)
    except OSError as exc:
        raise StateError("could not create or secure state directory") from exc


def read_topic():
    try:
        info = os.lstat(TOPIC_FILE)
    except OSError as exc:
        raise TopicError("cannot access required ntfy topic file") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise TopicError("ntfy topic path is not a regular file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise TopicError("ntfy topic file is not owned by the notifier user")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise TopicError("ntfy topic file permissions are too broad")
    try:
        with open(TOPIC_FILE, encoding="utf-8") as handle:
            topic = handle.read().rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise TopicError("cannot read required ntfy topic file") from exc
    if not TOPIC_RE.fullmatch(topic):
        raise TopicError(
            f"ntfy topic is invalid: {redact_topic(topic)}; expected 1-128 safe ASCII characters"
        )
    return topic


def parse_rfc3339(raw):
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.replace("Z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        fraction = ""
        while rest and rest[0].isdigit():
            fraction += rest[0]
            rest = rest[1:]
        text = f"{head}.{fraction[:6]}{rest}"
    try:
        value = datetime.fromisoformat(text)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (ValueError, OverflowError):
        return None


parse_active_at = parse_rfc3339  # compatibility name used by existing tests


def _fetch_json(url):
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            payload = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        print(f"Prometheus API request failed ({type(exc).__name__})", file=sys.stderr)
        return None
    if not isinstance(payload, dict) or payload.get("status") != "success":
        print("Prometheus API returned an unsuccessful or malformed envelope", file=sys.stderr)
        return None
    return payload.get("data")


def _parse_alerts(data):
    if not isinstance(data, dict) or not isinstance(data.get("alerts"), list):
        return None, None, 0
    firing = []
    pending = []
    invalid = 0
    for alert in data["alerts"]:
        if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict):
            invalid += 1
            continue
        labels = alert["labels"]
        if (not labels or not all(isinstance(k, str) and isinstance(v, str)
                                  for k, v in labels.items())
                or not isinstance(labels.get("alertname"), str)
                or not labels.get("alertname")):
            invalid += 1
            continue
        if alert.get("state") == "firing":
            firing.append(alert)
        elif alert.get("state") == "pending":
            pending.append(alert)
        else:
            invalid += 1
    return firing, pending, invalid


def _validate_rule_evidence(rules_data, runtime_data):
    if not isinstance(rules_data, dict) or not isinstance(runtime_data, dict):
        return False, 0.0, "missing rule/runtime evidence"
    groups = rules_data.get("groups")
    server_time = parse_rfc3339(runtime_data.get("serverTime"))
    start_time = parse_rfc3339(runtime_data.get("startTime"))
    if not isinstance(groups, list) or not groups or server_time is None or start_time is None:
        return False, 0.0, "malformed rule/runtime evidence"
    uptime = server_time - start_time
    if uptime < 0:
        return False, 0.0, "Prometheus runtime clock evidence is invalid"
    alert_rule_count = 0
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("rules"), list):
            return False, uptime, "malformed rule group"
        interval = group.get("interval", 0)
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
            return False, uptime, "invalid rule interval"
        evaluated = parse_rfc3339(group.get("lastEvaluation"))
        if evaluated is None:
            return False, uptime, "missing rule evaluation timestamp"
        age = server_time - evaluated
        if age < -30 or age > max(RULE_EVALUATION_MAX_AGE, interval * 4):
            return False, uptime, "stale rule evaluation"
        for rule in group["rules"]:
            if not isinstance(rule, dict) or rule.get("type") != "alerting":
                return False, uptime, "unexpected rule API entry"
            alert_rule_count += 1
            if rule.get("health") != "ok":
                return False, uptime, "unhealthy alert rule evaluation"
    if alert_rule_count == 0:
        return False, uptime, "no alert rules loaded"
    return True, uptime, ""


def fetch_alert_snapshot():
    alerts_data = _fetch_json(PROMETHEUS_ALERTS_URL)
    if alerts_data is None:
        return AlertSnapshot([], [], 0, False, False, 0.0, "alerts API unavailable")
    firing, pending, invalid = _parse_alerts(alerts_data)
    if firing is None:
        print("Prometheus alerts API returned an unexpected shape", file=sys.stderr)
        return AlertSnapshot([], [], 0, False, False, 0.0, "alerts API malformed")
    rules_data = _fetch_json(PROMETHEUS_RULES_URL)
    runtime_data = _fetch_json(PROMETHEUS_RUNTIME_URL)
    fresh, uptime, reason = _validate_rule_evidence(rules_data, runtime_data)
    if invalid:
        print(f"Prometheus alerts API contained {invalid} malformed alert entr"
              f"{'y' if invalid == 1 else 'ies'}; valid firing alerts remain eligible, resolutions are held",
              file=sys.stderr)
    if not fresh:
        print(f"Prometheus alert-state evidence is not fresh ({reason})", file=sys.stderr)
    return AlertSnapshot(firing, pending, invalid, True, fresh, uptime, reason)


def fetch_firing_alerts():
    """Compatibility helper: valid firing alerts or None on unusable evidence."""
    snapshot = fetch_alert_snapshot()
    return snapshot.firing if snapshot.query_ok and snapshot.evidence_fresh else None


def canonical_labels(alert):
    labels = alert.get("labels", {})
    return "|".join(f"{key}={labels[key]}" for key in sorted(labels))


def _salt_bytes(state):
    meta = state.get(META_KEY)
    salt = meta.get("fingerprint_salt") if isinstance(meta, dict) else None
    if not isinstance(salt, str) or not HEX_64_RE.fullmatch(salt):
        raise StateError("state fingerprint salt is missing or invalid")
    return bytes.fromhex(salt)


def fingerprint(alert, salt=None):
    """HMAC of the exact sorted label set; raw labels never enter state keys."""
    if salt is None:
        salt = b"notifier-selftest-fixture-salt"
    if isinstance(salt, str):
        salt = bytes.fromhex(salt) if HEX_64_RE.fullmatch(salt) else salt.encode()
    return hmac.new(salt, canonical_labels(alert).encode("utf-8"), hashlib.sha256).hexdigest()


def _new_state(now):
    return {
        SCHEMA_KEY: SCHEMA_VERSION,
        META_KEY: {
            "fingerprint_salt": secrets.token_hex(32),
            "initialized_at": now,
            "baseline_pending": True,
            "complete_poll_streak": 0,
        },
        METRICS_KEY: default_metrics(),
    }


def _entry_state_mac(entry, salt):
    unsigned = {key: value for key, value in entry.items() if key != "state_mac"}
    payload = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hmac.new(salt, payload, hashlib.sha256).hexdigest()


def _validate_safe_label_map(labels):
    if not isinstance(labels, dict):
        raise StateError("stored notification labels are invalid")
    for key, value in labels.items():
        if key not in NOTIFICATION_LABEL_ALLOWLIST or not isinstance(value, str):
            raise StateError("stored notification labels are invalid")
        if not value or value != sanitize_label_value(value):
            raise StateError("stored notification label value is invalid")
        if key == "file" and os.path.basename(value) != value:
            raise StateError("stored notification file label is invalid")
        if key == "mountpoint" and not (
            value == "/"
            or value == "(non-root filesystem)"
            or value.startswith("/System/Volumes/")
        ):
            raise StateError("stored notification mountpoint is invalid")


def _validate_state_v2(state, *, require_mac=True):
    if not isinstance(state, dict) or state.get(SCHEMA_KEY) != SCHEMA_VERSION:
        raise StateError("state schema is invalid")
    allowed_top = RESERVED_KEYS | _known_alert_keys(state)
    if set(state) - allowed_top:
        raise StateError("state contains unknown entries")

    meta = state.get(META_KEY)
    if not isinstance(meta, dict) or set(meta) != {
        "fingerprint_salt", "initialized_at", "baseline_pending",
        "complete_poll_streak",
    }:
        raise StateError("state metadata is invalid")
    salt = _salt_bytes(state)
    if not isinstance(meta.get("baseline_pending"), bool):
        raise StateError("state baseline flag is invalid")
    if _valid_number(meta.get("initialized_at")) != meta.get("initialized_at"):
        raise StateError("state initialization timestamp is invalid")
    if _valid_int(meta.get("complete_poll_streak")) != meta.get("complete_poll_streak"):
        raise StateError("state poll streak is invalid")

    raw_metrics = state.get(METRICS_KEY)
    if not isinstance(raw_metrics, dict) or set(raw_metrics) - set(default_metrics()):
        raise StateError("state metrics are invalid")
    for value in raw_metrics.values():
        if _valid_number(value) != value:
            raise StateError("state metric value is invalid")

    alert_fields = {
        "alertname", "safe_labels", "status", "since", "missing_polls",
        "sequence_id", "next_attempt_at", "outcome_unknown",
        "firing_failure_counted", "resolution_failure_counted", "state_mac",
    }
    for key in _known_alert_keys(state):
        digest = key[len(ALERT_KEY_PREFIX):]
        entry = state.get(key)
        if not HEX_64_RE.fullmatch(digest) or not isinstance(entry, dict):
            raise StateError("alert state entry is invalid")
        expected_fields = alert_fields if require_mac else alert_fields - {"state_mac"}
        if set(entry) - alert_fields or not expected_fields <= set(entry):
            raise StateError("alert state entry fields are invalid")
        if entry.get("alertname") != sanitize_name(entry.get("alertname")):
            raise StateError("stored alert name is invalid")
        _validate_safe_label_map(entry.get("safe_labels"))
        if entry.get("status") not in ALERT_ENTRY_STATUSES:
            raise StateError("alert state status is invalid")
        if _valid_number(entry.get("since")) != entry.get("since"):
            raise StateError("alert state timestamp is invalid")
        if _valid_int(entry.get("missing_polls")) != entry.get("missing_polls"):
            raise StateError("alert missing-poll count is invalid")
        if entry.get("sequence_id") != sequence_id(digest):
            raise StateError("alert sequence identity is invalid")
        if _valid_number(entry.get("next_attempt_at")) != entry.get("next_attempt_at"):
            raise StateError("alert retry timestamp is invalid")
        for field in (
            "outcome_unknown", "firing_failure_counted",
            "resolution_failure_counted",
        ):
            if not isinstance(entry.get(field), bool):
                raise StateError("alert state flag is invalid")
        if require_mac:
            actual = entry.get("state_mac")
            expected = _entry_state_mac(entry, salt)
            if not isinstance(actual, str) or not hmac.compare_digest(actual, expected):
                raise StateError("alert state integrity check failed")

    outage = state.get(UNREACHABLE_KEY)
    if outage is not None:
        outage_fields = {
            "failures", "notified", "blocked", "resolution_blocked",
            "sequence_id", "next_attempt_at", "firing_failure_counted",
            "resolution_failure_counted", "state_mac",
        }
        if not isinstance(outage, dict):
            raise StateError("Prometheus outage state is invalid")
        expected_fields = outage_fields if require_mac else outage_fields - {"state_mac"}
        if set(outage) - outage_fields or not expected_fields <= set(outage):
            raise StateError("Prometheus outage state fields are invalid")
        if _valid_int(outage.get("failures")) != outage.get("failures"):
            raise StateError("Prometheus outage count is invalid")
        if outage.get("sequence_id") != "unbound-prometheus-alert-state":
            raise StateError("Prometheus outage sequence is invalid")
        if _valid_number(outage.get("next_attempt_at")) != outage.get("next_attempt_at"):
            raise StateError("Prometheus outage retry timestamp is invalid")
        for field in (
            "notified", "blocked", "resolution_blocked",
            "firing_failure_counted", "resolution_failure_counted",
        ):
            if not isinstance(outage.get(field), bool):
                raise StateError("Prometheus outage flag is invalid")
        if require_mac:
            actual = outage.get("state_mac")
            expected = _entry_state_mac(outage, salt)
            if not isinstance(actual, str) or not hmac.compare_digest(actual, expected):
                raise StateError("Prometheus outage state integrity check failed")


def _sign_state_entries(state):
    _validate_state_v2(state, require_mac=False)
    salt = _salt_bytes(state)
    for key in _known_alert_keys(state):
        state[key]["state_mac"] = _entry_state_mac(state[key], salt)
    if isinstance(state.get(UNREACHABLE_KEY), dict):
        state[UNREACHABLE_KEY]["state_mac"] = _entry_state_mac(
            state[UNREACHABLE_KEY], salt
        )
    _validate_state_v2(state, require_mac=True)


def load_state():
    try:
        info = _regular_owned_file(STATE_FILE, repair_mode=True)
    except StateError as exc:
        if not os.path.exists(STATE_FILE):
            return {}, "missing"
        return {}, "error"
    if info.st_size > MAX_STATE_BYTES:
        return {}, "error"
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}, "error"
    if not isinstance(state, dict):
        return {}, "error"
    return state, "ok"


def _migrate_state(state, now):
    """Normalize legacy raw-fingerprint state without retaining raw labels."""
    if state.get(SCHEMA_KEY) == SCHEMA_VERSION:
        _validate_state_v2(state)
        return state

    migrated = _new_state(now)
    migrated[META_KEY]["baseline_pending"] = False
    migrated[METRICS_KEY] = get_metrics(state, False, now=now)
    salt = _salt_bytes(migrated)
    for key, entry in state.items():
        if key in {METRICS_KEY, UNREACHABLE_KEY} or key.startswith("__"):
            continue
        if not isinstance(key, str):
            continue
        digest = hmac.new(salt, key.encode("utf-8"), hashlib.sha256).hexdigest()
        name = entry.get("alertname") if isinstance(entry, dict) else None
        migrated[f"{ALERT_KEY_PREFIX}{digest}"] = {
            "alertname": sanitize_name(name),
            "safe_labels": {},
            "status": "firing_notified",
            "since": _valid_number(entry.get("since") if isinstance(entry, dict) else 0),
            "missing_polls": 0,
            "sequence_id": sequence_id(digest),
            "next_attempt_at": 0,
            "outcome_unknown": False,
            "firing_failure_counted": False,
            "resolution_failure_counted": False,
        }
    outage = state.get(UNREACHABLE_KEY)
    if isinstance(outage, dict):
        migrated[UNREACHABLE_KEY] = {
            "failures": _valid_int(outage.get("failures")),
            "notified": bool(outage.get("notified")),
            "blocked": bool(outage.get("blocked")),
            "resolution_blocked": bool(outage.get("resolution_blocked")),
            "sequence_id": "unbound-prometheus-alert-state",
            "next_attempt_at": _valid_number(outage.get("next_attempt_at")),
            "firing_failure_counted": bool(outage.get("firing_failure_counted")),
            "resolution_failure_counted": bool(
                outage.get("resolution_failure_counted")
            ),
        }
    return migrated


def _quarantine_bad_state(now):
    if not os.path.exists(STATE_FILE):
        return
    try:
        _regular_owned_file(STATE_FILE, repair_mode=True)
        destination = f"{STATE_FILE}.invalid-{now}"
        os.replace(STATE_FILE, destination)
        os.chmod(destination, 0o600)
    except (OSError, StateError):
        # Never delete an unsafe path.  The subsequent save will fail closed.
        pass


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path, content, mode):
    directory = os.path.dirname(path)
    temp = f"{path}.tmp-{os.getpid()}-{time.time_ns()}"
    descriptor = None
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        os.chmod(path, mode)
        _fsync_directory(directory)
    except OSError as exc:
        raise StateError("atomic runtime write failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temp)


def save_state(state):
    _ensure_private_dir(os.path.dirname(STATE_FILE))
    _sign_state_entries(state)
    rendered = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    _atomic_write(STATE_FILE, rendered, 0o600)


class RunLock:
    def __init__(self, path=None):
        self.path = path or LOCK_FILE
        self.handle = None

    def __enter__(self):
        _ensure_private_dir(os.path.dirname(self.path))
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(self.path, 0o600)
        self.handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                if os.path.getsize(self.path) == 0:
                    self.handle.write(b"\0")
                    self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                raise OSError("no file-locking primitive")
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise OverlapError("another notifier run is active") from exc
        return self

    def __exit__(self, *_args):
        if self.handle is None:
            return
        with contextlib.suppress(OSError):
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.handle.close()
        self.handle = None


def sanitize_name(value):
    if not isinstance(value, str):
        return "UnknownAlert"
    safe = SAFE_NAME_RE.sub("_", value.strip())[:96]
    return safe or "UnknownAlert"


def sanitize_label_value(value):
    if not isinstance(value, str):
        return ""
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in value)[:160]


def sanitize_title(value):
    if not isinstance(value, str):
        return "Notifier transition"
    safe = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in value).strip()
    return safe[:160] or "Notifier transition"


def safe_labels(alert):
    labels = alert.get("labels", {})
    allowed = {
        key: sanitize_label_value(labels[key])
        for key in NOTIFICATION_LABEL_ALLOWLIST
        if isinstance(labels.get(key), str) and labels[key]
    }
    if "file" in allowed:
        allowed["file"] = os.path.basename(allowed["file"])
    if "mountpoint" in allowed:
        mountpoint = allowed["mountpoint"]
        if mountpoint != "/" and not mountpoint.startswith("/System/Volumes/"):
            allowed["mountpoint"] = "(non-root filesystem)"
    return allowed


def sequence_id(digest):
    return f"unbound-alert-{digest[:40]}"


def format_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _safe_numeric_value(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return value if math.isfinite(number) else None


def describe(alert):
    """Build a body only from explicitly allowed labels and numeric fields."""
    labels = alert.get("labels", {})
    name = sanitize_name(labels.get("alertname"))
    priority = PRIORITY_BY_SEVERITY.get(labels.get("severity", ""), "high")
    parts = ["Prometheus alert is firing."]
    allowed = safe_labels(alert)
    if allowed:
        parts.append(", ".join(f"{key}={allowed[key]}" for key in sorted(allowed)))
    active_epoch = parse_active_at(alert.get("activeAt"))
    now = time.time()
    if active_epoch is not None and 0 <= active_epoch <= now:
        parts.append(f"active for {format_duration(now - active_epoch)}")
    value = _safe_numeric_value(alert.get("value"))
    if value is not None:
        parts.append(f"value: {value}")
    return name, "\n".join(parts), priority


def _retry_after_seconds(headers, now=None):
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return 0
    try:
        delay = int(raw)
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = math.ceil(parsed.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, min(delay, MAX_RETRY_AFTER))


def _transport_outcome_unknown(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    known_before_acceptance = (
        socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError,
    )
    if isinstance(reason, known_before_acceptance):
        return False
    return isinstance(reason, (socket.timeout, TimeoutError, ConnectionResetError, OSError))


def push_ntfy(topic, title, message, priority, dry_run=False, sequence=None,
              sleep=time.sleep):
    """Publish with bounded retries and sanitized errors.

    A deterministic ntfy sequence ID makes crash retries update the same client
    notification.  ntfy remains append-only server-side, so this is not a claim
    of transactional exactly-once delivery.
    """
    title = sanitize_title(title)
    if dry_run:
        print(f"[dry-run] would publish one {priority} transition titled {title!r}; body withheld")
        return DeliveryResult(True, attempts=0)
    headers = {"Title": title, "Priority": priority, "Tags": "rotating_light"}
    if sequence:
        headers["X-Sequence-ID"] = sanitize_name(sequence)
    attempts = 0
    unknown = False
    for attempt in range(MAX_DELIVERY_ATTEMPTS):
        attempts += 1
        request = urllib.request.Request(
            f"{NTFY_SERVER.rstrip('/')}/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if 200 <= response.status < 300:
                    return DeliveryResult(True, attempts=attempts, status_code=response.status)
                code = int(response.status)
                retry_after = _retry_after_seconds(response.headers) if code == 429 else 0
                permanent = 400 <= code < 500 and code != 429
        except http.client.InvalidURL:
            print(f"ntfy server request rejected locally ({redact_topic(topic)})", file=sys.stderr)
            return DeliveryResult(False, permanent=True, attempts=attempts)
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            retry_after = _retry_after_seconds(exc.headers) if code == 429 else 0
            permanent = 400 <= code < 500 and code != 429
        except (urllib.error.URLError, OSError, UnicodeError, ValueError) as exc:
            unknown = unknown or _transport_outcome_unknown(exc)
            code = 0
            retry_after = 0
            permanent = False
            print(f"ntfy server request failed ({type(exc).__name__}); endpoint withheld",
                  file=sys.stderr)
        except Exception as exc:  # defensive: never let a URL-bearing exception escape
            print(f"ntfy server request failed ({type(exc).__name__}); endpoint withheld",
                  file=sys.stderr)
            return DeliveryResult(False, permanent=True, outcome_unknown=True,
                                  attempts=attempts)

        if permanent:
            print(f"ntfy server permanently rejected a request (HTTP {code})", file=sys.stderr)
            return DeliveryResult(False, permanent=True, outcome_unknown=unknown,
                                  attempts=attempts, status_code=code)
        if attempt + 1 >= MAX_DELIVERY_ATTEMPTS:
            print(f"ntfy server did not accept a request after {attempts} bounded attempt(s)",
                  file=sys.stderr)
            return DeliveryResult(False, outcome_unknown=unknown, attempts=attempts,
                                  retry_after=retry_after, status_code=code)
        if retry_after > MAX_INLINE_RETRY_AFTER:
            print("ntfy rate limit requested deferred retry; no early retry was attempted",
                  file=sys.stderr)
            return DeliveryResult(False, outcome_unknown=unknown, attempts=attempts,
                                  retry_after=retry_after, status_code=code)
        delay = retry_after or RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
        sleep(delay)
    raise AssertionError("bounded delivery loop fell through")


def _valid_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _valid_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value if math.isfinite(value) and value >= 0 else 0


def default_metrics():
    return {
        "last_success": 0,
        "last_failure": 0,
        "last_server_acceptance": 0,
        "sent_total": 0,
        "fail_total": 0,
        "delivery_unknown_total": 0,
        "state_errors_total": 0,
        "state_write_errors_total": 0,
        "state_resets_total": 0,
        "retry_attempts_total": 0,
        "permanent_rejections_total": 0,
        "consecutive_failures": 0,
    }


def get_metrics(state, state_had_error=False, now=None):
    metrics = default_metrics()
    raw = state.get(METRICS_KEY) if isinstance(state, dict) else None
    if isinstance(raw, dict):
        for key in metrics:
            metrics[key] = _valid_number(raw.get(key))
    if now is not None:
        for key in ("last_success", "last_failure", "last_server_acceptance"):
            if metrics[key] > now + 300:
                metrics[key] = 0
                state_had_error = True
    if state_had_error:
        metrics["state_errors_total"] += 1
    return metrics


def _metric_lines(metrics, observation, now):
    lines = []
    if metrics["last_success"] > 0:
        lines += [
            "# TYPE unbound_job_last_success_timestamp_seconds gauge",
            f'unbound_job_last_success_timestamp_seconds{{task="alertnotify"}} {int(metrics["last_success"])}',
        ]

    def gauge(name, help_text, value):
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {value}"))

    def counter(name, help_text, value):
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} counter", f"{name} {int(value)}"))

    gauge("unbound_ntfy_last_attempt_timestamp_seconds",
          "Unix timestamp of the notifier's most recent process run.", now)
    if metrics["last_failure"] > 0:
        gauge("unbound_ntfy_last_failure_timestamp_seconds",
              "Unix timestamp of the most recent failed query, state operation, or ntfy request.",
              int(metrics["last_failure"]))
    if metrics["last_server_acceptance"] > 0:
        gauge("unbound_ntfy_last_server_acceptance_timestamp_seconds",
              "Unix timestamp of the most recent HTTP request accepted by the ntfy server; phone receipt is unknown.",
              int(metrics["last_server_acceptance"]))
    gauge("unbound_ntfy_prometheus_query_success",
          "1 if the alerts API returned a successfully parsed envelope this run.",
          int(bool(observation["query_ok"])))
    gauge("unbound_ntfy_alert_state_processed",
          "1 if alert, rule-evaluation, and runtime evidence was complete and fresh this run.",
          int(bool(observation["state_processed"])))
    gauge("unbound_ntfy_resolutions_enabled",
          "1 if complete evidence and Prometheus restart grace allowed resolutions this run.",
          int(bool(observation["resolutions_enabled"])))
    gauge("unbound_ntfy_state_persistence_success",
          "1 if durable state writes completed this run.",
          int(bool(observation["persistence_ok"])))
    gauge("unbound_ntfy_topic_ready",
          "1 if the topic file existed with safe ownership, mode, encoding, and syntax this run.",
          int(bool(observation["topic_ready"])))
    gauge("unbound_ntfy_phone_receipt_known",
          "Always 0: ntfy HTTP acceptance cannot prove operator-device receipt.", 0)
    gauge("unbound_ntfy_firing_alerts", "Valid firing alerts in this poll.", observation["firing_count"])
    gauge("unbound_ntfy_pending_alerts", "Valid pending alerts in this poll; pending alerts are not published.", observation["pending_count"])
    gauge("unbound_ntfy_invalid_alerts", "Malformed alert entries isolated in this poll.", observation["invalid_alerts"])
    gauge("unbound_ntfy_suppressed_duplicates", "Current firing alerts already represented in durable state.", observation["suppressed"])
    gauge("unbound_ntfy_resolutions_suppressed", "Potential resolutions held by incomplete, restart-grace, or dependency evidence.", observation["resolutions_suppressed"])
    gauge("unbound_ntfy_blocked_transitions", "Current firing or resolved transitions latched after a permanent ntfy rejection.", observation["blocked_transitions"])
    gauge("unbound_ntfy_consecutive_failures", "Consecutive notifier runs that did not fully succeed.", int(metrics["consecutive_failures"]))
    counter("unbound_ntfy_server_acceptances_total", "ntfy HTTP requests accepted by the server; phone receipt is unknown.", metrics["sent_total"])
    counter("unbound_ntfy_notifications_sent_total", "Deprecated compatibility alias for ntfy server acceptances, not phone delivery.", metrics["sent_total"])
    counter("unbound_ntfy_delivery_failures_total", "Logical ntfy transitions not accepted after bounded attempts.", metrics["fail_total"])
    counter("unbound_ntfy_delivery_outcome_unknown_total", "Requests whose timeout/reset left ntfy server acceptance unknown.", metrics["delivery_unknown_total"])
    counter("unbound_ntfy_state_errors_total", "Invalid, unreadable, unsafe, or clock-inconsistent state observations.", metrics["state_errors_total"])
    counter("unbound_ntfy_state_write_errors_total", "Durable state writes that failed.", metrics["state_write_errors_total"])
    counter("unbound_ntfy_state_resets_total", "Missing or invalid state baselines adopted without replaying active alerts.", metrics["state_resets_total"])
    counter("unbound_ntfy_retry_attempts_total", "Additional ntfy HTTP attempts after the first bounded attempt.", metrics["retry_attempts_total"])
    counter("unbound_ntfy_permanent_rejections_total", "Permanent ntfy HTTP 4xx rejections excluding 429.", metrics["permanent_rejections_total"])
    return lines


def write_metrics(metrics, query_ok=None, firing_count=0, suppressed=0, now=None,
                  observation=None):
    if now is None:
        now = int(time.time())
    if observation is None:
        observation = {
            "query_ok": bool(query_ok), "state_processed": bool(query_ok),
            "resolutions_enabled": bool(query_ok), "persistence_ok": True,
            "topic_ready": True, "firing_count": firing_count, "pending_count": 0,
            "invalid_alerts": 0, "suppressed": suppressed,
            "resolutions_suppressed": 0, "blocked_transitions": 0,
        }
    try:
        os.makedirs(METRICS_DIR, exist_ok=True)
        path = os.path.join(METRICS_DIR, "unbound-alertnotify.prom")
        _atomic_write(path, "\n".join(_metric_lines(metrics, observation, now)) + "\n", 0o644)
        return True
    except (OSError, StateError):
        print("could not write notifier textfile metrics", file=sys.stderr)
        return False


def _new_alert_entry(alert, digest, now, status="pending_firing"):
    name, _message, _priority = describe(alert)
    return {
        "alertname": name,
        "safe_labels": safe_labels(alert),
        "status": status,
        "since": now,
        "missing_polls": 0,
        "sequence_id": sequence_id(digest),
        "next_attempt_at": 0,
        "outcome_unknown": False,
        "firing_failure_counted": False,
        "resolution_failure_counted": False,
    }


def _known_alert_keys(state):
    return {key for key in state if isinstance(key, str) and key.startswith(ALERT_KEY_PREFIX)}


def _blocked_transition_count(state):
    blocked = sum(
        1 for key in _known_alert_keys(state)
        if isinstance(state.get(key), dict)
        and state[key].get("status") in {"blocked_firing", "blocked_resolution"}
    )
    outage = state.get(UNREACHABLE_KEY)
    if isinstance(outage, dict) and (
        outage.get("blocked") or outage.get("resolution_blocked")
    ):
        blocked += 1
    return blocked


def _pending_transition_count(state):
    pending = sum(
        1 for key in _known_alert_keys(state)
        if isinstance(state.get(key), dict)
        and state[key].get("status") in {"pending_firing", "pending_resolved"}
    )
    outage = state.get(UNREACHABLE_KEY)
    if isinstance(outage, dict) and outage.get("notified") and not (
        outage.get("blocked") or outage.get("resolution_blocked")
    ):
        pending += 1
    return pending


def _resolution_blocked(alertname, active_names):
    if GLOBAL_RESOLUTION_BLOCKERS & active_names:
        return True
    if alertname in NODE_DEPENDENT_ALERTS and (
            {"NodeExporterDown", "TextfileScrapeError"} & active_names):
        return True
    if alertname in BLACKBOX_DEPENDENT_ALERTS and (
            {"BlackboxExporterDown", "BlackboxProbeResultInventoryMismatch"} & active_names):
        return True
    if alertname in UNBOUND_DEPENDENT_ALERTS and "UnboundExporterDown" in active_names:
        return True
    if alertname in {"PrometheusConfigReloadFailed", "PrometheusRuleEvaluationFailures"} \
            and "PrometheusSelfScrapeDown" in active_names:
        return True
    if PAIR_RESOLUTION_BLOCKERS.get(alertname, set()) & active_names:
        return True
    return False


def _persist_or_record_failure(state, metrics, observation, now):
    state[METRICS_KEY] = metrics
    try:
        save_state(state)
        return True
    except StateError:
        metrics["state_write_errors_total"] += 1
        metrics["last_failure"] = now
        observation["persistence_ok"] = False
        print("could not persist notifier state", file=sys.stderr)
        return False


def _record_delivery_result(metrics, result, now, *, count_failure=True):
    metrics["retry_attempts_total"] += max(0, result.attempts - 1)
    if result.accepted:
        metrics["sent_total"] += 1
        metrics["last_server_acceptance"] = now
        return
    if count_failure:
        metrics["fail_total"] += 1
    metrics["last_failure"] = now
    if result.outcome_unknown:
        metrics["delivery_unknown_total"] += 1
    if result.permanent:
        metrics["permanent_rejections_total"] += 1


def _deliver_entry(state, key, alert, topic, metrics, observation, now, is_resolution=False):
    entry = state[key]
    if not _persist_or_record_failure(state, metrics, observation, now):
        return False, True
    if is_resolution:
        labels = entry.get("safe_labels") if isinstance(entry.get("safe_labels"), dict) else {}
        parts = ["Prometheus alert is no longer firing after two complete, dependency-qualified polls."]
        if labels:
            parts.append(", ".join(f"{name}={labels[name]}" for name in sorted(labels)))
        title = f"RESOLVED: {entry.get('alertname', 'UnknownAlert')}"
        message = "\n".join(parts)
        priority = "default"
    else:
        name, message, priority = describe(alert)
        title = f"FIRING: {name}"
    result = push_ntfy(topic, title, message, priority, False,
                       sequence=entry.get("sequence_id"))
    failure_flag = (
        "resolution_failure_counted" if is_resolution
        else "firing_failure_counted"
    )
    _record_delivery_result(
        metrics, result, now, count_failure=not bool(entry.get(failure_flag))
    )
    if result.accepted:
        if is_resolution:
            del state[key]
        else:
            entry["status"] = "firing_notified"
            entry["outcome_unknown"] = False
            entry["next_attempt_at"] = 0
    elif result.permanent:
        if is_resolution:
            entry["status"] = "blocked_resolution"
            entry["next_attempt_at"] = 0
        else:
            entry["status"] = "blocked_firing"
    else:
        entry["outcome_unknown"] = bool(entry.get("outcome_unknown")) or result.outcome_unknown
        entry["next_attempt_at"] = now + max(120, result.retry_after)
    if not result.accepted:
        entry[failure_flag] = True
    persisted = _persist_or_record_failure(state, metrics, observation, now)
    return result.accepted, not persisted


def _process_unavailable_state(state, snapshot, topic, metrics, observation, now):
    entry = state.get(UNREACHABLE_KEY)
    if not isinstance(entry, dict):
        entry = {
            "failures": 0,
            "notified": False,
            "blocked": False,
            "resolution_blocked": False,
            "sequence_id": "unbound-prometheus-alert-state",
            "next_attempt_at": 0,
            "firing_failure_counted": False,
            "resolution_failure_counted": False,
        }
    entry["failures"] = _valid_int(entry.get("failures")) + 1
    state[UNREACHABLE_KEY] = entry
    metrics["last_failure"] = now
    next_attempt = _valid_number(entry.get("next_attempt_at"))
    if next_attempt > now + MAX_RETRY_AFTER:
        next_attempt = now + MAX_RETRY_AFTER
        entry["next_attempt_at"] = next_attempt
    if (entry["failures"] >= UNREACHABLE_THRESHOLD and not entry.get("notified")
            and not entry.get("blocked") and topic is not None):
        if next_attempt > now:
            return False, False, 0
        if not _persist_or_record_failure(state, metrics, observation, now):
            return True, True, 0
        result = push_ntfy(
            topic,
            "FIRING: PrometheusAlertStateUnavailable",
            "Prometheus alert-state polling, rule freshness, or runtime evidence has failed repeatedly. Rule-based notification state is held until complete evidence recovers.",
            "high",
            False,
            sequence=entry["sequence_id"],
        )
        _record_delivery_result(
            metrics,
            result,
            now,
            count_failure=not bool(entry.get("firing_failure_counted")),
        )
        if result.accepted:
            entry["notified"] = True
            entry["next_attempt_at"] = 0
        elif result.permanent:
            entry["blocked"] = True
        else:
            entry["next_attempt_at"] = now + max(120, result.retry_after)
        if not result.accepted:
            entry["firing_failure_counted"] = True
        if not _persist_or_record_failure(state, metrics, observation, now):
            return True, True, 1
        return False, not result.accepted, 1
    return False, False, 0


def _recover_unavailable_state(state, topic, metrics, observation, now):
    entry = state.get(UNREACHABLE_KEY)
    if not isinstance(entry, dict):
        state.pop(UNREACHABLE_KEY, None)
        return False, False, 0
    if entry.get("notified") and topic is None:
        return False, True, 0
    if entry.get("notified") and topic is not None:
        if entry.get("resolution_blocked"):
            return False, True, 0
        next_attempt = _valid_number(entry.get("next_attempt_at"))
        if next_attempt > now + MAX_RETRY_AFTER:
            next_attempt = now + MAX_RETRY_AFTER
            entry["next_attempt_at"] = next_attempt
        if next_attempt > now:
            return False, True, 0
        if not _persist_or_record_failure(state, metrics, observation, now):
            return True, True, 0
        result = push_ntfy(
            topic,
            "RESOLVED: PrometheusAlertStateUnavailable",
            "Prometheus alert-state evidence is complete and fresh again.",
            "default",
            False,
            sequence=entry.get("sequence_id", "unbound-prometheus-alert-state"),
        )
        _record_delivery_result(
            metrics,
            result,
            now,
            count_failure=not bool(entry.get("resolution_failure_counted")),
        )
        if result.accepted:
            state.pop(UNREACHABLE_KEY, None)
        elif result.permanent:
            entry["resolution_blocked"] = True
            entry["resolution_failure_counted"] = True
            entry["next_attempt_at"] = 0
        else:
            entry["resolution_failure_counted"] = True
            entry["next_attempt_at"] = now + max(120, result.retry_after)
        persisted = _persist_or_record_failure(state, metrics, observation, now)
        return not persisted, not result.accepted, 1
    state.pop(UNREACHABLE_KEY, None)
    return not _persist_or_record_failure(state, metrics, observation, now), False, 0


def process_snapshot(state, snapshot, topic, metrics, observation, now):
    meta = state[META_KEY]
    if not snapshot.query_ok or not snapshot.evidence_fresh:
        meta["complete_poll_streak"] = 0
        observation["resolutions_suppressed"] = len(_known_alert_keys(state))
        persistence_failed, _delivery_failed, _events = _process_unavailable_state(
            state, snapshot, topic, metrics, observation, now
        )
        observation["blocked_transitions"] = _blocked_transition_count(state)
        return persistence_failed, 1

    events = 0
    run_failures = 0
    if snapshot.invalid_alerts:
        persistence_failed, delivery_failed, outage_events = _process_unavailable_state(
            state, snapshot, topic, metrics, observation, now
        )
        events += outage_events
        run_failures += int(delivery_failed)
        if persistence_failed:
            observation["blocked_transitions"] = _blocked_transition_count(state)
            return True, max(1, run_failures)
    else:
        persistence_failed, delivery_failed, outage_events = _recover_unavailable_state(
            state, topic, metrics, observation, now
        )
        events += outage_events
        run_failures += int(delivery_failed)
        if persistence_failed:
            observation["blocked_transitions"] = _blocked_transition_count(state)
            return True, max(1, run_failures)

    salt = _salt_bytes(state)
    current = {f"{ALERT_KEY_PREFIX}{fingerprint(alert, salt)}": alert
               for alert in snapshot.firing}
    known = _known_alert_keys(state)

    if meta.get("baseline_pending"):
        for key, alert in current.items():
            state[key] = _new_alert_entry(alert, key[len(ALERT_KEY_PREFIX):], now,
                                          status="observed_baseline")
        meta["baseline_pending"] = False
        meta["complete_poll_streak"] = 1 if snapshot.state_complete else 0
        metrics["state_resets_total"] += 1
        observation["suppressed"] = len(current)
        return (not _persist_or_record_failure(state, metrics, observation, now)), 0

    for key in set(current) & known:
        entry = state[key]
        if isinstance(entry, dict):
            entry["missing_polls"] = 0
            if entry.get("status") == "pending_resolved":
                entry["status"] = "firing_notified"
            elif entry.get("status") == "blocked_resolution":
                entry["status"] = "firing_notified"
                entry["resolution_failure_counted"] = False

    for key, alert in current.items():
        if key not in state:
            state[key] = _new_alert_entry(alert, key[len(ALERT_KEY_PREFIX):], now)

    meta["complete_poll_streak"] = (
        _valid_int(meta.get("complete_poll_streak")) + 1 if snapshot.state_complete else 0
    )
    active_names = snapshot.active_alert_names
    known = _known_alert_keys(state)
    for key in sorted(known - set(current)):
        entry = state.get(key)
        if not isinstance(entry, dict):
            del state[key]
            continue
        name = entry.get("alertname", "UnknownAlert")
        blocked = (
            not snapshot.resolutions_enabled
            or not snapshot.state_complete
            or _resolution_blocked(name, active_names)
        )
        if blocked:
            entry["missing_polls"] = 0
            observation["resolutions_suppressed"] += 1
            continue
        entry["missing_polls"] = _valid_int(entry.get("missing_polls")) + 1
        if entry["missing_polls"] < RESOLUTION_CONFIRM_POLLS:
            observation["resolutions_suppressed"] += 1
            continue
        status = entry.get("status")
        if status == "blocked_resolution":
            observation["resolutions_suppressed"] += 1
            continue
        if status in {"observed_baseline", "blocked_firing"}:
            del state[key]
        elif status == "pending_firing" and not entry.get("outcome_unknown"):
            del state[key]
        else:
            entry["status"] = "pending_resolved"

    if not _persist_or_record_failure(state, metrics, observation, now):
        return True, 1

    for key in sorted(_known_alert_keys(state)):
        if events >= MAX_EVENTS_PER_RUN:
            break
        entry = state.get(key)
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        next_attempt = _valid_number(entry.get("next_attempt_at"))
        if next_attempt > now + MAX_RETRY_AFTER:
            next_attempt = now + MAX_RETRY_AFTER
            entry["next_attempt_at"] = next_attempt
        if next_attempt > now:
            continue
        if status == "pending_firing" and key in current:
            if topic is None:
                continue
            accepted, persistence_failed = _deliver_entry(
                state, key, current[key], topic, metrics, observation, now, False)
            events += 1
            run_failures += int(not accepted or persistence_failed)
            if persistence_failed:
                return True, run_failures
        elif status == "pending_resolved" and key not in current:
            if topic is None:
                continue
            accepted, persistence_failed = _deliver_entry(
                state, key, None, topic, metrics, observation, now, True)
            events += 1
            run_failures += int(not accepted or persistence_failed)
            if persistence_failed:
                return True, run_failures

    observation["suppressed"] = sum(
        1 for key in current
        if isinstance(state.get(key), dict)
        and state[key].get("status") in {
            "firing_notified", "observed_baseline", "blocked_firing"
        }
    )
    observation["blocked_transitions"] = _blocked_transition_count(state)
    if _pending_transition_count(state) or observation["blocked_transitions"]:
        run_failures = max(1, run_failures)
    return (not _persist_or_record_failure(state, metrics, observation, now)), run_failures


def test_send(topic):
    result = push_ntfy(
        topic,
        "TEST: mini alert notifier",
        "Manual notifier acceptance test. Confirm operator-device receipt separately.",
        "default",
        False,
        sequence=f"unbound-manual-test-{int(time.time())}",
    )
    if result.accepted:
        print("ntfy server accepted the test request; phone receipt remains to be checked")
        return 0
    print("ntfy server did not accept the test request", file=sys.stderr)
    return 1


def selftest():
    import tempfile
    ok = True

    def check(condition, message):
        nonlocal ok
        if not condition:
            print(f"FAIL {message}", file=sys.stderr)
            ok = False

    first = {"labels": {"b": "2", "a": "1"}}
    second = {"labels": {"a": "1", "b": "2"}}
    check(fingerprint(first) == fingerprint(second), "fingerprint ordering")
    check("fixture" not in redact_topic("fixture-private-topic"), "topic redaction")
    check(parse_active_at("2026-07-09T21:00:00.123456789Z") is not None,
          "RFC3339 nanoseconds")
    name, message, priority = describe({
        "labels": {
            "alertname": "FixtureAlert", "severity": "critical", "instance": "safe",
            "client": "synthetic-private-label",
        },
        "annotations": {"summary": "synthetic-private-message"},
        "value": "1.5",
    })
    check(name == "FixtureAlert" and priority == "urgent", "safe description identity")
    check("instance=safe" in message, "allowed label missing")
    check("synthetic-private" not in message, "private label/annotation leaked")

    global TOPIC_FILE, STATE_FILE, STATE_DIR, LOCK_FILE, METRICS_DIR
    originals = TOPIC_FILE, STATE_FILE, STATE_DIR, LOCK_FILE, METRICS_DIR
    with tempfile.TemporaryDirectory() as directory:
        STATE_DIR = os.path.join(directory, "state")
        STATE_FILE = os.path.join(STATE_DIR, "state.json")
        LOCK_FILE = os.path.join(STATE_DIR, "state.lock")
        METRICS_DIR = os.path.join(directory, "metrics")
        TOPIC_FILE = os.path.join(directory, "topic")
        with open(TOPIC_FILE, "w", encoding="utf-8") as handle:
            handle.write("synthetic-topic\n")
        os.chmod(TOPIC_FILE, 0o600)
        check(read_topic() == "synthetic-topic", "topic read")
        state = _new_state(100)
        save_state(state)
        loaded, status_value = load_state()
        check(status_value == "ok" and loaded[SCHEMA_KEY] == SCHEMA_VERSION,
              "durable state round trip")
        if os.name != "nt":
            check(stat.S_IMODE(os.stat(STATE_FILE).st_mode) == 0o600, "state mode")
        metrics = default_metrics()
        metrics["last_success"] = 100
        check(write_metrics(metrics, query_ok=True, now=200), "metrics write")
        rendered = open(os.path.join(METRICS_DIR, "unbound-alertnotify.prom"),
                        encoding="utf-8").read()
        check("unbound_ntfy_phone_receipt_known 0" in rendered, "receipt metric")
        check("synthetic-topic" not in rendered, "topic leaked to metrics")
    TOPIC_FILE, STATE_FILE, STATE_DIR, LOCK_FILE, METRICS_DIR = originals

    if ok:
        print("selftest: OK")
    return ok


def _observation(snapshot, topic_ready):
    return {
        "query_ok": snapshot.query_ok,
        "state_processed": snapshot.state_complete,
        "resolutions_enabled": snapshot.resolutions_enabled,
        "persistence_ok": True,
        "topic_ready": topic_ready,
        "firing_count": len(snapshot.firing),
        "pending_count": len(snapshot.pending),
        "invalid_alerts": snapshot.invalid_alerts,
        "suppressed": 0,
        "resolutions_suppressed": 0,
        "blocked_transitions": 0,
    }


def run_once(dry_run=False):
    now = int(time.time())
    if dry_run:
        snapshot = fetch_alert_snapshot()
        try:
            read_topic()
            topic_ready = True
        except TopicError as exc:
            print(str(exc), file=sys.stderr)
            topic_ready = False
        print(
            f"[dry-run] firing={len(snapshot.firing)} pending={len(snapshot.pending)} "
            f"state_complete={int(snapshot.state_complete)} "
            f"topic_ready={int(topic_ready)}; labels and bodies withheld"
        )
        return 0 if snapshot.query_ok and topic_ready else 1

    state, state_status = load_state()
    state_error = state_status == "error"
    if state_error:
        print("notifier state was invalid; active alerts will be adopted as a no-replay baseline",
              file=sys.stderr)
        _quarantine_bad_state(now)
    if state_status != "ok":
        state = _new_state(now)
    try:
        state = _migrate_state(state, now)
    except StateError:
        state_error = True
        _quarantine_bad_state(now)
        state = _new_state(now)

    metrics = get_metrics(state, state_error, now=now)
    snapshot = fetch_alert_snapshot()

    try:
        topic = "(dry-run)" if dry_run else read_topic()
        topic_ready = True
    except TopicError as exc:
        print(str(exc), file=sys.stderr)
        topic = None
        topic_ready = False

    observation = _observation(snapshot, topic_ready)
    persistence_failed, run_failures = process_snapshot(
        state, snapshot, topic, metrics, observation, now)
    if not topic_ready:
        run_failures += 1
        metrics["fail_total"] += 1
        metrics["last_failure"] = now

    full_success = (
        snapshot.state_complete and topic_ready and run_failures == 0
        and not persistence_failed
    )
    if full_success:
        metrics["last_success"] = now
        metrics["consecutive_failures"] = 0
    else:
        metrics["last_failure"] = now
        metrics["consecutive_failures"] += 1
    if not _persist_or_record_failure(state, metrics, observation, now):
        persistence_failed = True
    metrics_ok = write_metrics(metrics, observation=observation, now=now)
    return 0 if full_success and metrics_ok and not persistence_failed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="inspect current evidence without publishing, locking, or changing state")
    parser.add_argument("--test-send", action="store_true",
                        help="send one real acceptance test; phone receipt must be checked separately")
    parser.add_argument("--selftest", action="store_true",
                        help="run offline self-tests and exit")
    args = parser.parse_args()

    if args.selftest:
        raise SystemExit(0 if selftest() else 1)
    if args.test_send:
        try:
            raise SystemExit(test_send(read_topic()))
        except TopicError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
    if args.dry_run:
        raise SystemExit(run_once(dry_run=True))

    try:
        with RunLock():
            raise SystemExit(run_once())
    except OverlapError:
        print("another notifier run is active; this overlapping invocation did no work",
              file=sys.stderr)
        raise SystemExit(0)
    except StateError:
        print("notifier state path is unavailable or unsafe", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
