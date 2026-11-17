"""Check model-extracted training rates against the wiki source they claim to quote.

Usage: python scripts/validate_wiki_rates.py <guides_dir> <rates_dir> <out.json>

Each rates/<page>.json row (see the extraction instructions) must carry a `quote` that appears
verbatim in guides/<page>.wiki, and every xp/h number in the row must appear in that quote
(as 86,000 / 86000 / 86k / 0.086m ...). Accepted rows are written to <out.json> with their
source page; rejected rows are listed on stderr with the reason.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?")


def numbers_in(text: str) -> set[int]:
    found = set()
    for digits, suffix in _NUMBER.findall(text):
        value = float(digits.replace(",", ""))
        if suffix:
            value *= 1_000 if suffix.lower() == "k" else 1_000_000
        # Accept either neighbour of a fractional figure (194,512.5 may be written 194512 or 194513).
        found.update({math.floor(value), math.ceil(value)})
    return found


def check_row(row: dict, source: str) -> str | None:
    """Return a rejection reason, or None if the row is grounded in the source."""
    quote = row.get("quote") or ""
    if not quote or quote not in source:
        return "quote not found verbatim in source"
    stated = numbers_in(quote)
    for field in ("xp_per_hour_low", "xp_per_hour_high"):
        value = row.get(field)
        # numbers_in rounds decimals, so a rounded fractional figure (113,513.514 -> 113514) matches.
        if value is not None and value not in stated:
            return f"{field}={value} not in quote"
    if row.get("xp_per_hour_low") is None and row.get("xp_per_hour_high") is None:
        return "no xp/h number"
    if not isinstance(row.get("level_min"), int):
        return "missing level_min"
    return None


def main(guides_dir: Path, rates_dir: Path, out: Path) -> None:
    accepted, rejected = [], 0
    for rates_file in sorted(rates_dir.glob("*.json")):
        page = rates_file.stem
        source_file = guides_dir / f"{page}.wiki"
        if not source_file.exists():
            continue
        source = source_file.read_text()
        try:
            rows = json.loads(rates_file.read_text())
        except json.JSONDecodeError as e:
            print(f"{page}: invalid JSON ({e})", file=sys.stderr)
            continue
        kept = 0
        for row in rows:
            reason = check_row(row, source)
            if reason:
                rejected += 1
                print(f"{page}: REJECT {row.get('method')!r}: {reason}", file=sys.stderr)
            else:
                kept += 1
                accepted.append({**row, "source": "https://runescape.wiki/w/" + page})
        print(f"{page}: {kept}/{len(rows)} rows accepted", file=sys.stderr)
    out.write_text(json.dumps(accepted, indent=1, ensure_ascii=False))
    print(f"accepted {len(accepted)}, rejected {rejected} -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
