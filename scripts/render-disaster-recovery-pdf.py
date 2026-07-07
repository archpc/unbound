#!/usr/bin/env python3
"""
Render DISASTER-RECOVERY.md into a clean, Apple-manual-styled PDF.

Usage:  python3 scripts/render-disaster-recovery-pdf.py

Regenerate this any time DISASTER-RECOVERY.md changes — the PDF is a build
artifact (gitignored), never hand-edited. Requires Google Chrome for the
--print-to-pdf headless render; no other dependency (pandoc/weasyprint/etc.
are not installed on Honk, so this hand-rolls the small Markdown subset the
runbook actually uses: headers, tables, code fences, checklists, bold links,
one-level blockquote callouts, hr).
"""
import html
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "DISASTER-RECOVERY.md"
OUT_PDF = REPO / "DISASTER-RECOVERY.pdf"
TMP_HTML = REPO / "scripts" / ".dr-render-tmp.html"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', s)
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    return s


BLOCK_START = re.compile(r'^(#{1,4}\s|```|---\s*$|\s*\|\s*\S|> |\d+\.\s|- \[ \] |- )')


def merge_continuations(lines):
    """Join soft-wrapped source lines (Markdown 'lazy continuation') back into
    one logical line per paragraph/list-item, so a list item hand-wrapped
    across multiple source lines doesn't split into a bullet + orphan para."""
    merged = []
    in_fence = False
    for raw in lines:
        if raw.strip().startswith('```'):
            in_fence = not in_fence
            merged.append(raw)
            continue
        if in_fence or not raw.strip():
            merged.append(raw)
            continue
        if BLOCK_START.match(raw) or not merged or not merged[-1].strip():
            merged.append(raw)
            continue
        prev = merged[-1]
        if prev.strip().startswith('|'):
            merged.append(raw)
        else:
            merged[-1] = prev.rstrip() + ' ' + raw.strip()
    return merged


def parse_table(lines, i):
    header = [c.strip() for c in lines[i].strip().strip('|').split('|')]
    i += 2
    rows = []
    while i < len(lines) and lines[i].strip().startswith('|'):
        rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
        i += 1
    out = ['<table>', '<thead><tr>' + ''.join(f'<th>{inline(h)}</th>' for h in header) + '</tr></thead>', '<tbody>']
    for r in rows:
        out.append('<tr>' + ''.join(f'<td>{inline(c)}</td>' for c in r) + '</tr>')
    out.append('</tbody></table>')
    return '\n'.join(out), i


