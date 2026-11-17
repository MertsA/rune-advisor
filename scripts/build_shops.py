"""Build data/shops.json: items sold for coins in shops (ironmen can buy from shops).

Usage: python scripts/build_shops.py

From the wiki's Bucket:Storeline (https://runescape.wiki, CC BY-NC-SA 3.0). Only coin shops with a
stock and a price are kept (reward shops with other currencies, historical stock and free items are
dropped): {item: [[shop, stock, price, members], ...]}, cheapest first.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/shops.json"


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def main() -> None:
    shops: dict[str, list] = {}
    fields = ["page_name", "sold_item", "store_stock", "store_sell_price", "store_currency", "is_historical",
              "is_members_only"]
    for r in wiki.bucket_all(CachedClient(), "storeline", fields):
        stock, price = _num(r.get("store_stock")), _num(r.get("store_sell_price"))
        if r.get("is_historical") or r.get("store_currency") != ["coins"] or not stock or not price:
            continue
        # Like the item bucket, Storeline sets is_members_only to an empty value on members' shops and leaves
        # the key out otherwise, so the key's presence is the flag (testing for True marked every shop free).
        shops.setdefault(r["sold_item"], []).append([r["page_name"], int(stock), price, "is_members_only" in r])
    for offers in shops.values():
        offers.sort(key=lambda o: (o[2], -o[1]))
    OUT.write_text(json.dumps({"source": "https://runescape.wiki Bucket:Storeline (CC BY-NC-SA 3.0)",
                               "items": shops}, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(shops)} items sold in coin shops -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
