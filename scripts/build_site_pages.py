#!/usr/bin/env python3
"""Render the privacy policy and the terms of use from their documents into the site's pages.

The documents in docs/policy/ are the authoritative text: edit those, never the HTML. Each is Markdown with
a short header of `key: value` lines between `---` marks:

    page:      the folder under site/ the page is written to (site/<page>/index.html)
    revision:  the line shown under the title ("Draft of ..., nth revision.")
    draft:     optional; when present the page is marked a draft and this text follows "Draft, not in force."

The first `# heading` is the page's title. `[OPEN n: ...]` in the text marks an undecided passage and is
highlighted on the page. Tables are wrapped so they scroll on a narrow screen.

    uv run python scripts/build_site_pages.py            write the pages
    uv run python scripts/build_site_pages.py --check    exit 1 if a page is not what its document renders to

The pages are committed (the site is served as plain files with no build step on the server), and a test
keeps them in step with the documents.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "docs" / "policy"
SITE = ROOT / "site"
OPEN_MARK = re.compile(r"\[OPEN (?:\d+|n):\s[^\]]*\]")

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{window_title} - Rune Advisor</title>
  <meta name="referrer" content="no-referrer">
  <link rel="icon" href="/assets/icon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/assets/site.css">
</head>
<body>
  <header class="site">
    <a class="name" href="/">Rune Advisor</a>
    <nav>
      <a href="/docs/">Docs</a>
      <a href="/docs/data/">What is sent</a>
      <a href="/account/">Your account</a>
      <a href="/privacy/">Privacy</a>
      <a href="/terms/">Terms</a>
    </nav>
  </header>
  <main>
<!-- Rendered from docs/policy/{source} by scripts/build_site_pages.py. Edit the document, not this page. -->
{body}  </main>
  <footer class="site">
    <p>Rune Advisor is a fan-made tool. Created using intellectual property belonging to Jagex Limited under the terms of Jagex's Fan Content Policy. This content is not endorsed by or affiliated with Jagex.</p>
    <p>RuneScape is a
    trademark of Jagex Ltd. Game data in the planner comes from the
    <a href="https://runescape.wiki/">RuneScape Wiki</a> (CC BY-NC-SA 3.0).</p>
    <p>Contact: <a href="mailto:support@runeadvisor.com">support@runeadvisor.com</a>.
    Privacy: <a href="mailto:privacy@runeadvisor.com">privacy@runeadvisor.com</a>.</p>
  </footer>
</body>
</html>
"""


def _mark_open(rendered: str) -> str:
    return OPEN_MARK.sub(lambda m: f'<span class="open">{m.group(0)}</span>', rendered)


def render(source: Path) -> tuple[str, str]:
    """(page folder, the page's HTML) for one document."""
    text = source.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SystemExit(f"{source}: no header")
    header, _, body = text[4:].partition("\n---\n")
    meta = dict((key.strip(), value.strip()) for key, _, value in (line.partition(":") for line in header.splitlines() if line.strip()))
    title_match = re.search(r"^# (.+)$", body, flags=re.M)
    if not title_match or "page" not in meta or "revision" not in meta:
        raise SystemExit(f"{source}: needs `page`, `revision` and a `# title`")
    title = title_match.group(1).strip()
    body = body[:title_match.start()] + body[title_match.end():]
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    content = md.render(body)
    content = content.replace("<table>", '<div class="scroll"><table>').replace("</table>", "</table></div>")
    top = f"<h1>{html.escape(title)}</h1>\n"
    if meta.get("draft"):
        top += f'<div class="draft"><strong>Draft, not in force.</strong> {md.renderInline(meta["draft"])}</div>\n'
    top += f'<p class="muted">{html.escape(meta["revision"])}</p>\n'
    window_title = title + (" (draft)" if meta.get("draft") else "")
    return meta["page"], PAGE.format(window_title=html.escape(window_title), source=source.name, body=_mark_open(top + content))


def pages() -> dict[Path, str]:
    """Every page a document renders to: path under site/ -> HTML."""
    out = {}
    for source in sorted(SOURCES.glob("*.md")):
        page, rendered = render(source)
        out[SITE / page / "index.html"] = rendered
    return out


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = 0
    for path, rendered in pages().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == rendered:
            continue
        stale += 1
        if check:
            print(f"out of date: {path.relative_to(ROOT)}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
    if check and not stale:
        print("site pages match their documents")
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
