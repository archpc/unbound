#!/usr/bin/env python3
"""Read-only deployment drift verifier for the Mac mini.

The manifest is authoritative. This tool never copies, edits, chmods, chowns,
or reloads anything locally or remotely. It sends one small Python probe over
SSH stdin, then compares only the files whose manifest policy is ``sha256``.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "deployment-manifest.json"
CATEGORY_ORDER = ("docs", "config", "script", "plist", "dashboard", "textfile")
CATEGORIES = set(CATEGORY_ORDER)
COMPARE_POLICIES = {"none", "existence", "sha256"}


class ManifestError(Exception):
    """The manifest cannot produce a trustworthy verification result."""


class RemoteUnavailable(Exception):
    """SSH or the remote probe failed, so remote state is unknown."""


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_path(repo_root, source):
    if not isinstance(source, str) or not source:
        raise ManifestError("source must be a non-empty repo-relative path")
    relative = Path(source)
    if relative.is_absolute():
        raise ManifestError("source paths must be repo-relative: {!r}".format(source))
    resolved = (repo_root / relative).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        raise ManifestError("source escapes the repo root: {!r}".format(source))
    return resolved


def load_manifest(path, repo_root=REPO_ROOT):
    try:
        with path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("cannot read manifest {}: {}".format(path, exc))

    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ManifestError("manifest must be an object with version 1")
    target = manifest.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("ssh"), str):
        raise ManifestError("manifest target.ssh must be a string")
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ManifestError("manifest entries must be a non-empty list")

    metadata_defaults = manifest.get("metadata_defaults", {})
    if not isinstance(metadata_defaults, dict):
        raise ManifestError("manifest metadata_defaults must be an object")
    for category, expected in metadata_defaults.items():
        if category not in CATEGORIES or not isinstance(expected, dict):
            raise ManifestError("invalid metadata default for {!r}".format(category))
        if set(expected) - {"owner", "group", "mode"}:
            raise ManifestError("unknown metadata field for {!r}".format(category))
        if "mode" in expected and not re.fullmatch(r"0[0-7]{3}", expected["mode"]):
            raise ManifestError("metadata mode for {!r} must be four octal digits".format(category))
        if any(not isinstance(value, str) or not value for value in expected.values()):
            raise ManifestError("metadata values for {!r} must be non-empty strings".format(category))

    entries = []
    destinations = set()
    for index, raw in enumerate(raw_entries):
        label = "entry {}".format(index + 1)
        if not isinstance(raw, dict):
            raise ManifestError("{} must be an object".format(label))

        category = raw.get("category")
        if category not in CATEGORIES:
            raise ManifestError("{} has unknown category {!r}".format(label, category))
        compare = raw.get("compare")
        if compare not in COMPARE_POLICIES:
            raise ManifestError("{} has unknown compare policy {!r}".format(label, compare))
        required = raw.get("required", True)
        sensitive = raw.get("sensitive", False)
        if not isinstance(required, bool) or not isinstance(sensitive, bool):
            raise ManifestError("{} required/sensitive fields must be booleans".format(label))

        source = raw.get("source")
        source_path = None
        if source is not None:
            source_path = _source_path(repo_root, source)
        destination = raw.get("destination")
        if destination is not None:
            if not isinstance(destination, str) or not destination.startswith("/"):
                raise ManifestError("{} destination must be an absolute path or null".format(label))
            if destination in destinations:
                raise ManifestError("duplicate runtime destination: {}".format(destination))
            destinations.add(destination)

        name = raw.get("name")
        if not isinstance(name, str) or not name:
            name = source or destination or label

        if source is None and not sensitive:
            raise ManifestError("{} may omit source only when sensitive=true".format(label))
        if compare == "sha256" and (source is None or destination is None or sensitive):
            raise ManifestError("{} sha256 requires non-sensitive source and destination".format(label))
        if compare != "none" and destination is None:
            raise ManifestError("{} comparison requires a destination".format(label))
        if category == "docs" and (destination is not None or compare != "none"):
            raise ManifestError("{} docs entries must be local-only".format(label))
        if category == "textfile" and compare != "existence":
            raise ManifestError("{} textfiles must use existence comparison".format(label))
        if sensitive and compare != "existence":
            raise ManifestError("{} sensitive entries must use existence comparison".format(label))

        metadata = raw.get("metadata", {})
        if not isinstance(metadata, dict) or set(metadata) - {"owner", "group", "mode"}:
            raise ManifestError("{} has invalid metadata declaration".format(label))
        if "mode" in metadata and not re.fullmatch(r"0[0-7]{3}", metadata["mode"]):
            raise ManifestError("{} metadata mode must be four octal digits".format(label))
        if any(not isinstance(value, str) or not value for value in metadata.values()):
            raise ManifestError("{} metadata values must be non-empty strings".format(label))

        entry = dict(raw)
        entry.update({
            "index": index,
            "name": name,
            "required": required,
            "sensitive": sensitive,
            "source_path": source_path,
            "expected_metadata": dict(
                metadata_defaults.get(category, {}),
                **metadata,
            ),
        })
        entries.append(entry)

    normalized = dict(manifest)
    normalized["entries"] = entries
    return normalized


REMOTE_PROBE = r'''
import hashlib
import json
import os
import pwd
import grp
import stat

items = json.loads(__ITEMS__)
facts = []
for item in items:
    fact = {"index": item["index"]}
    try:
        metadata = os.stat(item["path"])
    except FileNotFoundError:
        fact["exists"] = False
        facts.append(fact)
        continue
    except OSError as exc:
        fact.update({"exists": None, "error": type(exc).__name__})
        facts.append(fact)
        continue

    fact["exists"] = True
    if stat.S_ISREG(metadata.st_mode):
        fact["kind"] = "file"
    elif stat.S_ISDIR(metadata.st_mode):
        fact["kind"] = "directory"
    else:
        fact["kind"] = "other"
    fact["mode"] = format(stat.S_IMODE(metadata.st_mode), "04o")
    try:
        fact["owner"] = pwd.getpwuid(metadata.st_uid).pw_name
    except KeyError:
        fact["owner"] = str(metadata.st_uid)
    try:
        fact["group"] = grp.getgrgid(metadata.st_gid).gr_name
    except KeyError:
        fact["group"] = str(metadata.st_gid)

    if item["sha256"] and fact["kind"] == "file":
        try:
            digest = hashlib.sha256()
            with open(item["path"], "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            fact["sha256"] = digest.hexdigest()
        except OSError as exc:
            fact["hash_error"] = type(exc).__name__
    facts.append(fact)

print(json.dumps(facts, separators=(",", ":")))
'''


def build_remote_script(entries):
    items = []
    for entry in entries:
        destination = entry.get("destination")
        if destination is None:
            continue
        items.append({
            "index": entry["index"],
            "path": destination,
            "sha256": entry["compare"] == "sha256" and not entry["sensitive"],
        })
    payload = json.dumps(items, separators=(",", ":"))
    return REMOTE_PROBE.replace("__ITEMS__", repr(payload))


def query_remote(host, entries, connect_timeout=10):
    command = [
        "/usr/bin/ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout={}".format(connect_timeout),
        host,
        "/usr/bin/python3", "-",
    ]
    try:
        completed = subprocess.run(
            command,
            input=build_remote_script(entries),
            text=True,
            capture_output=True,
            timeout=connect_timeout + 15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemoteUnavailable("SSH probe could not run ({})".format(type(exc).__name__))
    if completed.returncode != 0:
        raise RemoteUnavailable("SSH probe exited {}".format(completed.returncode))
    try:
        raw_facts = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RemoteUnavailable("SSH probe returned malformed JSON")
    if not isinstance(raw_facts, list):
        raise RemoteUnavailable("SSH probe returned an unexpected result shape")

    facts = {}
    for fact in raw_facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("index"), int):
            raise RemoteUnavailable("SSH probe returned malformed facts")
        facts[fact["index"]] = fact
    expected = sum(1 for entry in entries if entry.get("destination") is not None)
    if len(facts) != expected:
        raise RemoteUnavailable("SSH probe returned an incomplete result set")
    return facts


def metadata_suffix(fact):
    owner = fact.get("owner")
    group = fact.get("group")
    mode = fact.get("mode")
    if owner is None or group is None or mode is None:
        return "metadata unavailable"
    return "remote={}:{} mode={}".format(owner, group, mode)


def mapping_text(entry):
    source = entry.get("source")
    if source is None:
        source = "(runtime-only; content not in repo)"
    destination = entry.get("destination")
    if destination is None:
        destination = "(local only)"
    return "{} -> {}".format(source, destination)


def render_report(manifest, manifest_path, repo_root, remote_facts=None, remote_error=None):
    lines = [
        "Deployment drift verification (read-only)",
        "Manifest: {}".format(manifest_path),
        "Target: {}".format(manifest["target"]["ssh"]),
    ]
    if remote_error:
        lines.append("REMOTE INCOMPLETE: {} (not counted as drift)".format(remote_error))

    counts = {"ok": 0, "drift": 0, "missing": 0, "optional": 0, "unknown": 0}
    entries = manifest["entries"]
    for category in CATEGORY_ORDER:
        category_entries = [entry for entry in entries if entry["category"] == category]
        if not category_entries:
            continue
        lines.extend(("", "[{}]".format(category)))
        for entry in category_entries:
            mapping = mapping_text(entry)
            source_path = entry["source_path"]

            if source_path is not None and not source_path.is_file():
                if entry["required"]:
                    counts["missing"] += 1
                    lines.append("MISSING  {} | required source absent".format(mapping))
                else:
                    counts["optional"] += 1
                    lines.append("OPTIONAL {} | source absent".format(mapping))
                continue

            if entry.get("destination") is None:
                counts["ok"] += 1
                lines.append("OK       {}".format(mapping))
                continue

            if remote_error:
                counts["unknown"] += 1
                lines.append("UNKNOWN  {} | remote not checked".format(mapping))
                continue
            fact = (remote_facts or {}).get(entry["index"])
            if fact is None or fact.get("exists") is None:
                counts["unknown"] += 1
                lines.append("UNKNOWN  {} | remote metadata unavailable".format(mapping))
                continue
            if fact.get("exists") is False:
                if entry["required"]:
                    counts["missing"] += 1
                    lines.append("MISSING  {} | required destination absent".format(mapping))
                else:
                    counts["optional"] += 1
                    lines.append("OPTIONAL {} | destination absent".format(mapping))
                continue
            if fact.get("kind") != "file":
                counts["drift"] += 1
                lines.append("DRIFT    {} | destination is {}".format(mapping, fact.get("kind", "unknown")))
                continue

            suffix = metadata_suffix(fact)
            expected = entry["expected_metadata"]
            mismatches = []
            for key in ("owner", "group", "mode"):
                if key in expected and fact.get(key) != expected[key]:
                    mismatches.append(
                        "{} expected={} actual={}".format(
                            key, expected[key], fact.get(key, "unavailable")
                        )
                    )
            if mismatches:
                counts["drift"] += 1
                lines.append(
                    "DRIFT    {} | {}; {}".format(
                        mapping, "; ".join(mismatches), suffix
                    )
                )
                continue
            if entry["sensitive"]:
                counts["ok"] += 1
                lines.append("OK       {} | {}; content redacted; checksum skipped".format(mapping, suffix))
                continue
            if entry["compare"] == "sha256":
                remote_hash = fact.get("sha256")
                if remote_hash is None:
                    counts["unknown"] += 1
                    lines.append("UNKNOWN  {} | {}; checksum unavailable".format(mapping, suffix))
                    continue
                local_hash = sha256_file(source_path)
                if local_hash != remote_hash:
                    counts["drift"] += 1
                    lines.append(
                        "DRIFT    {} | sha256 local={} remote={}; {}".format(
                            mapping, local_hash[:12], remote_hash[:12], suffix
                        )
                    )
                    continue
                counts["ok"] += 1
                lines.append("OK       {} | sha256 match; {}".format(mapping, suffix))
                continue

            counts["ok"] += 1
            lines.append("OK       {} | exists; checksum skipped; {}".format(mapping, suffix))

    lines.extend((
        "",
        "Summary: ok={ok} drift={drift} missing_required={missing} "
        "optional_missing={optional} unknown={unknown}".format(**counts),
    ))
    if counts["drift"] or counts["missing"]:
        lines.append("RESULT: DRIFT")
        exit_code = 1
    elif counts["unknown"]:
        lines.append("RESULT: INCOMPLETE (no confirmed drift; exits 0 by policy)")
        exit_code = 0
    else:
        lines.append("RESULT: CLEAN")
        exit_code = 0
    return "\n".join(lines), exit_code, counts


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help="manifest path (default: %(default)s)",
    )
    parser.add_argument("--host", help="override manifest target.ssh")
    parser.add_argument("--connect-timeout", type=int, default=10)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.host:
            manifest["target"] = dict(manifest["target"], ssh=args.host)
        try:
            facts = query_remote(
                manifest["target"]["ssh"], manifest["entries"], args.connect_timeout
            )
            remote_error = None
        except RemoteUnavailable as exc:
            facts = None
            remote_error = str(exc)
        report, exit_code, _counts = render_report(
            manifest, args.manifest, REPO_ROOT, facts, remote_error
        )
    except ManifestError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1
    print(report)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
