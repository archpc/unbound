#!/usr/bin/env python3
"""Tests for the read-only deployment manifest verifier."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DOCS = (
    "README.md",
    "ARCHITECTURE.md",
    "RUNBOOK.md",
    "DEPLOYMENT.md",
    "TROUBLESHOOTING.md",
    "REFERENCE.md",
    "DISASTER-RECOVERY.md",
    "REPO_LAYOUT.md",
    "DASHBOARDS.md",
    "AGENTS.md",
    "CLAUDE.md",
)
SPEC = importlib.util.spec_from_file_location(
    "deployment_verifier", ROOT / "tools" / "verify-deployment.py"
)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class DeploymentVerifierTests(unittest.TestCase):
    def write_manifest(self, root, entries):
        path = root / "manifest.json"
        path.write_text(json.dumps({
            "version": 1,
            "target": {"ssh": "localadmin@example.invalid"},
            "entries": entries,
        }))
        return path

    def source_entry(self, source="source.conf", required=True, compare="sha256"):
        return {
            "category": "config",
            "source": source,
            "destination": "/runtime/source.conf",
            "required": required,
            "compare": compare,
        }

    def remote_file(self, sha=None):
        fact = {
            "index": 0,
            "exists": True,
            "kind": "file",
            "owner": "root",
            "group": "wheel",
            "mode": "0644",
        }
        if sha is not None:
            fact["sha256"] = sha
        return fact

    def test_repository_manifest_has_all_categories_and_existing_sources(self):
        manifest = verifier.load_manifest(ROOT / "deployment-manifest.json", ROOT)
        self.assertEqual(
            {entry["category"] for entry in manifest["entries"]},
            set(verifier.CATEGORY_ORDER),
        )
        self.assertEqual(sum(entry["sensitive"] for entry in manifest["entries"]), 2)
        docs = [entry for entry in manifest["entries"] if entry["category"] == "docs"]
        self.assertEqual(tuple(entry["source"] for entry in docs), CANONICAL_DOCS)
        for entry in docs:
            with self.subTest(document=entry["source"]):
                self.assertIsNone(entry["destination"])
                self.assertTrue(entry["required"])
                self.assertEqual(entry["compare"], "none")
        for entry in manifest["entries"]:
            if entry["source_path"] is not None:
                self.assertTrue(entry["source_path"].is_file(), entry["source"])

    def test_rejects_source_path_traversal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = self.write_manifest(root, [self.source_entry("../escape")])
            with self.assertRaises(verifier.ManifestError):
                verifier.load_manifest(path, root)

    def test_clean_sha_report_includes_owner_and_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = b"same content\n"
            (root / "source.conf").write_bytes(data)
            path = self.write_manifest(root, [self.source_entry()])
            manifest = verifier.load_manifest(path, root)
            fact = self.remote_file(digest(data))
            report, code, counts = verifier.render_report(
                manifest, path, root, {0: fact}
            )
            self.assertEqual(code, 0)
            self.assertEqual(counts["ok"], 1)
            self.assertIn("sha256 match", report)
            self.assertIn("remote=root:wheel mode=0644", report)
            self.assertIn("RESULT: CLEAN", report)

    def test_checksum_drift_and_missing_required_exit_one(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source.conf").write_text("repo")
            entries = [
                self.source_entry(),
                {
                    "category": "script",
                    "source": "missing.sh",
                    "destination": "/runtime/missing.sh",
                    "required": True,
                    "compare": "sha256",
                },
            ]
            path = self.write_manifest(root, entries)
            manifest = verifier.load_manifest(path, root)
            report, code, counts = verifier.render_report(
                manifest, path, root, {0: self.remote_file(digest(b"different"))}
            )
            self.assertEqual(code, 1)
            self.assertEqual(counts["drift"], 1)
            self.assertEqual(counts["missing"], 1)
            self.assertIn("RESULT: DRIFT", report)

    def test_optional_destination_missing_does_not_fail(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source.conf").write_text("optional")
            path = self.write_manifest(
                root, [self.source_entry(required=False, compare="existence")]
            )
            manifest = verifier.load_manifest(path, root)
            report, code, counts = verifier.render_report(
                manifest, path, root, {0: {"index": 0, "exists": False}}
            )
            self.assertEqual(code, 0)
            self.assertEqual(counts["optional"], 1)
            self.assertIn("OPTIONAL", report)

    def test_sensitive_runtime_file_is_never_hashed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            entry = {
                "name": "secret topic",
                "category": "config",
                "source": None,
                "destination": "/runtime/secret.topic",
                "required": True,
                "compare": "existence",
                "sensitive": True,
            }
            path = self.write_manifest(root, [entry])
            manifest = verifier.load_manifest(path, root)
            fact = self.remote_file()
            report, code, _counts = verifier.render_report(
                manifest, path, root, {0: fact}
            )
            remote_script = verifier.build_remote_script(manifest["entries"])
            self.assertEqual(code, 0)
            self.assertIn("content redacted; checksum skipped", report)
            self.assertNotIn("sha256 match", report)
            self.assertIn('"sha256":false', remote_script)

    def test_remote_failure_is_incomplete_but_not_drift(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source.conf").write_text("repo")
            path = self.write_manifest(root, [self.source_entry()])
            manifest = verifier.load_manifest(path, root)
            report, code, counts = verifier.render_report(
                manifest, path, root, remote_error="SSH probe exited 255"
            )
            self.assertEqual(code, 0)
            self.assertEqual(counts["unknown"], 1)
            self.assertIn("RESULT: INCOMPLETE", report)
            self.assertNotIn("DRIFT    ", report)

    def test_query_remote_sends_probe_over_stdin(self):
        entries = [{
            "index": 0,
            "destination": "/runtime/file",
            "compare": "sha256",
            "sensitive": False,
        }]
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps([self.remote_file("a" * 64)]), stderr="",
        )
        with mock.patch.object(verifier.subprocess, "run", return_value=completed) as run:
            facts = verifier.query_remote("localadmin@example.invalid", entries, 3)
        self.assertEqual(facts[0]["sha256"], "a" * 64)
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["/usr/bin/python3", "-"])
        self.assertIn("BatchMode=yes", command)
        self.assertIn("hashlib.sha256", run.call_args.kwargs["input"])
        self.assertTrue(run.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
