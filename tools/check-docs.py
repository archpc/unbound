#!/usr/bin/env python3
"""Validate repository documentation without reading runtime state or secrets."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOCS = (
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

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
SHELL_FENCE_RE = re.compile(
    r"^[ \t]*```(?:sh|bash)\s*\n(.*?)^[ \t]*```\s*$",
    re.DOTALL | re.MULTILINE,
)
ALERT_RE = re.compile(r"^\s*- alert:\s*([A-Za-z0-9_]+)\s*$", re.MULTILINE)
RECORD_RE = re.compile(r"^\s*- record:\s*([A-Za-z0-9_:]+)\s*$", re.MULTILINE)
JOB_RE = re.compile(r"^\s*- job_name:\s*([A-Za-z0-9_]+)\s*$", re.MULTILINE)
LABEL_RE = re.compile(
    r"<key>Label</key>\s*<string>([^<]+)</string>", re.MULTILINE
)

REPO_PATH_PREFIXES = (
    "config/",
    "launchd/",
    "scripts/",
    "tools/",
    "tests/",
    "docs/",
)

REQUIRED_CUSTOM_METRICS = {
    "unbound_job_last_success_timestamp_seconds",
    "unbound_blocklist_lines",
    "unbound_blocklist_minimum_lines",
    "unbound_warmlist_domains",
    "unbound_roothints_records",
    "unbound_client_rpz_blocks_5m",
    "unbound_client_last_rpz_block_seconds",
    "unbound_client_rpz_unattributed_5m",
    "unbound_client_rpz_parse_errors_5m",
    "unbound_ntfy_last_attempt_timestamp_seconds",
    "unbound_ntfy_last_failure_timestamp_seconds",
    "unbound_ntfy_last_server_acceptance_timestamp_seconds",
    "unbound_ntfy_prometheus_query_success",
    "unbound_ntfy_alert_state_processed",
    "unbound_ntfy_resolutions_enabled",
    "unbound_ntfy_state_persistence_success",
    "unbound_ntfy_topic_ready",
    "unbound_ntfy_blocked_transitions",
    "unbound_ntfy_phone_receipt_known",
    "unbound_ntfy_firing_alerts",
    "unbound_ntfy_pending_alerts",
    "unbound_ntfy_invalid_alerts",
    "unbound_ntfy_suppressed_duplicates",
    "unbound_ntfy_resolutions_suppressed",
    "unbound_ntfy_server_acceptances_total",
    "unbound_ntfy_notifications_sent_total",
    "unbound_ntfy_delivery_failures_total",
    "unbound_ntfy_delivery_outcome_unknown_total",
    "unbound_ntfy_state_errors_total",
    "unbound_ntfy_state_write_errors_total",
    "unbound_ntfy_state_resets_total",
    "unbound_ntfy_retry_attempts_total",
    "unbound_ntfy_permanent_rejections_total",
    "unbound_ntfy_consecutive_failures",
    "apple_content_cache_active",
    "apple_content_cache_activated",
    "apple_content_cache_status_ok",
    "apple_content_cache_totals_since_seconds",
    "apple_content_cache_detail_bytes",
    "time_machine_destination_configured",
    "time_machine_backup_running",
    "time_machine_latest_backup_available",
    "time_machine_latest_backup_timestamp_seconds",
    "time_machine_exporter_collection_success",
}

SECRET_PATTERNS = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "long SSH key material": re.compile(r"ssh-(?:rsa|ed25519)\s+[A-Za-z0-9+/]{40,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "OpenAI-style token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._~-]{20,}", re.IGNORECASE),
    "ntfy topic URL": re.compile(r"https://ntfy\.sh/[A-Za-z0-9._-]{12,}"),
}


def github_slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[`*_~]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return re.sub(r"\s+", "-", text)


def anchors(text: str) -> set[str]:
    result: set[str] = set()
    counts: dict[str, int] = {}
    for heading in HEADING_RE.findall(text):
        base = github_slug(heading)
        count = counts.get(base, 0)
        result.add(base if count == 0 else f"{base}-{count}")
        counts[base] = count + 1
    return result


def split_link(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        raw = raw[1 : raw.index(">")]
    elif " \"" in raw:
        raw = raw.split(" \"", 1)[0]
    path, marker, anchor = raw.partition("#")
    return unquote(path), unquote(anchor if marker else "")


def load_docs(errors: list[str]) -> dict[Path, str]:
    docs: dict[Path, str] = {}
    for relative in DOCS:
        path = ROOT / relative
        if not path.is_file():
            errors.append(f"missing canonical document: {relative}")
            continue
        docs[path] = path.read_text(encoding="utf-8")
    return docs


def check_links(docs: dict[Path, str], errors: list[str]) -> None:
    anchor_cache = {path: anchors(text) for path, text in docs.items()}
    for source, text in docs.items():
        for raw in LINK_RE.findall(text):
            path_text, anchor = split_link(raw)
            if path_text.startswith(("http://", "https://", "mailto:")):
                continue
            target = source if not path_text else (source.parent / path_text).resolve()
            try:
                target.relative_to(ROOT)
            except ValueError:
                errors.append(f"{source.name}: link escapes repository: {raw}")
                continue
            if not target.exists():
                errors.append(f"{source.name}: missing link target: {raw}")
                continue
            if anchor:
                if target.suffix.lower() != ".md":
                    errors.append(f"{source.name}: anchor targets non-Markdown file: {raw}")
                    continue
                target_text = docs.get(target)
                if target_text is None:
                    target_text = target.read_text(encoding="utf-8")
                    anchor_cache[target] = anchors(target_text)
                if anchor not in anchor_cache[target]:
                    errors.append(f"{source.name}: missing anchor: {raw}")


def check_backticked_paths(docs: dict[Path, str], errors: list[str]) -> None:
    for source, text in docs.items():
        for value in BACKTICK_RE.findall(text):
            if not value.startswith(REPO_PATH_PREFIXES):
                continue
            if any(char in value for char in ("*", "<", ">", "{", "}")):
                continue
            candidate = value.rstrip("/.,;:")
            if not (ROOT / candidate).exists():
                errors.append(f"{source.name}: missing referenced repo path: {value}")


def check_mermaid(docs: dict[Path, str], errors: list[str]) -> None:
    for source, text in docs.items():
        openings = list(re.finditer(r"^```mermaid\s*$", text, re.MULTILINE))
        for opening in openings:
            closing = text.find("\n```", opening.end())
            if closing < 0:
                errors.append(f"{source.name}: unclosed Mermaid fence")
                continue
            body = text[opening.end() : closing].strip()
            first = body.splitlines()[0] if body else ""
            if not re.match(r"^(?:flowchart|sequenceDiagram)\b", first):
                errors.append(f"{source.name}: unsupported or empty Mermaid diagram: {first!r}")


def check_shell_examples(docs: dict[Path, str], errors: list[str]) -> None:
    """Parse shell examples without executing them.

    Angle-bracket metavariables are documentation notation but shell redirection syntax,
    so replace them with a harmless word before invoking ``bash -n``.
    """
    for source, text in docs.items():
        for index, body in enumerate(SHELL_FENCE_RE.findall(text), start=1):
            sanitized = re.sub(r"<[^>\n]+>", "placeholder", body)
            completed = subprocess.run(
                ["/bin/bash", "-n"],
                input=sanitized,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip().splitlines()[-1]
                errors.append(
                    f"{source.name}: shell block {index} fails bash syntax: {detail}"
                )


def check_implementation_inventory(docs: dict[Path, str], errors: list[str]) -> None:
    reference = docs.get(ROOT / "REFERENCE.md", "")

    rules = (ROOT / "config/prometheus/prometheus.rules.yml").read_text(encoding="utf-8")
    for name in sorted(set(ALERT_RE.findall(rules))):
        if f"`{name}`" not in reference:
            errors.append(f"REFERENCE.md: missing alert: {name}")
    for name in sorted(set(RECORD_RE.findall(rules))):
        if f"`{name}`" not in reference:
            errors.append(f"REFERENCE.md: missing recording rule: {name}")

    labels: set[str] = set()
    for path in (ROOT / "launchd").glob("*.plist"):
        labels.update(LABEL_RE.findall(path.read_text(encoding="utf-8")))
    for name in sorted(labels):
        if f"`{name}`" not in reference:
            errors.append(f"REFERENCE.md: missing launchd label: {name}")

    prom = (ROOT / "config/prometheus/prometheus.yml").read_text(encoding="utf-8")
    for name in sorted(set(JOB_RE.findall(prom))):
        if f"`{name}`" not in reference:
            errors.append(f"REFERENCE.md: missing Prometheus job: {name}")

    for path in sorted((ROOT / "config/grafana/dashboards").glob("*.json")):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        uid = dashboard.get("uid")
        if not uid or f"`{uid}`" not in reference:
            errors.append(f"REFERENCE.md: missing dashboard UID: {uid or path.name}")

    for metric in sorted(REQUIRED_CUSTOM_METRICS):
        if f"`{metric}" not in reference:
            errors.append(f"REFERENCE.md: missing custom metric: {metric}")

    manifest = json.loads((ROOT / "deployment-manifest.json").read_text(encoding="utf-8"))
    manifest_docs = [
        entry.get("source") for entry in manifest.get("entries", [])
        if entry.get("category") == "docs"
    ]
    if tuple(manifest_docs) != DOCS:
        errors.append(
            "deployment-manifest.json docs entries must match the canonical "
            "documentation inventory"
        )
    for entry in manifest.get("entries", []):
        source = entry.get("source")
        if source and not (ROOT / source).is_file():
            errors.append(f"deployment manifest source does not exist: {source}")


def check_contract_and_secrets(docs: dict[Path, str], errors: list[str]) -> None:
    readme = docs.get(ROOT / "README.md", "")
    if "RFC 8174" not in readme or "rfc-editor.org/rfc/rfc8174" not in readme:
        errors.append("README.md: missing RFC 8174 declaration/link")

    combined = "\n".join(docs.values())
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(combined):
            errors.append(f"documentation contains possible {label}")


def run_checks() -> list[str]:
    errors: list[str] = []
    docs = load_docs(errors)
    check_links(docs, errors)
    check_backticked_paths(docs, errors)
    check_mermaid(docs, errors)
    check_shell_examples(docs, errors)
    check_implementation_inventory(docs, errors)
    check_contract_and_secrets(docs, errors)
    return errors


def main() -> int:
    errors = run_checks()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print(f"documentation validation failed: {len(errors)} error(s)")
        return 1
    print(
        "documentation validation passed: links, anchors, paths, Mermaid, shell "
        "syntax, implementation inventories, RFC 8174, and secret patterns"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
