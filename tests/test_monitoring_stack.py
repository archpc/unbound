#!/usr/bin/env python3
"""Focused stdlib tests for the runtime-critical monitoring collectors."""

import contextlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notifier = load_module("monitoring_notifier", "scripts/prometheus_alert_notifier.py")
clientstats = load_module("monitoring_clientstats", "scripts/unbound_client_rpz_exporter.py")
contentcache = load_module("monitoring_contentcache", "scripts/apple_content_cache_exporter.py")


APPLE_STATUS_FIXTURE = """Apple content cache status:
    Activated: true
    Active: true
    ActualCacheUsed: 4.9 MB
    CacheDetails: (3)
        iCloud: Zero KB
        iOS Software: 1.25 GB
        Mac \"Software\" \\ Updates: 2 MB
    CacheFree: 55.79 GB
    CacheLimit: 199 GB
    CacheStatus: OK
    CacheUsed: 1.82 GB
    MaxCachePressureLast1Hour: 12.5%
    PersonalCacheFree: 20 GB
    PersonalCacheLimit: 40 GB
    PersonalCacheUsed: Zero KB
    Port: 49344
    PublicAddress: 203.0.113.9
    RegistrationStatus: 1
    TotalBytesAreSince: 2026-07-04 14:14:49
    TotalBytesReturnedToClients: 10.5 GB
    TotalBytesStoredFromOrigin: 8.25 GB
    PrivateAddresses: (1)
        192.168.0.100
"""


def parse_exposition(lines):
    """Return HELP/TYPE/sample metadata and reject duplicate declarations."""
    helps = {}
    types = {}
    samples = []
    for line in lines:
        if line.startswith("# HELP "):
            _, _, name, help_text = line.split(" ", 3)
            if name in helps:
                raise AssertionError(f"duplicate HELP for {name}")
            helps[name] = help_text
        elif line.startswith("# TYPE "):
            _, _, name, metric_type = line.split(" ", 3)
            if name in types:
                raise AssertionError(f"duplicate TYPE for {name}")
            types[name] = metric_type
        elif line and not line.startswith("#"):
            samples.append(line.split(None, 1)[0].split("{", 1)[0])
    missing_types = sorted(set(samples) - set(types))
    if missing_types:
        raise AssertionError(f"samples missing TYPE: {missing_types}")
    return helps, types, samples