def convert(md):
    lines = merge_continuations(md.split('\n'))
    out = []
    i = 0
    in_code = False
    code_buf = []
    list_stack = []
    para_buf = []

    def flush_para():
        if para_buf:
            out.append('<p>' + inline(' '.join(para_buf)) + '</p>')
            para_buf.clear()

    def close_lists():
        while list_stack:
            out.append(f'</{list_stack.pop()}>')

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith('```'):
            flush_para()
            if not in_code:
                in_code = True
                code_buf = []
            else:
                in_code = False
                block = '\n'.join(code_buf)
                is_diagram = any(ch in block for ch in '┌┐└┘─│▼▶◀')
                cls = ' class="diagram"' if is_diagram else ''
                out.append(f'<pre{cls}><code>' + html.escape(block) + '</code></pre>')
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        if not line.strip():
            flush_para()
            close_lists()
            i += 1
            continue

        if line.strip() == '---':
            flush_para(); close_lists()
            out.append('<hr/>')
            i += 1
            continue

        m = re.match(r'^(#{1,4})\s+(.*)', line)
        if m:
            flush_para(); close_lists()
            level = len(m.group(1))
            out.append(f'<h{level}>{inline(m.group(2))}</h{level}>')
            i += 1
            continue

        m = re.match(r'^> \*\*(.+?)\*\*\s*(.*)', line)
        if m:
            flush_para(); close_lists()
            quote_lines = [m.group(0)[2:]]
            j = i + 1
            while j < len(lines) and lines[j].startswith('>'):
                quote_lines.append(lines[j][2:] if lines[j].startswith('> ') else lines[j][1:])
                j += 1
            out.append(f'<div class="callout"><p>{inline(" ".join(quote_lines))}</p></div>')
            i = j
            continue

        if re.match(r'^\s*\|', line):
            flush_para(); close_lists()
            table_html, i = parse_table(lines, i)
            out.append(table_html)
            continue

        m = re.match(r'^- \[ \] (.*)', line)
        if m:
            flush_para()
            if not list_stack or list_stack[-1] != 'ul-check':
                close_lists()
                out.append('<ul class="checklist">')
                list_stack.append('ul-check')
            out.append(f'<li><span class="box"></span>{inline(m.group(1))}</li>')
            i += 1
            continue

        m = re.match(r'^- (.*)', line)
        if m:
            flush_para()
            if not list_stack or list_stack[-1] != 'ul':
                close_lists()
                out.append('<ul>')
                list_stack.append('ul')
            out.append(f'<li>{inline(m.group(1))}</li>')
            i += 1
            continue

        m = re.match(r'^\d+\.\s+(.*)', line)
        if m:
            flush_para()
            if not list_stack or list_stack[-1] != 'ol':
                close_lists()
                out.append('<ol>')
                list_stack.append('ol')
            out.append(f'<li>{inline(m.group(1))}</li>')
            i += 1
            continue

        m = re.match(r'^\*(.+?)\*$', line.strip())
        if m and not line.strip().startswith('**'):
            flush_para(); close_lists()
            out.append(f'<p class="italic-note">{inline(m.group(1))}</p>')
            i += 1
            continue

        para_buf.append(line.strip())
        i += 1

    flush_para(); close_lists()
    return '\n'.join(out)


