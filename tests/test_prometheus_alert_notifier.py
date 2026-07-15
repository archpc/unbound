#!/usr/bin/env python3
"""Failure-matrix and state-machine tests for the local alert notifier."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hardened_prometheus_alert_notifier",
    ROOT / "scripts/monitoring/prometheus_alert_notifier.py",
)
notifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notifier)


def alert(name="FixtureAlert", state="firing", **labels):
    return {
        "state": state,
        "labels": {"alertname": name, **labels},
        "annotations": {"summary": "synthetic annotation must not be trusted"},
        "activeAt": "2026-07-15T00:00:00Z",
        "value": "1",
    }


def snapshot(firing=None, pending=None, invalid=0, query_ok=True,
             evidence_fresh=True, uptime=3600):
    return notifier.AlertSnapshot(
        list(firing or []), list(pending or []), invalid,
        query_ok, evidence_fresh, uptime,
    )


class FakeResponse:
    def __init__(self, payload=b"", status=200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.payload


class PrometheusFailureMatrixTests(unittest.TestCase):
    def test_dependency_contract_names_match_the_rule_inventory(self):
        rules = (ROOT / "config/prometheus/prometheus.rules.yml").read_text()
        inventory = set(re.findall(r"^\s+- alert:\s+(\S+)", rules, re.MULTILINE))
        referenced = (
            notifier.GLOBAL_RESOLUTION_BLOCKERS
            | notifier.NODE_DEPENDENT_ALERTS
            | notifier.BLACKBOX_DEPENDENT_ALERTS
            | notifier.UNBOUND_DEPENDENT_ALERTS
            | set(notifier.PAIR_RESOLUTION_BLOCKERS)
            | set().union(*notifier.PAIR_RESOLUTION_BLOCKERS.values())
            | {
                "NodeExporterDown", "TextfileScrapeError", "BlackboxExporterDown",
                "BlackboxProbeResultInventoryMismatch", "UnboundExporterDown",
                "PrometheusSelfScrapeDown",
            }
        )
        self.assertEqual(len(inventory), 39)
        self.assertEqual(referenced - inventory, set())

    def test_network_http_and_payload_failures_are_unavailable(self):
        failures = [
            ConnectionRefusedError(),
            TimeoutError(),
            socket.gaierror(),
            urllib.error.URLError("synthetic DNS failure"),
            urllib.error.HTTPError("http://127.0.0.1", 503, "synthetic", {}, None),
        ]
        for failure in failures:
            with self.subTest(kind=type(failure).__name__), \
                    mock.patch.object(notifier.urllib.request, "urlopen", side_effect=failure), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(notifier._fetch_json(notifier.PROMETHEUS_ALERTS_URL))

        payloads = (b"not json", b'{"status":"success","data":', b"\xff")
        for payload in payloads:
            with self.subTest(payload=payload[:12]), \
                    mock.patch.object(
                        notifier.urllib.request, "urlopen",
                        return_value=FakeResponse(payload),
                    ), contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(notifier._fetch_json(notifier.PROMETHEUS_ALERTS_URL))

    def test_empty_alerts_are_genuine_zero_only_with_fresh_rule_evidence(self):
        rules = {
            "groups": [{
                "interval": 15,
                "lastEvaluation": "2026-07-15T04:00:00Z",
                "rules": [{"type": "alerting", "health": "ok"}],
            }],
        }
        runtime = {
            "serverTime": "2026-07-15T04:00:10Z",
            "startTime": "2026-07-15T03:00:00Z",
        }
        responses = [
            {"status": "success", "data": {"alerts": []}},
            {"status": "success", "data": rules},
            {"status": "success", "data": runtime},
        ]
        with mock.patch.object(
            notifier.urllib.request,
            "urlopen",
            side_effect=[FakeResponse(json.dumps(item).encode()) for item in responses],
        ):
            result = notifier.fetch_alert_snapshot()
        self.assertTrue(result.query_ok)
        self.assertTrue(result.state_complete)
        self.assertTrue(result.resolutions_enabled)
        self.assertEqual(result.firing, [])

    def test_stale_rule_evaluation_and_restart_grace_hold_resolutions(self):
        rules = {
            "groups": [{
                "interval": 15,
                "lastEvaluation": "2026-07-15T03:58:00Z",
                "rules": [{"type": "alerting", "health": "ok"}],
            }],
        }
        runtime = {
            "serverTime": "2026-07-15T04:00:10Z",
            "startTime": "2026-07-15T04:00:00Z",
        }
        fresh, uptime, reason = notifier._validate_rule_evidence(rules, runtime)
        self.assertFalse(fresh)
        self.assertEqual(uptime, 10)
        self.assertIn("stale", reason)

        rules["groups"][0]["lastEvaluation"] = "2026-07-15T04:00:05Z"
        fresh, uptime, _ = notifier._validate_rule_evidence(rules, runtime)
        self.assertTrue(fresh)
        self.assertLess(uptime, notifier.PROMETHEUS_RESTART_RESOLUTION_GRACE)
        result = snapshot([], uptime=uptime)
        self.assertTrue(result.state_complete)
        self.assertFalse(result.resolutions_enabled)

    def test_transient_rule_evaluation_gap_is_rechecked_before_failing_closed(self):
        alerts = {"alerts": []}
        runtime = {
            "serverTime": "2026-07-15T04:00:10Z",
            "startTime": "2026-07-15T03:00:00Z",
        }
        incomplete = {
            "groups": [{
                "interval": 15,
                "rules": [{"type": "alerting", "health": "unknown"}],
            }],
        }
        complete = {
            "groups": [{
                "interval": 15,
                "lastEvaluation": "2026-07-15T04:00:05Z",
                "rules": [{"type": "alerting", "health": "ok"}],
            }],
        }
        with mock.patch.object(
            notifier,
            "_fetch_json",
            side_effect=[alerts, incomplete, runtime, complete, runtime],
        ) as fetch, mock.patch.object(notifier.time, "sleep") as sleep:
            result = notifier.fetch_alert_snapshot()
        self.assertTrue(result.state_complete)
        self.assertTrue(result.resolutions_enabled)
        self.assertEqual(fetch.call_count, 5)
        sleep.assert_called_once_with(notifier.RULE_EVIDENCE_RECHECK_DELAY)

    def test_persistent_rule_evaluation_gap_remains_bounded_and_incomplete(self):
        alerts = {"alerts": []}
        runtime = {
            "serverTime": "2026-07-15T04:00:10Z",
            "startTime": "2026-07-15T03:00:00Z",
        }
        incomplete = {
            "groups": [{
                "interval": 15,
                "rules": [{"type": "alerting", "health": "unknown"}],
            }],
        }
        responses = [alerts]
        for _attempt in range(notifier.RULE_EVIDENCE_RECHECK_ATTEMPTS):
            responses.extend((incomplete, runtime))
        with mock.patch.object(
            notifier, "_fetch_json", side_effect=responses
        ) as fetch, mock.patch.object(notifier.time, "sleep") as sleep, \
                contextlib.redirect_stderr(io.StringIO()):
            result = notifier.fetch_alert_snapshot()
        self.assertFalse(result.state_complete)
        self.assertEqual(fetch.call_count, 1 + 2 * notifier.RULE_EVIDENCE_RECHECK_ATTEMPTS)
        self.assertEqual(
            sleep.call_count, notifier.RULE_EVIDENCE_RECHECK_ATTEMPTS - 1
        )

    def test_unhealthy_or_missing_alert_rules_fail_closed_without_count_constant(self):
        runtime = {
            "serverTime": "2026-07-15T04:00:10Z",
            "startTime": "2026-07-15T03:00:00Z",
        }
        for rules in (
            {"groups": []},
            {"groups": [{
                "interval": 15,
                "lastEvaluation": "2026-07-15T04:00:05Z",
                "rules": [],
            }]},
            {"groups": [{
                "interval": 15,
                "lastEvaluation": "2026-07-15T04:00:05Z",
                "rules": [{"type": "alerting", "health": "err"}],
            }]},
        ):
            with self.subTest(rules=rules):
                self.assertFalse(notifier._validate_rule_evidence(rules, runtime)[0])

    def test_one_malformed_alert_is_isolated_from_valid_alerts(self):
        firing, pending, invalid = notifier._parse_alerts({"alerts": [
            {"state": "firing", "labels": {"alertname": "Valid"}},
            {"state": "firing", "labels": "malformed"},
            {"state": "pending", "labels": {"alertname": "Waiting"}},
        ]})
        self.assertEqual([item["labels"]["alertname"] for item in firing], ["Valid"])
        self.assertEqual([item["labels"]["alertname"] for item in pending], ["Waiting"])
        self.assertEqual(invalid, 1)


class NtfyFailureMatrixTests(unittest.TestCase):
    def _http_error(self, code, headers=None):
        return urllib.error.HTTPError(
            "https://example.invalid/redacted", code, "synthetic", headers or {}, None
        )

    def test_200_202_and_malformed_success_body_are_server_acceptance(self):
        for status, body in ((200, b"not-json"), (202, b'{"truncated"')):
            with self.subTest(status=status), mock.patch.object(
                notifier.urllib.request, "urlopen",
                return_value=FakeResponse(body, status=status),
            ):
                result = notifier.push_ntfy(
                    "synthetic-topic", "FIRING: Fixture", "synthetic body", "high",
                    sequence="synthetic-sequence",
                )
            self.assertTrue(result.accepted)
            self.assertEqual(result.attempts, 1)

    def test_permanent_400_401_403_404_rejections_are_not_retried(self):
        for code in (400, 401, 403, 404):
            opener = mock.Mock(side_effect=self._http_error(code))
            with self.subTest(code=code), mock.patch.object(
                notifier.urllib.request, "urlopen", opener
            ), contextlib.redirect_stderr(io.StringIO()):
                result = notifier.push_ntfy(
                    "synthetic-topic", "FIRING: Fixture", "synthetic body", "high",
                    sleep=lambda _seconds: self.fail("permanent errors must not sleep"),
                )
            self.assertFalse(result.accepted)
            self.assertTrue(result.permanent)
            self.assertEqual(opener.call_count, 1)

    def test_429_retry_after_is_honored_or_deferred_without_early_retry(self):
        sleeps = []
        short = mock.Mock(side_effect=[
            self._http_error(429, {"Retry-After": "2"}),
            FakeResponse(status=202),
        ])
        with mock.patch.object(notifier.urllib.request, "urlopen", short), \
                contextlib.redirect_stderr(io.StringIO()):
            result = notifier.push_ntfy(
                "synthetic-topic", "FIRING: Fixture", "synthetic body", "high",
                sleep=sleeps.append,
            )
        self.assertTrue(result.accepted)
        self.assertEqual(sleeps, [2])
        self.assertEqual(short.call_count, 2)

        long = mock.Mock(side_effect=self._http_error(429, {"Retry-After": "120"}))
        with mock.patch.object(notifier.urllib.request, "urlopen", long), \
                contextlib.redirect_stderr(io.StringIO()):
            result = notifier.push_ntfy(
                "synthetic-topic", "FIRING: Fixture", "synthetic body", "high",
                sleep=lambda _seconds: self.fail("long Retry-After must be deferred"),
            )
        self.assertFalse(result.accepted)
        self.assertEqual(result.retry_after, 120)
        self.assertEqual(long.call_count, 1)

    def test_500_502_503_and_transport_errors_have_bounded_attempts(self):
        for failure in (
            self._http_error(500), self._http_error(502), self._http_error(503),
            urllib.error.URLError(ConnectionResetError()),
        ):
            opener = mock.Mock(side_effect=failure)
            with self.subTest(kind=repr(failure)), mock.patch.object(
                notifier.urllib.request, "urlopen", opener
            ), contextlib.redirect_stderr(io.StringIO()):
                result = notifier.push_ntfy(
                    "synthetic-topic", "FIRING: Fixture", "synthetic body", "high",
                    sleep=lambda _seconds: None,
                )
            self.assertFalse(result.accepted)
            self.assertEqual(opener.call_count, notifier.MAX_DELIVERY_ATTEMPTS)

    def test_timeout_reset_are_unknown_but_dns_refusal_tls_are_known_not_accepted(self):
        cases = [
            (urllib.error.URLError(TimeoutError()), True),
            (urllib.error.URLError(ConnectionResetError()), True),
            (urllib.error.URLError(socket.gaierror()), False),
            (urllib.error.URLError(ConnectionRefusedError()), False),
            (urllib.error.URLError(ssl.SSLCertVerificationError()), False),
        ]
        for failure, expected_unknown in cases:
            with self.subTest(kind=type(failure.reason).__name__), mock.patch.object(
                notifier.urllib.request, "urlopen", side_effect=failure
            ), mock.patch.object(notifier, "MAX_DELIVERY_ATTEMPTS", 1), \
                    contextlib.redirect_stderr(io.StringIO()):
                result = notifier.push_ntfy(
                    "synthetic-topic", "FIRING: Fixture", "synthetic body", "high"
                )
            self.assertEqual(result.outcome_unknown, expected_unknown)

    def test_topic_url_and_message_body_never_enter_errors(self):
        topic = "synthetic-secret-topic-value"
        body = "synthetic-secret-message-body"
        error = urllib.error.URLError(
            f"request failed for https://ntfy.sh/{topic} containing {body}"
        )
        stream = io.StringIO()
        with mock.patch.object(notifier.urllib.request, "urlopen", side_effect=error), \
                mock.patch.object(notifier, "MAX_DELIVERY_ATTEMPTS", 1), \
                contextlib.redirect_stderr(stream):
            notifier.push_ntfy(topic, "FIRING: Fixture", body, "high")
        self.assertNotIn(topic, stream.getvalue())
        self.assertNotIn(body, stream.getvalue())

    def test_isolated_http_endpoint_receives_deterministic_sequence(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append({
                    "path": self.path,
                    "sequence": self.headers.get("X-Sequence-ID"),
                    "body": self.rfile.read(length),
                })
                self.send_response(202)
                self.end_headers()
                self.wfile.write(b"synthetic malformed response body")

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with mock.patch.object(notifier, "NTFY_SERVER", base):
                result = notifier.push_ntfy(
                    "synthetic-topic", "FIRING: Fixture", "synthetic body", "high",
                    sequence="stable-fixture-sequence",
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertTrue(result.accepted)
        self.assertEqual(received[0]["path"], "/synthetic-topic")
        self.assertEqual(received[0]["sequence"], "stable-fixture-sequence")
        self.assertEqual(received[0]["body"], b"synthetic body")


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = self.temp.name
        replacements = {
            "STATE_DIR": os.path.join(root, "state"),
            "STATE_FILE": os.path.join(root, "state", "notified-alerts.json"),
            "LOCK_FILE": os.path.join(root, "state", "notified-alerts.lock"),
            "METRICS_DIR": os.path.join(root, "metrics"),
        }
        for name, value in replacements.items():
            patcher = mock.patch.object(notifier, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = 1_800_000_000

    def ready_state(self):
        state = notifier._new_state(self.now)
        state[notifier.META_KEY]["baseline_pending"] = False
        return state, notifier.default_metrics()

    def process(self, state, metrics, current, *, pending=None, invalid=0,
                uptime=3600, query_ok=True, evidence_fresh=True, topic="synthetic-topic"):
        snap = snapshot(
            current, pending, invalid, query_ok=query_ok,
            evidence_fresh=evidence_fresh, uptime=uptime,
        )
        observation = notifier._observation(snap, topic is not None)
        failed, failures = notifier.process_snapshot(
            state, snap, topic, metrics, observation, self.now
        )
        return failed, failures, observation

    def test_pending_never_notifies_and_firing_dedupes_then_resolves_once(self):
        state, metrics = self.ready_state()
        fixture = alert(instance="127.0.0.1:9100")
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=202)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            self.process(state, metrics, [], pending=[
                alert("Pending", state="pending")
            ])
            self.assertEqual(push.call_count, 0)

            self.process(state, metrics, [fixture])
            self.assertEqual(push.call_count, 1)
            self.assertTrue(push.call_args.args[1].startswith("FIRING:"))

            self.process(state, metrics, [fixture])
            self.assertEqual(push.call_count, 1)

            self.process(state, metrics, [])
            self.assertEqual(push.call_count, 1)
            self.process(state, metrics, [])
            self.assertEqual(push.call_count, 2)
            self.assertTrue(push.call_args.args[1].startswith("RESOLVED:"))
            self.process(state, metrics, [])
            self.assertEqual(push.call_count, 2)

    def test_label_order_is_stable_and_changed_labels_create_new_identity(self):
        state, metrics = self.ready_state()
        first = alert(instance="one", job="node")
        reordered = {
            **first,
            "labels": dict(reversed(list(first["labels"].items()))),
        }
        changed = alert(instance="two", job="node")
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=200)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            self.process(state, metrics, [first])
            self.process(state, metrics, [reordered])
            self.assertEqual(push.call_count, 1)
            self.process(state, metrics, [changed])
            self.assertEqual(push.call_count, 2)
            self.process(state, metrics, [changed])
            self.assertEqual(push.call_count, 3)  # old identity resolves on confirmation

    def test_api_failure_malformed_entry_restart_and_dependency_hold_resolutions(self):
        state, metrics = self.ready_state()
        original = alert("UpstreamDNSPathDown", job="blackbox_upstream")
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=200)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            self.process(state, metrics, [original])
            self.assertEqual(push.call_count, 1)

            # Whole-API failure: no resolution.
            self.process(state, metrics, [], query_ok=False, evidence_fresh=False)
            self.assertEqual(push.call_count, 1)

            # Valid API plus one malformed entry: additions remain possible,
            # but the known alert cannot resolve.
            self.process(state, metrics, [alert("Another")], invalid=1)
            self.assertEqual(push.call_count, 2)

            # Fresh Prometheus restart and a pending dependency blocker both hold.
            self.process(state, metrics, [], uptime=30)
            self.process(
                state, metrics, [],
                pending=[alert("BlackboxExporterDown", state="pending")],
            )
            self.assertEqual(push.call_count, 2)

            # Once complete evidence returns, two qualified polls are required.
            self.process(state, metrics, [])
            titles = [call.args[1] for call in push.call_args_list]
            self.assertNotIn("RESOLVED: UpstreamDNSPathDown", titles)
            self.process(state, metrics, [])
            titles = [call.args[1] for call in push.call_args_list]
            self.assertIn("RESOLVED: UpstreamDNSPathDown", titles)

    def test_missing_breadcrumb_and_textfile_error_block_dependent_resolution(self):
        state, metrics = self.ready_state()
        original = alert("BlocklistUpdateStale", task="blocklist")
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=200)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            self.process(state, metrics, [original])
            for blocker in ("BlocklistBreadcrumbAbsent", "TextfileScrapeError"):
                self.process(
                    state, metrics, [],
                    pending=[alert(blocker, state="pending")],
                )
                self.process(
                    state, metrics, [],
                    pending=[alert(blocker, state="pending")],
                )
            titles = [call.args[1] for call in push.call_args_list]
            self.assertNotIn("RESOLVED: BlocklistUpdateStale", titles)

    def test_persistent_malformed_state_warns_directly_without_discarding_valid_firing(self):
        state, metrics = self.ready_state()
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=200)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            for _index in range(notifier.UNREACHABLE_THRESHOLD):
                self.process(state, metrics, [alert("ValidDuringPartial")], invalid=1)
                self.now += 120
        titles = [call.args[1] for call in push.call_args_list]
        self.assertIn("FIRING: ValidDuringPartial", titles)
        self.assertIn("FIRING: PrometheusAlertStateUnavailable", titles)

    def test_missing_or_corrupt_state_adopts_active_alerts_without_replay_storm(self):
        state = notifier._new_state(self.now)
        metrics = notifier.default_metrics()
        active = [alert(f"Fixture{index}", instance=str(index)) for index in range(20)]
        with mock.patch.object(notifier, "push_ntfy") as push:
            self.process(state, metrics, active)
            self.assertEqual(push.call_count, 0)
            self.assertFalse(state[notifier.META_KEY]["baseline_pending"])
            statuses = {
                entry.get("status") for key, entry in state.items()
                if key.startswith(notifier.ALERT_KEY_PREFIX)
            }
            self.assertEqual(statuses, {"observed_baseline"})
            self.process(state, metrics, active)
            self.assertEqual(push.call_count, 0)

    def test_parseable_structural_corruption_is_quarantined_without_notification(self):
        state, _metrics = self.ready_state()
        digest = "a" * 64
        state[f"{notifier.ALERT_KEY_PREFIX}{digest}"] = notifier._new_alert_entry(
            alert("CorruptFixture"), digest, self.now, status="firing_notified"
        )
        notifier.save_state(state)
        parsed = json.loads(Path(notifier.STATE_FILE).read_text())
        parsed[f"{notifier.ALERT_KEY_PREFIX}{digest}"]["safe_labels"] = {
            "instance": "synthetic-private-value\n"
        }
        Path(notifier.STATE_FILE).write_text(json.dumps(parsed), encoding="utf-8")

        snap = snapshot([])
        with mock.patch.object(notifier, "fetch_alert_snapshot", return_value=snap), \
                mock.patch.object(notifier, "read_topic", return_value="synthetic-topic"), \
                mock.patch.object(notifier, "push_ntfy") as push, \
                mock.patch.object(notifier, "write_metrics", return_value=True):
            self.assertEqual(notifier.run_once(), 0)
        self.assertEqual(push.call_count, 0)
        self.assertTrue(list(Path(notifier.STATE_DIR).glob("*.invalid-*")))
        self.assertNotIn("synthetic-private-value", Path(notifier.STATE_FILE).read_text())

    def test_legacy_outage_state_migrates_all_delivery_fields(self):
        legacy = {
            notifier.UNREACHABLE_KEY: {"failures": 2, "notified": False},
            notifier.METRICS_KEY: notifier.default_metrics(),
        }
        migrated = notifier._migrate_state(legacy, self.now)
        outage = migrated[notifier.UNREACHABLE_KEY]
        self.assertEqual(outage["sequence_id"], "unbound-prometheus-alert-state")
        self.assertFalse(outage["blocked"])
        self.assertFalse(outage["resolution_blocked"])
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=202)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            self.process(
                migrated,
                notifier.get_metrics(migrated),
                [],
                query_ok=False,
                evidence_fresh=False,
            )
        self.assertEqual(push.call_count, 1)
        self.assertEqual(
            push.call_args.kwargs["sequence"], "unbound-prometheus-alert-state"
        )

    def test_dry_run_does_not_read_lock_repair_or_quarantine_state(self):
        Path(notifier.STATE_DIR).mkdir(mode=0o700)
        Path(notifier.STATE_FILE).write_text("{synthetic invalid", encoding="utf-8")
        before = Path(notifier.STATE_FILE).read_bytes()
        snap = snapshot([])
        with mock.patch.object(notifier, "fetch_alert_snapshot", return_value=snap), \
                mock.patch.object(notifier, "read_topic", return_value="synthetic-topic"), \
                mock.patch.object(notifier, "load_state") as load, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(notifier.run_once(dry_run=True), 0)
        self.assertEqual(load.call_count, 0)
        self.assertEqual(Path(notifier.STATE_FILE).read_bytes(), before)
        self.assertFalse(Path(notifier.LOCK_FILE).exists())
        self.assertFalse(list(Path(notifier.STATE_DIR).glob("*.invalid-*")))

        with mock.patch.object(sys, "argv", ["notifier", "--dry-run"]), \
                mock.patch.object(notifier, "run_once", return_value=0), \
                mock.patch.object(notifier, "RunLock") as lock:
            with self.assertRaises(SystemExit) as exited:
                notifier.main()
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(lock.call_count, 0)

    def test_restart_with_durable_state_does_not_reannounce(self):
        state, metrics = self.ready_state()
        fixture = alert(instance="stable")
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=200)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            self.process(state, metrics, [fixture])
            self.assertEqual(push.call_count, 1)
        notifier.save_state(state)
        restarted, status_value = notifier.load_state()
        self.assertEqual(status_value, "ok")
        restarted_metrics = notifier.get_metrics(restarted)
        with mock.patch.object(notifier, "push_ntfy") as push:
            self.process(restarted, restarted_metrics, [fixture])
            self.assertEqual(push.call_count, 0)

    def test_state_and_notification_exclude_private_labels_and_annotations(self):
        state, metrics = self.ready_state()
        private = alert(
            client="synthetic-private-client",
            instance="allowed-instance",
            mountpoint="/Volumes/Synthetic Private Household Disk",
            file="/private/synthetic/path/fixture.prom",
            token="synthetic-private-token",
        )
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=200)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            self.process(state, metrics, [private])
        message = push.call_args.args[2]
        self.assertIn("instance=allowed-instance", message)
        self.assertIn("mountpoint=(non-root filesystem)", message)
        self.assertIn("file=fixture.prom", message)
        self.assertNotIn("synthetic-private-client", message)
        self.assertNotIn("synthetic-private-token", message)
        self.assertNotIn("Synthetic Private Household Disk", message)
        self.assertNotIn("/private/synthetic/path", message)
        self.assertNotIn("synthetic annotation", message)
        rendered = Path(notifier.STATE_FILE).read_text()
        self.assertNotIn("synthetic-private-client", rendered)
        self.assertNotIn("synthetic-private-token", rendered)

    def test_per_run_rate_limit_and_permanent_failure_prevent_storms(self):
        state, metrics = self.ready_state()
        active = [alert(f"Fixture{index}") for index in range(15)]
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=200)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            self.process(state, metrics, active)
            self.assertEqual(push.call_count, notifier.MAX_EVENTS_PER_RUN)
            self.now += 120
            self.process(state, metrics, active)
            self.assertEqual(push.call_count, 15)

        state, metrics = self.ready_state()
        rejected = notifier.DeliveryResult(
            False, permanent=True, attempts=1, status_code=403
        )
        with mock.patch.object(notifier, "push_ntfy", return_value=rejected) as push:
            self.process(state, metrics, [alert("Blocked")])
            _failed, failures, observation = self.process(
                state, metrics, [alert("Blocked")]
            )
            self.assertEqual(push.call_count, 1)
            self.assertEqual(metrics["permanent_rejections_total"], 1)
            self.assertEqual(metrics["fail_total"], 1)
            self.assertEqual(failures, 1)
            self.assertEqual(observation["blocked_transitions"], 1)

    def test_direct_recovery_shares_event_cap_and_rejection_stays_unhealthy(self):
        state, metrics = self.ready_state()
        state[notifier.UNREACHABLE_KEY] = {
            "failures": 3,
            "notified": True,
            "blocked": False,
            "resolution_blocked": False,
            "sequence_id": "unbound-prometheus-alert-state",
            "next_attempt_at": 0,
            "firing_failure_counted": False,
            "resolution_failure_counted": False,
        }
        active = [alert(f"CapFixture{index}") for index in range(10)]
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=202)
        with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as push:
            _failed, failures, _observation = self.process(state, metrics, active)
        self.assertEqual(push.call_count, notifier.MAX_EVENTS_PER_RUN)
        self.assertEqual(failures, 1)  # one rule transition remains queued

        state, _metrics = self.ready_state()
        state[notifier.UNREACHABLE_KEY] = {
            "failures": 3,
            "notified": True,
            "blocked": False,
            "resolution_blocked": False,
            "sequence_id": "unbound-prometheus-alert-state",
            "next_attempt_at": 0,
            "firing_failure_counted": False,
            "resolution_failure_counted": False,
        }
        notifier.save_state(state)
        transient = notifier.DeliveryResult(False, attempts=3, retry_after=300)
        snap = snapshot([])
        with mock.patch.object(notifier, "fetch_alert_snapshot", return_value=snap), \
                mock.patch.object(notifier, "read_topic", return_value="synthetic-topic"), \
                mock.patch.object(notifier, "push_ntfy", return_value=transient) as push, \
                mock.patch.object(notifier, "write_metrics", return_value=True), \
                mock.patch.object(notifier.time, "time", return_value=self.now):
            self.assertEqual(notifier.run_once(), 1)
        persisted, status_value = notifier.load_state()
        persisted = notifier._migrate_state(persisted, self.now)
        self.assertEqual(status_value, "ok")
        self.assertEqual(
            persisted[notifier.UNREACHABLE_KEY]["next_attempt_at"], self.now + 300
        )
        self.assertEqual(persisted[notifier.METRICS_KEY]["consecutive_failures"], 1)
        self.assertEqual(persisted[notifier.METRICS_KEY]["fail_total"], 1)

        with mock.patch.object(notifier, "fetch_alert_snapshot", return_value=snap), \
                mock.patch.object(notifier, "read_topic", return_value="synthetic-topic"), \
                mock.patch.object(notifier, "push_ntfy") as deferred_push, \
                mock.patch.object(notifier, "write_metrics", return_value=True), \
                mock.patch.object(notifier.time, "time", return_value=self.now + 120):
            self.assertEqual(notifier.run_once(), 1)
        self.assertEqual(deferred_push.call_count, 0)
        persisted, _status = notifier.load_state()
        persisted = notifier._migrate_state(persisted, self.now + 120)
        self.assertEqual(persisted[notifier.METRICS_KEY]["consecutive_failures"], 2)
        self.assertEqual(persisted[notifier.METRICS_KEY]["fail_total"], 1)

        state, metrics = self.ready_state()
        state[notifier.UNREACHABLE_KEY] = {
            "failures": 3,
            "notified": True,
            "blocked": False,
            "resolution_blocked": False,
            "sequence_id": "unbound-prometheus-alert-state",
            "next_attempt_at": 0,
            "firing_failure_counted": False,
            "resolution_failure_counted": False,
        }
        permanent = notifier.DeliveryResult(
            False, permanent=True, attempts=1, status_code=403
        )
        with mock.patch.object(notifier, "push_ntfy", return_value=permanent) as push:
            _failed, failures, observation = self.process(state, metrics, [])
            self.assertEqual(failures, 1)
            self.assertEqual(observation["blocked_transitions"], 1)
            self.process(state, metrics, [])
        self.assertEqual(push.call_count, 1)
        self.assertTrue(state[notifier.UNREACHABLE_KEY]["resolution_blocked"])
        self.assertEqual(metrics["fail_total"], 1)

    def test_transient_failure_is_deferred_and_bounded_across_runs(self):
        state, metrics = self.ready_state()
        transient = notifier.DeliveryResult(
            False, attempts=3, retry_after=300, outcome_unknown=True
        )
        accepted = notifier.DeliveryResult(True, attempts=1, status_code=202)
        with mock.patch.object(
            notifier, "push_ntfy", side_effect=[transient, accepted]
        ) as push:
            self.process(state, metrics, [alert("Retry")])
            self.assertEqual(push.call_count, 1)
            self.now += 120
            self.process(state, metrics, [alert("Retry")])
            self.assertEqual(push.call_count, 1)
            self.now += 180
            self.process(state, metrics, [alert("Retry")])
            self.assertEqual(push.call_count, 2)
        self.assertEqual(metrics["delivery_unknown_total"], 1)
        self.assertEqual(metrics["retry_attempts_total"], 2)

    def test_crash_windows_persist_intent_and_reuse_sequence_id(self):
        for crash_point in ("before_result", "after_acceptance"):
            with self.subTest(crash_point=crash_point):
                state, metrics = self.ready_state()
                fixture = alert("CrashFixture")
                sequence_ids = []

                def crash_push(*_args, **kwargs):
                    sequence_ids.append(kwargs.get("sequence"))
                    raise SystemExit("synthetic crash")

                patches = [mock.patch.object(notifier, "push_ntfy", side_effect=crash_push)]
                if crash_point == "after_acceptance":
                    accepted = notifier.DeliveryResult(True, attempts=1, status_code=202)

                    def accepted_push(*_args, **kwargs):
                        sequence_ids.append(kwargs.get("sequence"))
                        return accepted

                    patches = [
                        mock.patch.object(notifier, "push_ntfy", side_effect=accepted_push),
                        mock.patch.object(
                            notifier, "_record_delivery_result",
                            side_effect=SystemExit("synthetic post-acceptance crash"),
                        ),
                    ]

                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaises(SystemExit):
                        self.process(state, metrics, [fixture])

                persisted, status_value = notifier.load_state()
                self.assertEqual(status_value, "ok")
                entries = [
                    item for key, item in persisted.items()
                    if key.startswith(notifier.ALERT_KEY_PREFIX)
                ]
                self.assertEqual(entries[0]["status"], "pending_firing")

                accepted = notifier.DeliveryResult(True, attempts=1, status_code=202)
                with mock.patch.object(notifier, "push_ntfy", return_value=accepted) as retry:
                    self.process(
                        persisted, notifier.get_metrics(persisted), [fixture]
                    )
                sequence_ids.append(retry.call_args.kwargs.get("sequence"))
                self.assertEqual(len(set(sequence_ids)), 1)

    def test_unwritable_state_prevents_send_and_metric_failure_is_reported(self):
        state, metrics = self.ready_state()
        with mock.patch.object(
            notifier, "save_state", side_effect=notifier.StateError("synthetic")
        ), mock.patch.object(notifier, "push_ntfy") as push, \
                contextlib.redirect_stderr(io.StringIO()):
            failed, failures, observation = self.process(
                state, metrics, [alert("NoState")]
            )
        self.assertTrue(failed)
        self.assertEqual(failures, 1)
        self.assertFalse(observation["persistence_ok"])
        self.assertEqual(push.call_count, 0)

        with mock.patch.object(
            notifier, "_atomic_write", side_effect=notifier.StateError("synthetic")
        ), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(notifier.write_metrics(metrics, query_ok=True, now=self.now))

    def test_atomic_state_permissions_temp_cleanup_and_clock_safety(self):
        state, metrics = self.ready_state()
        notifier.save_state(state)
        self.assertFalse(list(Path(notifier.STATE_DIR).glob("*.tmp-*")))
        if os.name != "nt":
            self.assertEqual(Path(notifier.STATE_FILE).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(notifier.STATE_DIR).stat().st_mode & 0o777, 0o700)

        future_state = {
            notifier.METRICS_KEY: {
                "last_success": self.now + 1000,
                "last_failure": self.now + 1000,
            }
        }
        parsed = notifier.get_metrics(future_state, now=self.now)
        self.assertEqual(parsed["last_success"], 0)
        self.assertEqual(parsed["last_failure"], 0)
        self.assertEqual(parsed["state_errors_total"], 1)

    def test_overlap_lock_allows_only_one_process(self):
        with notifier.RunLock(notifier.LOCK_FILE):
            with self.assertRaises(notifier.OverlapError):
                with notifier.RunLock(notifier.LOCK_FILE):
                    pass


class OperatorHelperContractTests(unittest.TestCase):
    def test_helper_quiesces_and_restores_exact_state_on_failure(self):
        helper = (
            ROOT / "tools" / "ntfy-delivery-hardening-operator.sh"
        ).read_text(encoding="utf-8")
        for required in (
            "require_clean_checkout",
            'launchctl disable "$NOTIFIER_JOB"',
            'launchctl bootout "$NOTIFIER_JOB"',
            "recover_failed_operation",
            'restore_backup_files "$RECOVERY_BACKUP"',
            'touch "$backup/state-present"',
            'touch "$backup/state-absent"',
            'rm -f -- "$STATE_FILE"',
            'install_atomic_user "$backup/notified-alerts.json"',
            "rollback-guard-",
        ):
            with self.subTest(required=required):
                self.assertIn(required, helper)


if __name__ == "__main__":
    unittest.main()
