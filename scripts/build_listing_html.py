"""Render the Plugin Hub (Mod.io) description as HTML.

    uv run python scripts/build_listing_html.py [--check]

Mod.io's description field takes a subset of HTML, not Markdown. The text is written once, in
plugins/rune_advisor_export/README.md (everything below its first `---` line), and rendered here to
plugins/listing/description.html, which is what gets pasted into the field. Only plain tags come out:
headings, paragraphs, lists, links, strong, em and code. `--check` exits 1 when the file is out of date.
"""

from __future__ import annotations

import sys
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "plugins" / "rune_advisor_export" / "README.md"
OUT = ROOT / "plugins" / "listing" / "description.html"


def render() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    head, sep, body = text.partition("\n---\n")
    if not sep:
        raise SystemExit(f"{SOURCE}: no '---' line separating the notes from the description")
    md = MarkdownIt("commonmark")
    # A line break inside a paragraph becomes a space: some editors show a pasted newline as a break.
    md.renderer.rules["softbreak"] = lambda *args, **kwargs: " "
    return md.render(body.strip() + "\n")


def main(argv: list[str]) -> int:
    rendered = render()
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else None
    if "--check" in argv:
        if current != rendered:
            print(f"out of date: {OUT.relative_to(ROOT)}")
            return 1
        print("the listing description matches its source")
        return 0
    if current != rendered:
        OUT.write_text(rendered, encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
