"""Embed wiki recipe data into data/wiki_rates.json rows, expanding same-skill intermediates.

Usage: python scripts/embed_wiki_recipes.py <methods.json> <mapping.json> [rates.json]
       python scripts/embed_wiki_recipes.py <combined.json> [rates.json]

rates.json defaults to data/wiki_rates.json (use data/wiki_rates_f2p.json for the free-to-play rows).

methods.json / mapping.json come from the method -> product mapping pass (a model
names, per extracted method, the product it makes; a person reviews the result): mapping[k]["output"] is the product made by methods[k]["i"].
A combined file holds one entry per row with the row index inline ({"i": 30, "output": "Fire", ...});
scripts/f2p_recipe_mapping.json is the hand-written one for the free-to-play guides.

A method like "elder rune burial sets" makes its product from intermediates (+5 armour, made
from +4, ... from bars) that are made with the same skill, and every step gives xp. We expand
those intermediates recursively so a variant's `xp` is the total skill xp per final product and
its `materials` are the inputs that aren't made with this skill (bars, grapes, logs...).

Besides `output`, a mapping entry may say:
- `input`: keep only the output's recipes that consume this material ("Fire" is the output of every
  log; "Oak logs, bonfire" wants the oak variant alone);
- `variants`: literal variants for things the recipe bucket records without an output (burying bones,
  scattering ashes: the bones page's production data lists the xp but makes nothing), with a `source`
  note; embedded as given, `match` = "hand";
- `spell`: a spell from data/mechanics.json cast for its own xp (teleports): the variant's materials are
  its runes less the elemental rune it needs most of (an elemental staff supplies that one), cast every
  `cooldown` ticks (SPELL_TICKS when none is listed), like the catalogue's teleport entries.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.mechanics import data as mechanics_data  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

RATES = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/wiki_rates.json"
MAX_DEPTH = 12
SPELL_TICKS = 4  # a teleport without a listed cooldown (scripts/build_recipe_catalogue.py's TELEPORT_TICKS)
ELEMENTAL_RUNES = {"Air rune", "Water rune", "Earth rune", "Fire rune"}


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_recipes() -> dict[str, list[dict]]:
    by_output: dict[str, list[dict]] = {}
    for r in wiki.bucket_all(CachedClient(), "recipe", ["page_name", "production_json"]):
        d = json.loads(r["production_json"] or "{}")
        for o in d.get("outputs", []):
            by_output.setdefault(o["name"], []).append(d)
    return by_output


def skill_xp(recipe: dict, skill: str) -> float | None:
    return next((_num(s.get("experience")) for s in recipe.get("skills", []) if s.get("name") == skill), None)


def expand(recipes: dict, recipe: dict, output: str, skill: str, depth: int = 0, seen: frozenset = frozenset()):
    """(xp per one unit of `output`, {raw material: qty per unit}) for a recipe, expanding
    materials that are themselves made with `skill`. Returns None if the chain can't be resolved."""
    out_qty = _num(next(o for o in recipe["outputs"] if o["name"] == output).get("quantity"), 1) or 1
    xp = skill_xp(recipe, skill) or 0.0
    raw: dict[str, float] = {}
    for m in recipe.get("materials", []):
        name, qty = m["name"], _num(m.get("quantity"), 1)
        sub = next((r for r in recipes.get(name, []) if skill_xp(r, skill)), None)
        if sub is not None and depth < MAX_DEPTH and name not in seen:
            inner = expand(recipes, sub, name, skill, depth + 1, seen | {output})
            if inner is None:
                return None
            sub_xp, sub_raw = inner
            xp += qty * sub_xp
            for k, v in sub_raw.items():
                raw[k] = raw.get(k, 0) + qty * v
        else:
            raw[name] = raw.get(name, 0) + qty
    return xp / out_qty, {k: v / out_qty for k, v in raw.items()}


def spell_variant(name: str) -> dict | None:
    """Casting `name` for its xp: runes less the staff's element, every cooldown (or SPELL_TICKS) ticks."""
    spell = next((s for s in mechanics_data().get("spells", []) if s.get("name") == name), None)
    if spell is None or not spell.get("runes") or not _num(spell.get("xp")):
        return None
    runes = sorted(((n, float(q)) for n, q in spell["runes"]), key=lambda r: r[0])
    staff = max((r for r in runes if r[0] in ELEMENTAL_RUNES), key=lambda r: r[1], default=None)
    materials = [[n, q] for n, q in runes if (n, q) != staff]
    return {"output_qty": 1, "xp": float(spell["xp"]), "ticks": _num(spell.get("cooldown")) or SPELL_TICKS,
            "materials": materials, **({"assumes": f"a staff supplies the {staff[0].lower()}s"} if staff else {})}


def bucket_variants(recipes: dict, output: str, skill: str, input_name: str | None = None) -> list[dict]:
    """Variants from the wiki's recipes for `output` that give `skill` xp (and use `input_name`, if given)."""
    variants = []
    for recipe in recipes.get(output, []):
        if not skill_xp(recipe, skill):
            continue
        if input_name and not any(m.get("name") == input_name for m in recipe.get("materials", [])):
            continue
        expanded = expand(recipes, recipe, output, skill)
        if expanded is None:
            continue
        xp, raw = expanded
        # Per final product; output_qty 1 so actions == products.
        v = {"output_qty": 1, "xp": round(xp, 3), "ticks": _num(recipe.get("ticks")),
             "materials": [[k, round(q, 4)] for k, q in sorted(raw.items())]}
        if v not in variants:
            variants.append(v)
    return variants


def embed(rows: list[dict], mapping: list[dict], recipes: dict) -> int:
    """Embed each mapping entry's recipe into rows[entry["i"]]; returns how many rows got one."""
    linked = 0
    for m in mapping:
        row = rows[m["i"]]
        row.pop("recipe", None)
        output = m.get("output")
        if m.get("variants"):
            variants, match = [dict(v, output_qty=v.get("output_qty", 1)) for v in m["variants"]], "hand"
        elif m.get("spell"):
            v = spell_variant(m["spell"])
            variants, match, output = [v] if v else [], "spell", output or m["spell"]
        elif output:
            variants, match = bucket_variants(recipes, output, row["skill"], m.get("input")), m.get("confidence")
        else:
            continue
        if variants:
            row["recipe"] = {"output": output, "variants": variants, "match": match,
                             **({"source": m["source"]} if m.get("source") else {})}
            linked += 1
        else:
            print(f"no usable recipe: {row['skill']} / {row['method']} -> {output}", file=sys.stderr)
    return linked


def main(first: Path, second: Path | None = None, third: Path | None = None) -> None:
    mapping = json.loads(first.read_text())
    if mapping and "i" in mapping[0]:  # combined file: {"i": row index, ...mapping}
        rates_file = second or RATES
    else:
        methods = mapping
        mapping = json.loads(second.read_text())
        assert len(methods) == len(mapping)
        mapping = [{**m, "i": meth["i"]} for meth, m in zip(methods, mapping)]
        rates_file = third or RATES
    rows = json.loads(rates_file.read_text())
    linked = embed(rows, mapping, load_recipes())
    rates_file.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    print(f"linked {linked} methods to recipes", file=sys.stderr)


if __name__ == "__main__":
    main(*(Path(a) for a in sys.argv[1:4]))