class PythonCollectorTests(unittest.TestCase):
    def test_content_cache_fixture_and_prometheus_contract(self):
        top, details = contentcache.parse_status_text(APPLE_STATUS_FIXTURE)
        self.assertEqual(top["CacheStatus"], "OK")
        self.assertEqual(details["iCloud"], "Zero KB")

        lines, warnings = contentcache.build_metrics(top, details, now=1_751_000_000)
        rendered = "\n".join(lines)
        helps, types, samples = parse_exposition(lines)

        self.assertEqual(types["apple_content_cache_returned_to_clients_bytes_total"], "counter")
        self.assertEqual(types["apple_content_cache_stored_from_origin_bytes_total"], "counter")
        self.assertIn("apple_content_cache_totals_since_seconds", samples)
        self.assertEqual(set(samples) - set(helps), set())
        self.assertNotIn("203.0.113.9", rendered)
        self.assertNotIn("192.168.0.100", rendered)
        self.assertIn('category="Mac \\"Software\\" \\\\ Updates"', rendered)
        self.assertFalse(any("TotalBytesAreSince" in warning for warning in warnings))

    def test_content_cache_does_not_invent_counter_semantics(self):
        top = {
            "TotalBytesAreSince": "not a timestamp",
            "TotalBytesReturnedToClients": "10 MB",
            "TotalBytesStoredFromOrigin": "5 MB",
        }
        lines, warnings = contentcache.build_metrics(top, {}, now=123)
        rendered = "\n".join(lines)
        _, types, _ = parse_exposition(lines)

        self.assertNotIn("apple_content_cache_returned_to_clients_bytes_total", rendered)
        self.assertNotIn("apple_content_cache_stored_from_origin_bytes_total", rendered)
        self.assertEqual(types["apple_content_cache_returned_to_clients_bytes"], "gauge")
        self.assertEqual(types["apple_content_cache_stored_from_origin_bytes"], "gauge")
        self.assertTrue(any("could not be parsed" in warning for warning in warnings))

    def test_clientstats_malformed_ndjson_is_counted_not_crashing(self):
        lines = [
            "[]",
            '"a JSON scalar"',
            '{"eventMessage": 7}',
            '{"eventMessage": "rpz: applied but structurally malformed"}',
            '{"eventMessage": "ordinary log line"}',
        ]
        blocks, last_block, unattributed, errors = clientstats.parse_ndjson(lines)
        self.assertEqual(dict(blocks), {})
        self.assertEqual(last_block, {})
        self.assertEqual(unattributed, 0)
        self.assertEqual(errors, 4)

    def test_clientstats_metrics_are_gauges_with_shared_breadcrumb(self):
        blocks, last_block, unattributed, errors = clientstats.parse_ndjson(
            clientstats._FIXTURE.strip().splitlines()
        )
        lines = clientstats.build_metrics(blocks, last_block, unattributed, errors, now=123)
        helps, types, samples = parse_exposition(lines)
        self.assertEqual(types["unbound_client_rpz_blocks_5m"], "gauge")
        self.assertEqual(types["unbound_client_last_rpz_block_seconds"], "gauge")
        self.assertEqual(types["unbound_job_last_success_timestamp_seconds"], "gauge")
        self.assertNotIn("unbound_job_last_success_timestamp_seconds", helps)
        self.assertIn("unbound_job_last_success_timestamp_seconds", samples)
        self.assertFalse(any(name.endswith("_total") for name in samples))

    def test_label_escaping(self):
        raw = 'a\\b"c\nd'
        expected = 'a\\\\b\\"c\\nd'
        self.assertEqual(contentcache.escape_label(raw), expected)
        self.assertEqual(clientstats.escape_label(raw), expected)

    def test_python_textfile_writes_replace_complete_files(self):
        with tempfile.TemporaryDirectory() as td:
            for module, filename in (
                (contentcache, "content.prom"),
                (clientstats, "client.prom"),
            ):
                path = os.path.join(td, filename)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("old partial content")
                module.write_atomic(path, ["# TYPE fixture gauge", "fixture 1"])
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "# TYPE fixture gauge\nfixture 1\n")
                self.assertFalse(os.path.exists(f"{path}.{os.getpid()}"))


class NotifierTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload=b"", status=200):
            self.payload = payload
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    def test_malformed_api_payloads_fail_closed(self):
        cases = [
            b"\xff",
            b"not json",
            json.dumps([]).encode(),
            json.dumps({"status": "success", "data": {"alerts": {}}}).encode(),
        ]
        for payload in cases:
            with self.subTest(payload=payload[:20]):
                with mock.patch.object(notifier.urllib.request, "urlopen",
                                       return_value=self.FakeResponse(payload)):
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertIsNone(notifier.fetch_firing_alerts())

    def test_api_filters_pending_and_malformed_alert_entries(self):
        payload = {
            "status": "success",
            "data": {"alerts": [
                None,
                {"state": "firing", "labels": "wrong type"},
                {"state": "pending", "labels": {"alertname": "Pending"}},
                {"state": "firing", "labels": {"alertname": "Ready"}},
            ]},
        }
        response = self.FakeResponse(json.dumps(payload).encode())
        with mock.patch.object(notifier.urllib.request, "urlopen", return_value=response):
            self.assertEqual(
                notifier.fetch_firing_alerts(),
                [{"state": "firing", "labels": {"alertname": "Ready"}}],
            )

    def test_network_failure_does_not_leak_topic(self):
        topic = "synthetic-private-topic-value"
        error = urllib.error.URLError(f"request failed for https://ntfy.sh/{topic}")
        with mock.patch.object(notifier.urllib.request, "urlopen", side_effect=error):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertFalse(notifier.push_ntfy(topic, "TEST", "message", "high", False))
            self.assertNotIn(topic, stderr.getvalue())

    def test_http_non_success_is_a_failed_delivery(self):
        response = self.FakeResponse(status=503)
        with mock.patch.object(notifier.urllib.request, "urlopen", return_value=response):
            self.assertFalse(
                notifier.push_ntfy("synthetic-topic", "TEST", "message", "high", False)
            )

    def test_corrupt_metric_values_are_discarded(self):
        state = {notifier.METRICS_KEY: {
            "last_success": math.nan,
            "last_failure": math.inf,
            "sent_total": -1,
            "fail_total": True,
            "state_errors_total": 7,
        }}
        metrics = notifier.get_metrics(state, state_had_error=True)
        self.assertEqual(metrics, {
            "last_success": 0,
            "last_failure": 0,
            "sent_total": 0,
            "fail_total": 0,
            "state_errors_total": 8,
        })

    def test_missing_topic_and_invalid_utf8_state_fail_safely(self):
        with tempfile.TemporaryDirectory() as td:
            old_topic = notifier.TOPIC_FILE
            old_state = notifier.STATE_FILE
            notifier.TOPIC_FILE = os.path.join(td, "missing-topic")
            notifier.STATE_FILE = os.path.join(td, "state.json")
            try:
                with self.assertRaises(notifier.TopicError):
                    notifier.read_topic()
                with open(notifier.STATE_FILE, "wb") as fh:
                    fh.write(b"\xff\xfe")
                state, had_error = notifier.load_state()
                self.assertEqual(state, {})
                self.assertTrue(had_error)
            finally:
                notifier.TOPIC_FILE = old_topic
                notifier.STATE_FILE = old_state

    def _isolate_files(self, td):
        old_state = notifier.STATE_FILE
        old_metrics = notifier.METRICS_DIR
        notifier.STATE_FILE = os.path.join(td, "state", "notified-alerts.json")
        notifier.METRICS_DIR = os.path.join(td, "textfile")
        self.addCleanup(setattr, notifier, "STATE_FILE", old_state)
        self.addCleanup(setattr, notifier, "METRICS_DIR", old_metrics)

    def _run_main(self, firing, delivered=True):
        with mock.patch.object(sys, "argv", ["prometheus_alert_notifier.py"]), \
                mock.patch.object(notifier, "read_topic", return_value="synthetic-topic"), \
                mock.patch.object(notifier, "fetch_firing_alerts", return_value=firing), \
                mock.patch.object(notifier, "push_ntfy", return_value=delivered) as push:
            with self.assertRaises(SystemExit) as result:
                notifier.main()
        return result.exception.code, push

    def test_state_and_counters_persist_across_fire_dedupe_resolve(self):
        alert = {
            "state": "firing",
            "labels": {"alertname": "FixtureAlert", "severity": "warning"},
            "annotations": {"summary": "fixture only"},
        }
        with tempfile.TemporaryDirectory() as td:
            self._isolate_files(td)

            code, push = self._run_main([alert])
            self.assertEqual(code, 0)
            self.assertEqual(push.call_count, 1)

            code, push = self._run_main([alert])
            self.assertEqual(code, 0)
            self.assertEqual(push.call_count, 0)

            code, push = self._run_main([])
            self.assertEqual(code, 0)
            self.assertEqual(push.call_count, 1)
            self.assertTrue(push.call_args.args[1].startswith("RESOLVED:"))

            state, had_error = notifier.load_state()
            self.assertFalse(had_error)
            self.assertEqual(state[notifier.METRICS_KEY]["sent_total"], 2)
            self.assertNotIn(notifier.fingerprint(alert), state)

            metric_path = os.path.join(notifier.METRICS_DIR, "unbound-alertnotify.prom")
            with open(metric_path, encoding="utf-8") as fh:
                metric_lines = fh.read().splitlines()
            helps, types, samples = parse_exposition(metric_lines)
            self.assertEqual(types["unbound_ntfy_notifications_sent_total"], "counter")
            self.assertEqual(types["unbound_job_last_success_timestamp_seconds"], "gauge")
            self.assertNotIn("unbound_job_last_success_timestamp_seconds", helps)
            self.assertIn("unbound_ntfy_notifications_sent_total", samples)

    def test_failed_delivery_is_counted_and_retried(self):
        first = {"state": "firing", "labels": {"alertname": "Known"}}
        second = {"state": "firing", "labels": {"alertname": "RetryMe"}}
        with tempfile.TemporaryDirectory() as td:
            self._isolate_files(td)
            self.assertEqual(self._run_main([first])[0], 0)
            state_before, _ = notifier.load_state()
            prior_success = state_before[notifier.METRICS_KEY]["last_success"]

            code, push = self._run_main([first, second], delivered=False)
            self.assertEqual(code, 1)
            self.assertEqual(push.call_count, 1)
            state, _ = notifier.load_state()
            metrics = state[notifier.METRICS_KEY]
            self.assertEqual(metrics["sent_total"], 1)
            self.assertEqual(metrics["fail_total"], 1)
            self.assertEqual(metrics["last_success"], prior_success)
            self.assertNotIn(notifier.fingerprint(second), state)

    def test_corrupt_unreachable_substate_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            self._isolate_files(td)
            os.makedirs(os.path.dirname(notifier.STATE_FILE))
            with open(notifier.STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump({notifier.UNREACHABLE_KEY: {
                    "failures": "not-an-integer", "notified": False,
                }}, fh)

            code, push = self._run_main(None)
            self.assertEqual(code, 1)
            self.assertEqual(push.call_count, 0)
            state, _ = notifier.load_state()
            self.assertEqual(state[notifier.UNREACHABLE_KEY]["failures"], 1)


class ShellAndRulesContractTests(unittest.TestCase):
    def _write_executable(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def _rebased_script(self, relative_path, destination, replacements):
        source = (ROOT / relative_path).read_text()
        for old, new in replacements:
            self.assertIn(old, source)
            source = source.replace(old, new)
        self._write_executable(destination, source)
        return destination

    def _run_script(self, script, *args, env=None):
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        return subprocess.run(
            ["/bin/bash", str(script), *args],
            text=True,
            capture_output=True,
            check=False,
            env=run_env,
            timeout=10,
        )

    def test_shell_collectors_share_breadcrumb_and_atomic_write_convention(self):
        collectors = {
            "unbound-etc/update-blocklist.sh": "blocklist",
            "warmup/build-warm-list.sh": "warmlist",
            "warmup/warm-at-boot.sh": "warmboot",
            "usr-local-sbin/unbound-refresh-hints": "roothints",
        }
        help_emitter = "echo '# HELP unbound_job_last_success_timestamp_seconds"
        type_emitter = "# TYPE unbound_job_last_success_timestamp_seconds gauge"
        for relative_path, task in collectors.items():
            with self.subTest(path=relative_path):
                source = (ROOT / relative_path).read_text()
                self.assertIn(type_emitter, source)
                self.assertIn(f'task="{task}"', source)
                self.assertNotIn(help_emitter, source)
                self.assertIn(".$$", source)
                self.assertIn("mv ", source)

    def test_shell_collectors_express_launchd_failure_exit_behavior(self):
        blocklist = (ROOT / "unbound-etc/update-blocklist.sh").read_text()
        warmboot = (ROOT / "warmup/warm-at-boot.sh").read_text()
        warmlist = (ROOT / "warmup/build-warm-list.sh").read_text()
        roothints = (ROOT / "usr-local-sbin/unbound-refresh-hints").read_text()
        self.assertIn("set -euo pipefail", blocklist)
        self.assertIn('exit "$status"', warmboot)
        self.assertIn("exit 1", warmlist)
        self.assertIn("set -euo pipefail", roothints)

    def test_blocklist_failure_preserves_breadcrumb_and_success_is_atomic(self):
        with tempfile.TemporaryDirectory() as raw_td:
            td = Path(raw_td)
            etc = td / "etc"
            blocklists = etc / "blocklists"
            metrics = td / "textfile"
            bin_dir = td / "bin"
            blocklists.mkdir(parents=True)
            metrics.mkdir()
            bin_dir.mkdir()
            (blocklists / "hagezi-normal.rpz").write_text("a.example. CNAME .\n")
            fixture = td / "downloaded.rpz"
            fixture.write_text("$TTL 300\nb.example. CNAME .\n")
            self._write_executable(bin_dir / "chown", "#!/bin/bash\nexit 0\n")
            self._write_executable(bin_dir / "curl", """#!/bin/bash
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-o' ]; then shift; out="$1"; fi
  shift
done
cp "$FIXTURE_RPZ" "$out"
""")
            prom = metrics / "unbound-blocklist.prom"
            prom.write_text(
                "# TYPE unbound_job_last_success_timestamp_seconds gauge\n"
                'unbound_job_last_success_timestamp_seconds{task="blocklist"} 77\n'
            )
            script = self._rebased_script(
                "unbound-etc/update-blocklist.sh",
                td / "update-blocklist.sh",
                [
                    ('ETC="/opt/homebrew/etc/unbound"', f'ETC="{etc}"'),
                    ('CTRL="/opt/homebrew/sbin/unbound-control"', 'CTRL="/usr/bin/true"'),
                    ('CHECK="/opt/homebrew/sbin/unbound-checkconf"', 'CHECK="/usr/bin/true"'),
                    ('METRICS_DIR="/opt/homebrew/var/lib/node_exporter/textfile"',
                     f'METRICS_DIR="{metrics}"'),
                    ('MIN_LINES=100000 ;;', 'MIN_LINES=1 ;;'),
                ],
            )
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FIXTURE_RPZ": str(fixture)}

            failed = self._run_script(script, "unknown-source", env=env)
            self.assertEqual(failed.returncode, 1)
            self.assertIn('task="blocklist"} 77', prom.read_text())
            self.assertFalse(any(metrics.glob("*.prom.*")))

            succeeded = self._run_script(script, "hagezi-normal", env=env)
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            rendered = prom.read_text()
            self.assertIn("unbound_blocklist_lines 2", rendered)
            self.assertNotIn("slot=", rendered)
            self.assertNotIn('task="blocklist"} 77', rendered)
            self.assertFalse(any(metrics.glob("*.prom.*")))

    def test_warmlist_success_and_failure_breadcrumb_behavior(self):
        with tempfile.TemporaryDirectory() as raw_td:
            td = Path(raw_td)
            metrics = td / "textfile"
            bin_dir = td / "bin"
            metrics.mkdir()
            bin_dir.mkdir()
            rpz = td / "blocklist.rpz"
            rpz.write_text("blocked.test. CNAME .\n")
            (td / "dns-warm-domains.txt").write_text("cloudflare.com\n")
            control = bin_dir / "unbound-control"
            self._write_executable(control, """#!/bin/bash
if [ "${FIXTURE_FAIL:-0}" = 1 ]; then
  echo 'permission denied' >&2
  exit 1
fi
cat <<'EOF'
msg www.example.com. IN A 300 1 0 0 1
msg api.example.com. IN A 300 1 0 0 1
msg ads.blocked.test. IN A 300 1 0 0 1
EOF
""")
            script = self._rebased_script(
                "warmup/build-warm-list.sh",
                td / "build-warm-list.sh",
                [
                    ('RPZ="/opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz"',
                     f'RPZ="{rpz}"'),
                    ('METRICS_DIR="/opt/homebrew/var/lib/node_exporter/textfile"',
                     f'METRICS_DIR="{metrics}"'),
                ],
            )
            output = td / "personal.txt"
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
            succeeded = self._run_script(script, str(output), "10", env=env)
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            domains = output.read_text()
            self.assertIn("example.com", domains)
            self.assertIn("cloudflare.com", domains)
            self.assertNotIn("blocked.test", domains)
            prom = metrics / "unbound-warmlist.prom"
            prior_metrics = prom.read_text()
            self.assertIn('task="warmlist"', prior_metrics)
            self.assertIn('kind="personal"} 1', prior_metrics)
            self.assertIn('kind="popular"} 1', prior_metrics)

            failed = self._run_script(
                script, str(td / "should-not-exist.txt"), "10",
                env={**env, "FIXTURE_FAIL": "1"},
            )
            self.assertEqual(failed.returncode, 1)
            self.assertFalse((td / "should-not-exist.txt").exists())
            self.assertEqual(prom.read_text(), prior_metrics)

    def test_warmboot_propagates_warmer_exit_and_writes_only_on_success(self):
        with tempfile.TemporaryDirectory() as raw_td:
            td = Path(raw_td)
            metrics = td / "textfile"
            bin_dir = td / "bin"
            metrics.mkdir()
            bin_dir.mkdir()
            self._write_executable(
                bin_dir / "unbound-control",
                "#!/bin/bash\nexit 0\n",
            )
            self._write_executable(
                td / "warm-unbound-cache.sh",
                '#!/bin/bash\nexit "${WARM_EXIT:-0}"\n',
            )
            (td / "dns-warm-domains.txt").write_text("example.com\n")
            script = self._rebased_script(
                "warmup/warm-at-boot.sh",
                td / "warm-at-boot.sh",
                [
                    ('export PATH="/opt/homebrew/sbin:/opt/homebrew/bin:$PATH"',
                     'export PATH="$PATH"'),
                    ('METRICS_DIR="/opt/homebrew/var/lib/node_exporter/textfile"',
                     f'METRICS_DIR="{metrics}"'),
                ],
            )
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
            succeeded = self._run_script(script, env=env)
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            prom = metrics / "unbound-warmboot.prom"
            self.assertIn('task="warmboot"', prom.read_text())
            self.assertFalse(any(metrics.glob("*.prom.*")))

            prom.unlink()
            failed = self._run_script(script, env={**env, "WARM_EXIT": "7"})
            self.assertEqual(failed.returncode, 7)
            self.assertFalse(prom.exists())

    def test_roothints_rejects_bad_download_without_replacing_destination(self):
        with tempfile.TemporaryDirectory() as raw_td:
            td = Path(raw_td)
            metrics = td / "textfile"
            bin_dir = td / "bin"
            metrics.mkdir()
            bin_dir.mkdir()
            destination = td / "root.hints"
            destination.write_text("old root hints\n")
            self._write_executable(bin_dir / "chown", "#!/bin/bash\nexit 0\n")
            self._write_executable(bin_dir / "curl", """#!/bin/bash
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-o' ]; then shift; out="$1"; fi
  shift
done
if [ "${FIXTURE_MODE:-valid}" = invalid ]; then
  echo 'bad download' > "$out"
else
  i=1
  while [ "$i" -le 13 ]; do
    echo "$i.ROOT-SERVERS.NET" >> "$out"
    i=$((i + 1))
  done
fi
""")
            script = self._rebased_script(
                "usr-local-sbin/unbound-refresh-hints",
                td / "unbound-refresh-hints",
                [
                    ('DEST="/opt/homebrew/etc/unbound/root.hints"',
                     f'DEST="{destination}"'),
                    ('METRICS_DIR="/opt/homebrew/var/lib/node_exporter/textfile"',
                     f'METRICS_DIR="{metrics}"'),
                ],
            )
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
            succeeded = self._run_script(script, env=env)
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            self.assertEqual(destination.read_text().count("ROOT-SERVERS.NET"), 13)
            prom = metrics / "unbound-roothints.prom"
            self.assertIn("unbound_roothints_records 13", prom.read_text())

            destination.write_text("known-good old file\n")
            prior_metrics = prom.read_text()
            failed = self._run_script(script, env={**env, "FIXTURE_MODE": "invalid"})
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(destination.read_text(), "known-good old file\n")
            self.assertEqual(prom.read_text(), prior_metrics)
            self.assertFalse((td / "root.hints.new").exists())

    def test_every_shared_breadcrumb_has_an_absent_series_alert(self):
        rules = (ROOT / "prometheus-etc/prometheus.rules.yml").read_text()
        for task in ("blocklist", "warmlist", "roothints", "warmboot", "alertnotify", "clientstats"):
            with self.subTest(task=task):
                expression = (
                    'absent(unbound_job_last_success_timestamp_seconds'
                    f'{{task="{task}"}})'
                )
                self.assertIn(expression, rules)
        self.assertIn(
            'absent(node_textfile_mtime_seconds{file="/opt/homebrew/var/lib/'
            'node_exporter/textfile/apple-content-cache.prom"})',
            rules,
        )

    def test_upstream_degradation_is_visible_and_alerted(self):
        rules = (ROOT / "prometheus-etc/prometheus.rules.yml").read_text()
        dashboard = (ROOT / "grafana-etc/dashboards/unbound-dns.json").read_text()
        expression = (
            'max(avg_over_time(probe_success{job="blackbox_upstream"}[10m])) < 0.95'
        )
        self.assertIn("UpstreamDNSPathDegraded", rules)
        self.assertIn(expression, rules)
        self.assertIn("Upstream success (10m)", dashboard)
        self.assertIn(
            'avg_over_time(probe_success{job=\\"blackbox_upstream\\"}[10m])',
            dashboard,
        )


if __name__ == "__main__":
    unittest.main()
