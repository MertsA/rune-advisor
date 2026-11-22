"""Build data/shops.json (items sold for coins: {item: [[shop, stock, price, members], ...]}, cheapest first) and
data/currency_shops.json (items sold for anything else: reaper points, thaler, Dungeoneering tokens...:
{item: [[shop, stock, {currency: price}, members], ...]}). Historical stock and free items are dropped.

Usage: python scripts/build_shops.py

From the wiki's Bucket:Storeline (https://runescape.wiki, CC BY-NC-SA 3.0). Which currencies the planner can
earn, and how fast, is currencies.py's business; every currency is kept here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/shops.json"
CURRENCY_OUT = OUT.with_name("currency_shops.json")


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def main() -> None:
    shops: dict[str, list] = {}
    other: dict[str, list] = {}
    fields = ["page_name", "sold_item", "store_stock", "store_sell_price", "store_currency", "is_historical",
              "is_members_only"]
    for r in wiki.bucket_all(CachedClient(), "storeline", fields):
        stock, price = _num(r.get("store_stock")), _num(r.get("store_sell_price"))
        currencies = r.get("store_currency") or []
        if not r.get("is_historical") and currencies and currencies != ["coins"]:
            # Several currencies pay together ("30;30;294;30" Pizazz points); "inf"/missing stock is unlimited.
            prices = [_num(x) for x in str(r.get("store_sell_price") or "").split(";")]
            if len(prices) == len(currencies) and all(prices) and any(p > 0 for p in prices):
                stock_n = _num(r.get("store_stock"))
                other.setdefault(r["sold_item"], []).append(
                    [r["page_name"], int(stock_n) if stock_n else None, dict(zip(currencies, prices)),
                     "is_members_only" in r])
            continue
        if r.get("is_historical") or currencies != ["coins"] or not stock or not price:
            continue
        # Like the item bucket, Storeline sets is_members_only to an empty value on members' shops and leaves
        # the key out otherwise, so the key's presence is the flag (testing for True marked every shop free).
        shops.setdefault(r["sold_item"], []).append([r["page_name"], int(stock), price, "is_members_only" in r])
    for offers in shops.values():
        offers.sort(key=lambda o: (o[2], -o[1]))
    OUT.write_text(json.dumps({"source": "https://runescape.wiki Bucket:Storeline (CC BY-NC-SA 3.0)",
                               "items": shops}, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(shops)} items sold in coin shops -> {OUT}", file=sys.stderr)
    CURRENCY_OUT.write_text(json.dumps({"source": "https://runescape.wiki Bucket:Storeline (CC BY-NC-SA 3.0)",
                                        "items": dict(sorted(other.items()))}, ensure_ascii=False,
                                       separators=(",", ":")))
    print(f"{len(other)} items sold for other currencies -> {CURRENCY_OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