STYLE = """
  @page {
    size: Letter;
    margin: 22mm 18mm 20mm 18mm;
    @bottom-center {
      content: "Mac Mini Recovery Runbook  —  " counter(page);
      font-family: -apple-system, "SF Pro Text", Helvetica, Arial, sans-serif;
      font-size: 8.5pt;
      color: #9a9a9a;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 10.3pt;
    line-height: 1.55;
    color: #1d1d1f;
    -webkit-font-smoothing: antialiased;
  }
  h1 {
    font-size: 22pt; font-weight: 700; letter-spacing: -0.02em;
    margin: 0 0 6pt 0; color: #1d1d1f; page-break-before: always;
  }
  body > h1:first-of-type { page-break-before: avoid; }
  h2 {
    font-size: 15pt; font-weight: 600; letter-spacing: -0.01em;
    margin: 26pt 0 10pt 0; padding-top: 4pt;
    border-top: 0.75pt solid #d2d2d7; color: #1d1d1f;
    page-break-after: avoid;
  }
  h2:first-of-type { border-top: none; margin-top: 4pt; }
  h3 { font-size: 12pt; font-weight: 600; margin: 16pt 0 6pt 0; color: #1d1d1f; page-break-after: avoid; }
  p { margin: 0 0 9pt 0; }
  strong { font-weight: 600; }
  a { color: #0066cc; text-decoration: none; }
  code {
    font-family: "SF Mono", "Menlo", "Courier New", monospace;
    font-size: 9pt; background: #f2f2f4; padding: 1.5pt 4pt;
    border-radius: 4pt; color: #d1381f;
  }
  pre {
    background: #1d1d1f; color: #f2f2f4; padding: 10pt 12pt;
    border-radius: 8pt; margin: 8pt 0 12pt 0; page-break-inside: avoid;
  }
  pre code {
    background: none; color: #f2f2f4; padding: 0; font-size: 8.7pt; line-height: 1.5;
    white-space: pre-wrap; overflow-wrap: anywhere; display: block;
  }
  pre.diagram code { white-space: pre; overflow-wrap: normal; font-size: 8pt; }
  table { width: 100%; border-collapse: collapse; margin: 10pt 0 14pt 0; font-size: 9.3pt; page-break-inside: avoid; }
  th { text-align: left; font-weight: 600; padding: 5pt 8pt; border-bottom: 1pt solid #1d1d1f; color: #1d1d1f; }
  td { padding: 5pt 8pt; border-bottom: 0.5pt solid #e2e2e5; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  ul, ol { margin: 6pt 0 12pt 0; padding-left: 18pt; }
  ul:not(.checklist) { list-style: none; padding-left: 4pt; }
  ul:not(.checklist) > li { position: relative; padding-left: 15pt; }
  ul:not(.checklist) > li::before {
    content: ""; position: absolute; left: 2pt; top: 6.5pt;
    width: 4pt; height: 4pt; border-radius: 50%; background: #86868b;
  }
  li { margin: 3pt 0; }
  ul.checklist { list-style: none; padding-left: 0; margin: 8pt 0 14pt 0; }
  ul.checklist li { position: relative; padding-left: 22pt; margin: 5pt 0; }
  ul.checklist .box {
    position: absolute; left: 0; top: 2.5pt; width: 10pt; height: 10pt;
    border: 1.25pt solid #86868b; border-radius: 2.5pt; display: inline-block;
  }
  .callout {
    background: #f5f7fb; border-left: 3pt solid #0066cc;
    border-radius: 0 6pt 6pt 0; padding: 9pt 12pt; margin: 10pt 0 14pt 0;
    page-break-inside: avoid;
  }
  .callout p { margin: 0; font-size: 9.7pt; }
  hr { border: none; border-top: 0.75pt solid #d2d2d7; margin: 18pt 0; }
  .italic-note { font-style: italic; color: #6e6e73; font-size: 9.3pt; margin-top: -4pt; }
  .cover { page-break-after: always; padding-top: 30mm; }
  .cover .kicker {
    font-size: 11pt; font-weight: 600; color: #0066cc; letter-spacing: 0.04em;
    text-transform: uppercase; margin-bottom: 14pt;
  }
  .cover h1 { page-break-before: avoid; font-size: 32pt; margin-bottom: 10pt; }
  .cover .subtitle { font-size: 13pt; color: #6e6e73; margin-bottom: 40pt; line-height: 1.5; }
  .cover .metaline {
    font-size: 9.5pt; color: #86868b; border-top: 0.75pt solid #d2d2d7;
    padding-top: 10pt; margin-top: 60pt;
  }
"""

COVER = """
<div class="cover">
  <div class="kicker">Field Manual</div>
  <h1>Mac Mini DNS + Monitoring<br/>Recovery Runbook</h1>
  <div class="subtitle">Full rebuild procedure for the Unbound DNS resolver,<br/>
  Prometheus + Grafana monitoring stack, and supporting exporters<br/>
  running on <strong>mini</strong> (192.168.0.100).</div>
  <div class="metaline">Source of truth: DISASTER-RECOVERY.md in this repository &nbsp;·&nbsp; For use during an actual outage</div>
</div>
"""


def find_chrome():
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    sys.exit("No Chrome/Chromium found at any known path — install Google Chrome or edit CHROME_CANDIDATES.")


def main():
    md = SRC.read_text()
    body = convert(md)
    html_doc = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Mac Mini DNS + Monitoring Recovery Runbook</title>"
        f"<style>{STYLE}</style></head><body>{COVER}{body}</body></html>"
    )
    TMP_HTML.write_text(html_doc)

    chrome = find_chrome()
    subprocess.run([
        chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
        f"--print-to-pdf={OUT_PDF}", "--print-to-pdf-no-header",
        f"file://{TMP_HTML}",
    ], check=True, capture_output=True)
    TMP_HTML.unlink(missing_ok=True)
    print(f"wrote {OUT_PDF}")


if __name__ == "__main__":
    main()
