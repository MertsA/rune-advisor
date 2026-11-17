"""Build data/disassembly.json: what disassembling each item gives, from the wiki's Bucket:Disassembly.

Usage: python scripts/build_disassembly.py

Per item: how many material rolls one item gets (`draws` = mat_quantity / item_quantity: runes and ammo
disassemble 50 at a time), the normal materials' weights (out of `total`), the base junk chance in percent per
junk chance reduction research tier (0-9, the Junk page), the special components (`chance` percent per
disassembly batch, `quantity`), Invention xp per item, and the item level. Augmented items and items that
transform when disassembled are left out: they're the player's gear, not a parts source.

Source: RuneScape Wiki, Bucket:Disassembly (https://runescape.wiki, CC BY-NC-SA 3.0).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/disassembly.json"


def _num(x, default=0.0) -> float:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return default


def main() -> None:
    rows = wiki.bucket_all(CachedClient(), "disassembly", ["page_name", "name", "json"])
    items: dict[str, dict] = {}
    for row in rows:
        d = json.loads(row.get("json") or "{}")
        name = d.get("name") or row.get("name")
        if not name or name in items or d.get("augmented") or d.get("transformed_items") or not d.get("materials"):
            continue  # first version of a page wins; augmented/transforming gear is never a parts source
        per = _num(d.get("item_quantity"), 1.0) or 1.0
        materials = {m: _num(w) for m, w in d["materials"].items() if _num(w) > 0}
        items[name] = {
            "level": int(_num(d.get("level"), 1)),
            "batch": per,
            "draws": _num(d.get("mat_quantity"), 1.0) / per,
            "materials": materials,
            "total": _num(d.get("totalweight")) or sum(materials.values()),
            "junk": [_num(j) for j in d.get("junk") or []],
            "special": {m: {"chance": _num(s.get("chance")), "quantity": _num(s.get("quantity"), 1.0)}
                        for m, s in (d.get("special") or {}).items()},
            "xp": _num(d.get("xp")) / per,
        }
    OUT.write_text(json.dumps({"source": "https://runescape.wiki Bucket:Disassembly (CC BY-NC-SA 3.0)",
                               "items": items}, separators=(",", ":"), sort_keys=True))
    print(f"wrote {len(items)} items to {OUT}")


if __name__ == "__main__":
    main()
