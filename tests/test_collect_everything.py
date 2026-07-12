#!/usr/bin/env python3
"""Security regression tests for the offline collection tool."""

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CollectionToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "tools/collect-everything.sh").read_text(
            encoding="utf-8"
        )

    def _functions(self, name):
        # Extract whole shell functions so the tests exercise the implementation
        # without running the collector, opening SSH, or reading runtime files.
        return re.findall(
            rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}",
            self.source,
        )

    def test_sensitive_runtime_paths_are_always_excluded(self):
        functions = self._functions("is_secret_path")
        self.assertEqual(len(functions), 1)
        function = functions[0]
        for path in (
            "monitoring/ntfy.topic",
            "warmup/dns-warm-domains.personal.txt",
            "nested/id_ed25519",
            "config/.env.local",
        ):
            with self.subTest(path=path):
                completed = subprocess.run(
                    ["/bin/bash", "-c", function + '\nis_secret_path "$1"', "bash", path],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_local_and_remote_redactors_cover_credential_forms(self):
        # The script embeds a second implementation for execution on the Mini; both
        # copies must provide the same minimum credential coverage.
        functions = self._functions("redact_stream")
        self.assertEqual(len(functions), 2)
        secrets = (
            "topic-value",
            "bearer-value",
            "password-value",
            "url-password",
        )
        payload = (
            f"ntfy_topic={secrets[0]}\n"
            f"Authorization: Bearer {secrets[1]}\n"
            f"https://user:{secrets[3]}@example.invalid/path\n"
            "https://ntfy.sh/" + secrets[2] + "\n"
        )
        for function in functions:
            completed = subprocess.run(
                ["/bin/bash", "-c", function + "\nredact_stream"],
                input=payload,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertGreaterEqual(completed.stdout.count("[REDACTED]"), 4)
            for secret in secrets:
                self.assertNotIn(secret, completed.stdout)

    def test_embedded_remote_collector_is_valid_bash(self):
        opening = '  cat > "$REMOTE_SCRIPT" <<\'REMOTE_EOF\'\n'
        self.assertIn(opening, self.source)
        embedded = self.source.split(opening, 1)[1].split("\nREMOTE_EOF", 1)[0]
        completed = subprocess.run(
            ["/bin/bash", "-n"],
            input=embedded,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
