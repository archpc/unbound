#!/usr/bin/env python3
"""Offline behavior tests for the Time Machine textfile collector."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "time_machine_exporter",
    ROOT / "scripts" / "monitoring" / "time_machine_exporter.py",
)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


def completed(arguments, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


class TimeMachineExporterTests(unittest.TestCase):
    def test_status_and_latest_timestamp_parsers(self):
        self.assertEqual(exporter.parse_backup_running("{ Running = 1; }"), 1)
        self.assertEqual(exporter.parse_backup_running("{\n Running = 0;\n}"), 0)
        self.assertIsNone(exporter.parse_backup_running("{ Percent = 0.5; }"))

        compact = exporter.parse_latest_backup_timestamp(
            "/example/2026-07-14-175830.backup"
        )
        expanded = exporter.parse_latest_backup_timestamp("2026-07-14 17:58:30")
        expected = int(time.mktime((2026, 7, 14, 17, 58, 30, -1, -1, -1)))
        self.assertEqual(compact, expected)
        self.assertEqual(expanded, expected)
        self.assertIsNone(exporter.parse_latest_backup_timestamp("no timestamp"))
        self.assertIsNone(
            exporter.parse_latest_backup_timestamp("2026-13-40-256199")
        )

    def test_preferences_fallback_reads_only_completed_snapshot_dates(self):
        sensitive_fixture = "/Volumes/Private Fixture/opaque-destination-id"
        preferences = plistlib.dumps(
            {
                "Destinations": [
                    {
                        "ID": sensitive_fixture,
                        "URL": f"file://{sensitive_fixture}",
                        "SnapshotDates": [
                            datetime(2026, 7, 14, 20, 0, 0),
                            "not-a-date",
                        ],
                    },
                    {
                        "Name": sensitive_fixture,
                        "SnapshotDates": [datetime(2026, 7, 15, 5, 1, 0)],
                    },
                    {"SnapshotDates": "invalid-container"},
                    "invalid-destination",
                ]
            }
        )

        timestamp = exporter.parse_latest_backup_timestamp_from_preferences(
            preferences
        )
        rendered = "\n".join(
            exporter.render_metrics(
                {
                    "latest_backup_timestamp_seconds": timestamp,
                    "collection_success": 1,
                },
                now=1,
            )
        )

        self.assertEqual(timestamp, 1784091660)
        self.assertNotIn(sensitive_fixture, rendered)

    @mock.patch.object(exporter.subprocess, "run")
    def test_defaults_export_uses_fixed_private_capture_contract(self, run):
        command = [
            exporter.DEFAULTS,
            "export",
            exporter.TIME_MACHINE_PREFERENCES_DOMAIN,
            "-",
        ]
        run.return_value = subprocess.CompletedProcess(
            command, 0, stdout=b"fixture", stderr=b""
        )

        result = exporter.run_defaults_export()

        self.assertEqual(result.stdout, b"fixture")
        run.assert_called_once_with(
            command,
            check=False,
            capture_output=True,
            timeout=exporter.COMMAND_TIMEOUT_SECONDS,
            env=mock.ANY,
        )
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["LANG"], "C")
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertNotIn("shell", run.call_args.kwargs)

    @mock.patch.object(exporter, "run_defaults_export")
    def test_preferences_reader_accepts_only_successful_valid_exports(
        self, run_defaults_export
    ):
        valid = plistlib.dumps(
            {
                "Destinations": [
                    {"SnapshotDates": [datetime(2026, 7, 15, 5, 1, 0)]}
                ]
            }
        )
        command = [exporter.DEFAULTS, "export"]

        run_defaults_export.return_value = subprocess.CompletedProcess(
            command, 0, stdout=valid, stderr=b""
        )
        self.assertEqual(
            exporter.read_latest_backup_timestamp_from_preferences(), 1784091660
        )

        invalid_results = (
            subprocess.CompletedProcess(command, 1, stdout=valid, stderr=b"private"),
            subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(command, 0, stdout=b"not a plist", stderr=b""),
            subprocess.CompletedProcess(
                command, 0, stdout=plistlib.dumps(["invalid-root"]), stderr=b""
            ),
            subprocess.CompletedProcess(
                command,
                0,
                stdout=plistlib.dumps({"Destinations": "invalid"}),
                stderr=b"",
            ),
            subprocess.CompletedProcess(
                command,
                0,
                stdout=plistlib.dumps(
                    {"Destinations": [{"SnapshotDates": ["not-a-date"]}]}
                ),
                stderr=b"",
            ),
        )
        for result in invalid_results:
            with self.subTest(returncode=result.returncode, stdout=result.stdout):
                run_defaults_export.return_value = result
                self.assertIsNone(
                    exporter.read_latest_backup_timestamp_from_preferences()
                )

    @mock.patch.object(exporter.subprocess, "run")
    def test_defaults_export_missing_or_timeout_is_a_safe_failure(self, run):
        run.side_effect = FileNotFoundError
        missing = exporter.run_defaults_export()
        self.assertEqual(missing.returncode, 127)
        self.assertEqual(missing.stdout, b"")
        self.assertEqual(missing.stderr, b"")

        run.side_effect = subprocess.TimeoutExpired(
            [exporter.DEFAULTS, "export"], exporter.COMMAND_TIMEOUT_SECONDS
        )
        timed_out = exporter.run_defaults_export()
        self.assertEqual(timed_out.returncode, 124)
        self.assertEqual(timed_out.stdout, b"")
        self.assertEqual(timed_out.stderr, b"")

    @mock.patch.object(exporter, "run_tmutil")
    def test_unconfigured_destination_is_successful_and_skips_other_commands(
        self, run_tmutil
    ):
        run_tmutil.return_value = completed(
            [exporter.TMUTIL, "destinationinfo"],
            returncode=1,
            stderr="No destinations configured.",
        )

        with mock.patch.object(exporter, "run_defaults_export") as defaults_export:
            snapshot, errors = exporter.collect_snapshot()

        self.assertEqual(errors, [])
        self.assertEqual(snapshot["destination_configured"], 0)
        self.assertEqual(snapshot["backup_running"], 0)
        self.assertEqual(snapshot["latest_backup_available"], 0)
        self.assertIsNone(snapshot["latest_backup_timestamp_seconds"])
        self.assertEqual(snapshot["collection_success"], 1)
        run_tmutil.assert_called_once_with("destinationinfo")
        defaults_export.assert_not_called()

    @mock.patch.object(exporter, "run_tmutil")
    def test_configured_destination_with_valid_latest_backup(self, run_tmutil):
        run_tmutil.side_effect = (
            completed(
                [exporter.TMUTIL, "destinationinfo"],
                stdout="Name : fixture\nID : fixture-id\n",
            ),
            completed(
                [exporter.TMUTIL, "status"], stdout="{ Running = 0; }"
            ),
            completed(
                [exporter.TMUTIL, "latestbackup", "-t"],
                stdout="2026-07-14-175830\n",
            ),
        )

        with mock.patch.object(exporter, "run_defaults_export") as defaults_export:
            snapshot, errors = exporter.collect_snapshot()
        rendered = "\n".join(exporter.render_metrics(snapshot, now=1))

        self.assertEqual(errors, [])
        self.assertEqual(snapshot["destination_configured"], 1)
        self.assertEqual(snapshot["backup_running"], 0)
        self.assertEqual(snapshot["latest_backup_available"], 1)
        self.assertIsNotNone(snapshot["latest_backup_timestamp_seconds"])
        self.assertEqual(snapshot["collection_success"], 1)
        self.assertNotIn("fixture-id", rendered)
        self.assertNotIn("/fixture/", rendered)
        self.assertNotIn("{", rendered)
        self.assertEqual(
            [call.args for call in run_tmutil.call_args_list],
            [("destinationinfo",), ("status",), ("latestbackup", "-t")],
        )
        defaults_export.assert_not_called()

    @mock.patch.object(exporter, "run_tmutil")
    def test_latest_backup_failure_is_explicit_and_does_not_leak_output(
        self, run_tmutil
    ):
        sensitive_fixture = "/Volumes/Private Fixture/opaque-destination-id"
        run_tmutil.side_effect = (
            completed(
                [exporter.TMUTIL, "destinationinfo"],
                stdout=f"Mount Point : {sensitive_fixture}\n",
            ),
            completed(
                [exporter.TMUTIL, "status"], stdout="{ Running = 1; }"
            ),
            completed(
                [exporter.TMUTIL, "latestbackup", "-t"],
                returncode=1,
                stderr=f"cannot inspect {sensitive_fixture}",
            ),
            completed(
                [exporter.TMUTIL, "latestbackup", "-t"],
                returncode=1,
                stderr=f"cannot inspect {sensitive_fixture}",
            ),
            completed(
                [exporter.TMUTIL, "latestbackup", "-t"],
                returncode=1,
                stderr=f"cannot inspect {sensitive_fixture}",
            ),
        )

        preferences_result = subprocess.CompletedProcess(
            [exporter.DEFAULTS, "export"],
            0,
            stdout=plistlib.dumps(
                {
                    "Destinations": [
                        {"ID": sensitive_fixture, "SnapshotDates": []}
                    ]
                }
            ),
            stderr=sensitive_fixture.encode(),
        )
        with mock.patch.object(exporter.time, "sleep") as sleep, mock.patch.object(
            exporter, "run_defaults_export", return_value=preferences_result
        ) as defaults_export:
            snapshot, errors = exporter.collect_snapshot()
        rendered = "\n".join(exporter.render_metrics(snapshot, now=1))
        diagnostics = "\n".join(errors)

        self.assertEqual(snapshot["destination_configured"], 1)
        self.assertEqual(snapshot["backup_running"], 1)
        self.assertEqual(snapshot["latest_backup_available"], 0)
        self.assertIsNone(snapshot["latest_backup_timestamp_seconds"])
        self.assertEqual(snapshot["collection_success"], 0)
        self.assertIn("time_machine_latest_backup_available 0", rendered)
        self.assertNotIn("time_machine_latest_backup_timestamp_seconds ", rendered)
        self.assertNotIn(sensitive_fixture, rendered)
        self.assertNotIn(sensitive_fixture, diagnostics)
        self.assertEqual(sleep.call_count, 2)
        defaults_export.assert_called_once_with()

    @mock.patch.object(exporter, "run_tmutil")
    def test_latest_backup_uses_preferences_when_launchd_lacks_access(
        self, run_tmutil
    ):
        expected = 1784091660
        run_tmutil.side_effect = (
            completed([exporter.TMUTIL, "destinationinfo"], stdout="configured"),
            completed([exporter.TMUTIL, "status"], stdout="{ Running = 0; }"),
            completed([exporter.TMUTIL, "latestbackup", "-t"], returncode=80),
            completed([exporter.TMUTIL, "latestbackup", "-t"], returncode=80),
            completed([exporter.TMUTIL, "latestbackup", "-t"], returncode=80),
        )

        with mock.patch.object(exporter.time, "sleep"), mock.patch.object(
            exporter,
            "read_latest_backup_timestamp_from_preferences",
            return_value=expected,
        ) as preferences:
            snapshot, errors = exporter.collect_snapshot()

        self.assertEqual(errors, [])
        self.assertEqual(snapshot["latest_backup_available"], 1)
        self.assertEqual(snapshot["latest_backup_timestamp_seconds"], expected)
        self.assertEqual(snapshot["collection_success"], 1)
        preferences.assert_called_once_with()

    @mock.patch.object(exporter, "run_tmutil")
    def test_latest_backup_transient_gap_is_retried(self, run_tmutil):
        run_tmutil.side_effect = (
            completed([exporter.TMUTIL, "destinationinfo"], stdout="configured"),
            completed([exporter.TMUTIL, "status"], stdout="{ Running = 0; }"),
            completed(
                [exporter.TMUTIL, "latestbackup", "-t"], stdout="not ready"
            ),
            completed(
                [exporter.TMUTIL, "latestbackup", "-t"],
                stdout="2026-07-14-175830",
            ),
        )

        with mock.patch.object(exporter.time, "sleep") as sleep:
            snapshot, errors = exporter.collect_snapshot()

        self.assertEqual(errors, [])
        self.assertEqual(snapshot["latest_backup_available"], 1)
        self.assertEqual(snapshot["collection_success"], 1)
        sleep.assert_called_once_with(exporter.LATEST_BACKUP_RETRY_DELAY_SECONDS)

    @mock.patch.object(exporter, "run_tmutil")
    def test_unknown_destination_state_is_not_reported_as_unconfigured(
        self, run_tmutil
    ):
        run_tmutil.return_value = completed(
            [exporter.TMUTIL, "destinationinfo"], returncode=72, stderr="failure"
        )

        snapshot, errors = exporter.collect_snapshot()
        rendered = "\n".join(exporter.render_metrics(snapshot, now=1))

        self.assertEqual(snapshot["collection_success"], 0)
        self.assertIsNone(snapshot["destination_configured"])
        self.assertNotIn("time_machine_destination_configured ", rendered)
        self.assertIn("time_machine_exporter_collection_success 0", rendered)
        self.assertEqual(len(errors), 1)

    @mock.patch.object(exporter, "run_tmutil")
    def test_status_parse_failure_keeps_valid_latest_backup_visible(self, run_tmutil):
        run_tmutil.side_effect = (
            completed([exporter.TMUTIL, "destinationinfo"], stdout="configured"),
            completed([exporter.TMUTIL, "status"], stdout="{ Percent = 0.5; }"),
            completed(
                [exporter.TMUTIL, "latestbackup", "-t"],
                stdout="2026-07-14-175830",
            ),
        )

        snapshot, errors = exporter.collect_snapshot()

        self.assertIsNone(snapshot["backup_running"])
        self.assertEqual(snapshot["latest_backup_available"], 1)
        self.assertIsNotNone(snapshot["latest_backup_timestamp_seconds"])
        self.assertEqual(snapshot["collection_success"], 0)
        self.assertEqual(len(errors), 1)

    def test_atomic_write_replaces_destination_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            destination = directory / "time-machine.prom"
            destination.write_text("old\n", encoding="utf-8")

            exporter.write_atomic(destination, ["metric_name 1"])

            self.assertEqual(destination.read_text(encoding="utf-8"), "metric_name 1\n")
            self.assertEqual(list(directory.glob(".*.tmp")), [])

    def test_atomic_write_failure_preserves_destination_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            destination = directory / "time-machine.prom"
            destination.write_text("known-good\n", encoding="utf-8")

            with mock.patch.object(exporter.os, "replace", side_effect=OSError("fail")):
                with self.assertRaises(OSError):
                    exporter.write_atomic(destination, ["replacement 1"])

            self.assertEqual(
                destination.read_text(encoding="utf-8"), "known-good\n"
            )
            self.assertEqual(list(directory.glob(".*.tmp")), [])

    def test_self_test_is_offline(self):
        with mock.patch.object(exporter, "run_tmutil") as run_tmutil, mock.patch.object(
            exporter, "run_defaults_export"
        ) as run_defaults_export:
            self.assertTrue(exporter.self_test())
        run_tmutil.assert_not_called()
        run_defaults_export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
