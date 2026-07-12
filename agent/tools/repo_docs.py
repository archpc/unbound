"""Deterministic search over the canonical repository documentation allowlist."""

from __future__ import annotations

import re

from agent.tools.common import read_regular_file_no_follow


APPROVED_DOCUMENTS = (
    "README.md", "ARCHITECTURE.md", "RUNBOOK.md", "DEPLOYMENT.md",
    "TROUBLESHOOTING.md", "REFERENCE.md", "DISASTER-RECOVERY.md",
    "REPO_LAYOUT.md", "DASHBOARDS.md", "AGENTS.md", "CLAUDE.md",
)
WORD_RE = re.compile(r"[A-Za-z0-9_.-]{2,}")


def build_tool(config):
    def repo_docs(arguments):
        query = arguments["query"]
        terms = {term.lower() for term in WORD_RE.findall(query)}
        if not terms:
            return {"query": query, "matches": [], "limitation": "No searchable terms were supplied."}
        matches = []
        for filename in APPROVED_DOCUMENTS:
            path = config.repository_root / filename
            try:
                text = read_regular_file_no_follow(path, 2 * 1024 * 1024)
            except (OSError, ValueError):
                continue
            lines = text.splitlines()
            for index, line in enumerate(lines):
                lower = line.lower()
                score = sum(term in lower for term in terms)
                if not score:
                    continue
                start = max(0, index - 1)
                end = min(len(lines), index + 2)
                snippet = "\n".join(lines[start:end])[:1600]
                matches.append({"document": filename, "line": index + 1,
                                "score": score, "untrusted_excerpt": snippet})
        matches.sort(key=lambda item: (-item["score"], APPROVED_DOCUMENTS.index(item["document"]), item["line"]))
        return {"query": query, "matches": matches[:12],
                "limitation": "Deterministic text search only; excerpts are untrusted evidence."}
    return repo_docs
