"""Shared fixed-argv and redaction helpers for read-only tools."""

from __future__ import annotations

import os
import re
import stat
import subprocess


IPV4_RE = re.compile(r"(?<![A-Za-z0-9])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9])")
SECRET_RE = re.compile(r"(?i)\b(token|secret|password|authorization|ntfy(?:\.topic)?)\b\s*[:=]\s*\S+")


def redact_text(text):
    text = SECRET_RE.sub(lambda match: match.group(1) + "=[REDACTED]", text)
    return IPV4_RE.sub("[REDACTED_IP]", text)


def run_fixed(argv, timeout_seconds, maximum_bytes=131072, cwd=None):
    """Run an exact argv without a shell and return bounded text."""
    try:
        completed = subprocess.run(
            [str(item) for item in argv], cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "returncode": None, "stdout": "", "stderr": "command timed out"}
    except OSError as exc:
        return {"status": "unavailable", "returncode": None, "stdout": "", "stderr": type(exc).__name__}
    stdout = completed.stdout[:maximum_bytes].decode("utf-8", errors="replace")
    stderr = completed.stderr[:4096].decode("utf-8", errors="replace")
    return {
        "status": "ok" if completed.returncode == 0 else "error",
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def read_regular_file_no_follow(path, maximum_bytes):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("approved path is not a regular file")
        chunks = []
        remaining = maximum_bytes
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)


def read_regular_file_tail_no_follow(path, maximum_bytes):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("approved path is not a regular file")
        start = max(0, info.st_size - maximum_bytes)
        os.lseek(descriptor, start, os.SEEK_SET)
        data = os.read(descriptor, maximum_bytes)
        if start and b"\n" in data:
            data = data.split(b"\n", 1)[1]
        return data.decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)
