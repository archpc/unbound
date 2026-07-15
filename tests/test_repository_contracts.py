#!/usr/bin/env python3
"""Offline structural contracts for deployable repository artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "deployment-manifest.json"
DASHBOARD_DIR = ROOT / "config" / "grafana" / "dashboards"

FORBIDDEN_REPOSITORY_PATHS = (
    "unbound-etc/",
    "prometheus-etc/",
    "grafana-etc/",
    "warmup/build-warm-list.sh",
    "warmup/check-blocked.sh",
    "warmup/warm-at-boot.sh",
    "warmup/warm-unbound-cache.sh",
    "usr-local-sbin/",
    "etc-newsyslog.d/",
    "etc-ssh-sshd_config.d/",
)

FORBIDDEN_RUNTIME_ARGUMENTS = (
    "/opt/homebrew/etc/unbound/update-blocklist.sh",
    "/opt/homebrew/etc/unbound/blocklist-tui.sh",
    "/Users/localadmin/monitoring/apple_content_cache_exporter.py",
    "/Users/localadmin/monitoring/unbound_client_rpz_exporter.py",
    "/Users/localadmin/monitoring/prometheus_alert_notifier.py",
    "/Users/localadmin/warmup/build-warm-list.sh",
    "/Users/localadmin/warmup/check-blocked.sh",
    "/Users/localadmin/warmup/warm-at-boot.sh",
    "/Users/localadmin/warmup/warm-unbound-cache.sh",
    "/usr/local/sbin/unbound-refresh-hints",
    "/usr/local/sbin/screenshare-on",
    "/usr/local/sbin/screenshare-off",
)


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


class RepositoryContractTests(unittest.TestCase):
    def test_manifest_covers_every_deployable_source(self):
        manifest = load_manifest()
        entries = manifest["entries"]
        sources = {entry.get("source") for entry in entries if entry.get("source")}

        tracked = subprocess.run(
            ["git", "ls-files", "config", "launchd", "scripts"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        deployable = {
            relative
            for relative in tracked
            if Path(relative).name != ".gitkeep" and (ROOT / relative).is_file()
        }
        # Include new worktree files before they are committed so validation
        # cannot miss an untracked deployable source during development.
        for root_name in ("config", "launchd", "scripts"):
            for path in (ROOT / root_name).rglob("*"):
                if (
                    not path.is_file()
                    or "__pycache__" in path.parts
                    or path.suffix == ".pyc"
                    or path.name == ".gitkeep"
                ):
                    continue
                deployable.add(path.relative_to(ROOT).as_posix())
        self.assertEqual(deployable - sources, set())

        script_entries = {
            entry["source"]: entry
            for entry in entries
            if entry["category"] == "script"
        }
        for relative in sorted(
            path for path in deployable if path.startswith("scripts/")
        ):
            path = ROOT / relative
            with self.subTest(script=relative):
                self.assertIn(relative, script_entries)
                if os.name != "nt":
                    self.assertTrue(path.stat().st_mode & 0o111)
                self.assertTrue(script_entries[relative]["destination"].startswith("/"))
                metadata = script_entries[relative].get(
                    "metadata", manifest["metadata_defaults"]["script"]
                )
                self.assertEqual(metadata["mode"], "0755")

    def test_launchd_labels_paths_and_manifest_entries_agree(self):
        entries = load_manifest()["entries"]
        plist_entries = {
            entry["source"]: entry
            for entry in entries
            if entry["category"] == "plist"
        }
        script_destinations = {
            entry["destination"]
            for entry in entries
            if entry["category"] == "script"
        }

        for path in sorted((ROOT / "launchd").glob("*.plist")):
            relative = path.relative_to(ROOT).as_posix()
            with self.subTest(plist=relative):
                if shutil.which("plutil"):
                    completed = subprocess.run(
                        ["plutil", "-convert", "json", "-o", "-", str(path)],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    plist = json.loads(completed.stdout)
                else:
                    plist = plistlib.loads(path.read_bytes())
                self.assertEqual(plist.get("Label"), path.stem)
                self.assertIn(relative, plist_entries)
                destination = plist_entries[relative].get("destination")
                if destination is not None:
                    self.assertEqual(destination, f"/Library/LaunchDaemons/{path.name}")

                arguments = plist.get("ProgramArguments")
                self.assertIsInstance(arguments, list)
                self.assertTrue(arguments)
                self.assertTrue(arguments[0].startswith("/"))
                for argument in arguments:
                    self.assertNotIn(argument, FORBIDDEN_RUNTIME_ARGUMENTS)
                    if argument.startswith("/usr/local/libexec/unbound-stack/"):
                        self.assertIn(argument, script_destinations)

    def test_dashboards_follow_machine_verifiable_contracts(self):
        seen_uids = set()
        dashboard_paths = sorted(DASHBOARD_DIR.glob("*.json"))
        self.assertEqual(len(dashboard_paths), 5)
        dashboards = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in dashboard_paths
        ]
        expected_uids = {
            "operations-overview",
            "unbound-dns",
            "host-services",
            "apple-content-cache",
            "prometheus-monitoring",
        }
        self.assertEqual({item["uid"] for item in dashboards}, expected_uids)

        for path, dashboard in zip(dashboard_paths, dashboards):
            with self.subTest(dashboard=path.name):
                uid = dashboard.get("uid")
                self.assertIsInstance(uid, str)
                self.assertTrue(uid)
                self.assertNotIn(uid, seen_uids)
                seen_uids.add(uid)

                self.assertIs(dashboard.get("editable"), False)
                self.assertTrue(dashboard.get("description"))
                self.assertTrue(dashboard.get("links"))
                self.assertIsInstance(dashboard.get("version"), int)
                tags = dashboard.get("tags", [])
                self.assertIn("mac-mini", tags)
                self.assertIn("homelab", tags)
                self.assertLess(tags.index("mac-mini"), tags.index("homelab"))

                panel_ids = []
                for panel in dashboard.get("panels", []):
                    if panel.get("type") not in {"row", "text"}:
                        self.assertTrue(panel.get("description"))
                    panel_type = panel.get("type")
                    targets = panel.get("targets", [])
                    ref_ids = [item.get("refId") for item in targets]
                    self.assertEqual(len(ref_ids), len(set(ref_ids)))
                    if panel_type in {"stat", "bargauge", "table"}:
                        self.assertIn("noValue", panel["fieldConfig"]["defaults"])
                        for query in targets:
                            self.assertIs(query.get("instant"), True)
                            self.assertIs(query.get("range"), False)
                    if panel_type in {"timeseries", "state-timeline"}:
                        for query in targets:
                            self.assertIs(query.get("instant"), False)
                            self.assertIs(query.get("range"), True)
                    self.assertEqual(panel.get("transformations", []), [])
                    position = panel.get("gridPos", {})
                    if position:
                        self.assertGreater(position.get("w", 0), 0)
                        self.assertGreater(position.get("h", 0), 0)
                        self.assertGreaterEqual(position.get("x", -1), 0)
                        self.assertLessEqual(position["x"] + position["w"], 24)
                    for item in walk_dicts(panel):
                        if isinstance(item.get("id"), int):
                            panel_ids.append(item["id"])
                        datasource = item.get("datasource")
                        if isinstance(datasource, dict):
                            self.assertEqual(datasource.get("uid"), "prometheus")
                self.assertEqual(len(panel_ids), len(set(panel_ids)))

                rectangles = []
                for panel in dashboard.get("panels", []):
                    position = panel.get("gridPos")
                    if position:
                        rectangles.append((panel["id"], position))
                for index, (left_id, left) in enumerate(rectangles):
                    for right_id, right in rectangles[index + 1 :]:
                        horizontal = (
                            left["x"] < right["x"] + right["w"]
                            and right["x"] < left["x"] + left["w"]
                        )
                        vertical = (
                            left["y"] < right["y"] + right["h"]
                            and right["y"] < left["y"] + left["h"]
                        )
                        self.assertFalse(
                            horizontal and vertical,
                            f"overlap between panel {left_id} and {right_id}",
                        )

                for link in dashboard["links"]:
                    match = re.match(r"/d/([^?]+)", link.get("url", ""))
                    self.assertIsNotNone(match)
                    self.assertIn(match.group(1), expected_uids)
                    self.assertIs(link.get("keepTime"), True)
                    self.assertIn("${__url_time_range}", link.get("url", ""))

    def test_scrape_datasource_macro_and_provisioning_contracts_agree(self):
        prometheus = (ROOT / "config/prometheus/prometheus.yml").read_text()
        datasource = (
            ROOT / "config/grafana/provisioning/datasources/prometheus.yml"
        ).read_text()
        provider = (
            ROOT / "config/grafana/provisioning/dashboards/unbound.yml"
        ).read_text()
        validator = (ROOT / "tools/validate-dashboard-promql.py").read_text()

        self.assertRegex(prometheus, r"(?m)^\s*scrape_interval:\s*15s\s*$")
        self.assertRegex(datasource, r"(?m)^\s*timeInterval:\s*15s\s*$")
        self.assertIn("SCRAPE_INTERVAL_SECONDS = 15", validator)
        self.assertRegex(datasource, r"(?m)^\s*uid:\s*prometheus\s*$")
        self.assertRegex(datasource, r"(?m)^\s*type:\s*prometheus\s*$")
        self.assertIn("disableDeletion: false", provider)
        self.assertIn("allowUiUpdates: false", provider)
        self.assertIn("path: /opt/homebrew/etc/grafana/dashboards", provider)

    def test_prometheus_inventory_blackbox_and_time_machine_wiring(self):
        prometheus = (ROOT / "config/prometheus/prometheus.yml").read_text()
        blackbox = (ROOT / "config/prometheus/blackbox.yml").read_text()
        rules = (ROOT / "config/prometheus/prometheus.rules.yml").read_text()
        dashboards = "\n".join(
            path.read_text() for path in sorted(DASHBOARD_DIR.glob("*.json"))
        )
        exporter = (
            ROOT / "scripts/monitoring/time_machine_exporter.py"
        ).read_text()

        blocks = re.findall(
            r"(?ms)^\s*- job_name:\s*([A-Za-z0-9_]+)\s*$"
            r"(.*?)(?=^\s*- job_name:|\Z)",
            prometheus,
        )
        counts = {}
        for name, body in blocks:
            target_match = re.search(r"targets:\s*(\[[^\]]*\])", body)
            self.assertIsNotNone(target_match, name)
            counts[name] = len(json.loads(target_match.group(1)))
        expected_counts = {
            "unbound": 1,
            "prometheus": 1,
            "node": 1,
            "blackbox_upstream": 2,
            "blackbox_resolver": 1,
            "blackbox_rpz_canary": 1,
            "blackbox_grafana": 1,
        }
        self.assertEqual(counts, expected_counts)
        for name, count in expected_counts.items():
            self.assertIn(f'count(up{{job="{name}"}})', rules)
            self.assertIn(f"== bool {count}", rules)

        self.assertIn("job_name: blackbox_grafana", prometheus)
        self.assertIn("module: [http_grafana_health]", prometheus)
        self.assertIn("http_grafana_health:", blackbox)
        self.assertIn('"database"\\s*:\\s*"ok"', blackbox)
        self.assertIn('probe_success{job="blackbox_grafana"}', rules)
        self.assertIn('probe_success{job=\\"blackbox_grafana\\"}', dashboards)

        time_machine_metrics = {
            "time_machine_destination_configured",
            "time_machine_backup_running",
            "time_machine_latest_backup_available",
            "time_machine_latest_backup_timestamp_seconds",
            "time_machine_exporter_collection_success",
        }
        for metric in time_machine_metrics:
            self.assertIn(metric, exporter)
            self.assertIn(metric, rules)
        for metric in time_machine_metrics - {"time_machine_latest_backup_available"}:
            self.assertIn(metric, dashboards)

    def test_dashboard_generator_matches_checked_in_json(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "generate-grafana-dashboards.py"),
                "--check",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_inherited_dashboard_inventory_matches_audited_baseline(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "generate-monitoring-audit-inventory.py"),
                "--check",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_dashboard_provider_rejects_ui_only_edits(self):
        provider = (
            ROOT / "config/grafana/provisioning/dashboards/unbound.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("allowUiUpdates: false", provider)
        self.assertNotIn("allowUiUpdates: true", provider)

    def test_grafana_deployment_uses_unified_storage_uid_verification(self):
        deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
        self.assertIn('"group"="dashboard.grafana.app"', deployment)
        self.assertIn('resource="dashboards"', deployment)
        self.assertNotIn(
            "select uid from dashboard where is_folder=0 order by uid;",
            deployment,
        )

    def test_active_sources_do_not_reference_retired_repository_paths(self):
        manifest = load_manifest()
        active_files = {
            ROOT / entry["source"]
            for entry in manifest["entries"]
            if entry.get("source")
            and entry["category"] in {"docs", "config", "script", "plist", "dashboard"}
        }
        for path in active_files:
            text = path.read_text(encoding="utf-8")
            for retired in FORBIDDEN_REPOSITORY_PATHS:
                with self.subTest(path=path.relative_to(ROOT), retired=retired):
                    self.assertNotIn(retired, text)

    def test_validator_is_offline_and_discovers_all_tests(self):
        validator = (ROOT / "tools" / "validate-repository.sh").read_text(
            encoding="utf-8"
        )
        network_commands = re.compile(
            r"\b(?:ssh|scp|rsync|curl|wget)\b", re.IGNORECASE
        )
        self.assertIn("python3 -m unittest discover", validator)
        self.assertNotRegex(validator, network_commands)
        self.assertNotIn("verify-deployment.py", validator)


if __name__ == "__main__":
    unittest.main()
