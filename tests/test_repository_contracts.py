#!/usr/bin/env python3
"""Offline structural contracts for deployable repository artifacts."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
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
            relative for relative in tracked if Path(relative).name != ".gitkeep"
        }
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
                self.assertTrue(path.stat().st_mode & 0o111)
                self.assertTrue(script_entries[relative]["destination"].startswith("/"))

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
            completed = subprocess.run(
                ["plutil", "-convert", "json", "-o", "-", str(path)],
                text=True,
                capture_output=True,
                check=False,
            )
            with self.subTest(plist=relative):
                self.assertEqual(completed.returncode, 0, completed.stderr)
                plist = json.loads(completed.stdout)
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
        for path in sorted(DASHBOARD_DIR.glob("*.json")):
            dashboard = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(dashboard=path.name):
                uid = dashboard.get("uid")
                self.assertIsInstance(uid, str)
                self.assertTrue(uid)
                self.assertNotIn(uid, seen_uids)
                seen_uids.add(uid)

                self.assertIs(dashboard.get("editable"), False)
                self.assertIsInstance(dashboard.get("version"), int)
                tags = dashboard.get("tags", [])
                self.assertIn("mac-mini", tags)
                self.assertIn("homelab", tags)
                self.assertLess(tags.index("mac-mini"), tags.index("homelab"))

                panel_ids = []
                for item in walk_dicts(dashboard.get("panels", [])):
                    if isinstance(item.get("id"), int):
                        panel_ids.append(item["id"])
                    datasource = item.get("datasource")
                    if isinstance(datasource, dict):
                        self.assertEqual(datasource.get("uid"), "prometheus")
                self.assertEqual(len(panel_ids), len(set(panel_ids)))

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
