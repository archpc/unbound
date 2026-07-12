"""Deterministic request-level rejection for explicit boundary-bypass requests."""

from __future__ import annotations

import re


POLICY_OVERRIDE_RE = re.compile(
    r"(?i)\b(?:ignore|override|bypass|disable|evade|break)\b.{0,40}"
    r"\b(?:system|prompt|policy|restrictions?|guardrail|allowlist|safety)\b"
)
WRITE_ACTION_RE = re.compile(
    r"(?i)(?:^|[.!?]\s*|\bplease\s+|\b(?:can|could|would|will)\s+you\s+)"
    r"(?:sudo\s+)?(?:restart|reload|deploy|install|edit|write|delete|remove|change)\b"
)
SHELL_ACTION_RE = re.compile(
    r"(?i)\b(?:run|execute|open)\b.{0,30}\b(?:shell|command|terminal|/bin/(?:sh|zsh|bash))\b"
)
SECRET_ACCESS_RE = re.compile(
    r"(?i)\b(?:read|show|print|reveal|expose|cat)\b.{0,40}"
    r"(?:\b(?:secret|token|credential|private key|ntfy topic)\b|/etc/passwd|\.ssh/)"
)


def rejection_reason(question):
    for reason, pattern in (
        ("policy_override", POLICY_OVERRIDE_RE),
        ("write_action", WRITE_ACTION_RE),
        ("arbitrary_shell", SHELL_ACTION_RE),
        ("secret_access", SECRET_ACCESS_RE),
    ):
        if pattern.search(question):
            return reason
    return None
