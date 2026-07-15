#!/usr/bin/env python3
"""Offline behavior tests for the Time Machine textfile collector."""

from __future__ import annotations

import importlib.util
from pathlib import Path
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

    @mock.patch.object(exporter, "run_tmutil")
    def test_unconfigured_destination_is_successful_and_skips_other_commands(
        self, run_tmutil
    ):
        run_tmutil.return_value = completed(
            [exporter.TMUTIL, "destinationinfo"],
            returncode=1,
            stderr="No destinations configured.",
        )

        snapshot, errors = exporter.collect_snapshot()

        self.assertEqual(errors, [])
        self.assertEqual(snapshot["destination_configured"], 0)
        self.assertEqual(snapshot["backup_running"], 0)
        self.assertEqual(snapshot["latest_backup_available"], 0)
        self.assertIsNone(snapshot["latest_backup_timestamp_seconds"])
        self.assertEqual(snapshot["collection_success"], 1)
        run_tmutil.assert_called_once_with("destinationinfo")

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
                [exporter.TMUTIL, "latestbackup"],
                stdout="/fixture/2026-07-14-175830.backup\n",
            ),
        )

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
            [("destinationinfo",), ("status",), ("latestbackup",)],
        )

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
                [exporter.TMUTIL, "latestbackup"],
                returncode=1,
                stderr=f"cannot inspect {sensitive_fixture}",
            ),
            completed(
                [exporter.TMUTIL, "latestbackup"],
                returncode=1,
                stderr=f"cannot inspect {sensitive_fixture}",
            ),
            completed(
                [exporter.TMUTIL, "latestbackup"],
                returncode=1,
                stderr=f"cannot inspect {sensitive_fixture}",
            ),
        )

        with mock.patch.object(exporter.time, "sleep") as sleep:
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

    @mock.patch.object(exporter, "run_tmutil")
    def test_latest_backup_transient_gap_is_retried(self, run_tmutil):
        run_tmutil.side_effect = (
            completed([exporter.TMUTIL, "destinationinfo"], stdout="configured"),
            completed([exporter.TMUTIL, "status"], stdout="{ Running = 0; }"),
            completed([exporter.TMUTIL, "latestbackup"], stdout="not ready"),
            completed(
                [exporter.TMUTIL, "latestbackup"],
                stdout="/fixture/2026-07-14-175830.backup",
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
                [exporter.TMUTIL, "latestbackup"],
                stdout="/fixture/2026-07-14-175830.backup",
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
        with mock.patch.object(exporter, "run_tmutil") as run_tmutil:
            self.assertTrue(exporter.self_test())
        run_tmutil.assert_not_called()


if __name__ == "__main__":
    unittest.main()
