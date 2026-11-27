#!/usr/bin/env python3
"""Write the exporter's static variable manifest: widths, kinds and the family blocks of
plugins/rune_advisor_export/lib/exported_vars.lua, and the server's copy in src/rune_advisor/wire_manifests/.

    uv run python scripts/build_exported_vars.py [--dump state/vars/var_ids.json]   # rewrite both
    uv run python scripts/build_exported_vars.py --check                            # verify, change nothing

exported_vars.lua is edited by hand: which variables, in which groups, with what comments. This script fills in
what the game decides, from the dev plugin's dump of every player variable ("Dump var names": name, id, base
var and mask, saved as state/vars/var_ids.json):
  - each entry's kind and width: "bit" at its mask's width for a VarBit, "var" 32 for a whole player variable.
    An entry already marked "client" (a client variable, which the dump doesn't hold) or "chat" / "ui" (a value read from
    the game's messages or its interface) is kept as it is. A name
    the dump doesn't have is an error: fix the list.
  - every block between "-- family PREFIX: ..." and "-- end family PREFIX": every variable whose name starts
    with PREFIX, sorted, except those already listed elsewhere in the file and those the quest and achievement
    tests already carry whole (below).
A variable some quest or achievement test reads travels in the config-derived part of the wire anyway, and the
server hands the planner every one of those it carries whole, by name (BaseManifest.exact_fields, from the packaged
game configs). Listing one here would send it twice, so a family leaves them out and a hand-written entry for one is
an error (the user, 2026-11-27: read achievements before adding exports).
The server's copy is the manifest by hash (wire.manifest_hash: the entries in order, "NAME/width,..."), so every
released manifest stays there and stored states and history posted under it can always be read. A run never
deletes an older manifest file.

--check (the test suite runs it): the Lua file's manifest is in wire_manifests/ and the same, entry for entry.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rune_advisor import wire  # noqa: E402

LUA = ROOT / "plugins" / "rune_advisor_export" / "lib" / "exported_vars.lua"
DUMP = ROOT / "state" / "vars" / "var_ids.json"
COMMENT_COLUMN = 52
FAMILY = re.compile(r"^\s*-- family ([A-Z0-9_]+_):")
END_FAMILY = re.compile(r"^\s*-- end family ([A-Z0-9_]+_)\s*$")
ENTRY = re.compile(r'^(\s*)\{ "([A-Za-z0-9_]+)", (\d+), "(var|bit|client)" \},\s*(--.*)?$')


def field_of(row: list) -> tuple[int, str]:
    """(width, kind) of a dump row [name, id, base, mask]."""
    base, mask = (row + [None, None])[2:4]
    if base and isinstance(mask, int) and mask:
        mask &= 0xFFFFFFFF
        start = (mask & -mask).bit_length() - 1
        return (mask >> start).bit_length(), "bit"
    return 32, "var"


def entry_line(indent: str, name: str, width: int, kind: str, comment: str | None) -> str:
    line = f'{indent}{{ "{name}", {width}, "{kind}" }},'
    return (line.ljust(COMMENT_COLUMN) if len(line) < COMMENT_COLUMN else line + " ") + comment if comment else line


def carried() -> set[str]:
    """Variables the packaged game configs' tests already carry whole."""
    from rune_advisor.sources import game_configs as gc

    configs = gc.load()
    return gc.base_manifest(configs).exact_fields() if configs is not None else set()


def rebuild(text: str, dump: dict[str, list], skip: set[str] = frozenset()) -> str:
    lines = text.split("\n")
    # Names listed by hand, outside the family blocks: a family leaves them out. (The blocks' own members are
    # not counted, or a second run would see every member as listed elsewhere and empty its block.)
    listed, inside = set(), None
    for line in lines:
        if inside is not None:
            if (m := END_FAMILY.match(line)) and m.group(1) == inside:
                inside = None
        elif m := FAMILY.match(line):
            inside = m.group(1)
        elif m := ENTRY.match(line):
            listed.add(m.group(2))
    out, family, problems, redundant = [], None, [], []
    for line in lines:
        if family is not None:  # inside a family block: drop the old members, write the new at its end
            if (m := END_FAMILY.match(line)) and m.group(1) == family:
                members = sorted(n for n in dump if n.startswith(family) and n not in listed and n not in skip)
                out += [entry_line("    ", n, *field_of(dump[n]), None) for n in members]
                out.append(line)
                family = None
            continue
        if m := FAMILY.match(line):
            family = m.group(1)
            out.append(line)
            continue
        if m := ENTRY.match(line):
            indent, name, width, kind, comment = m.group(1), m.group(2), int(m.group(3)), m.group(4), m.group(5)
            if name in skip:
                redundant.append(name)
            if name in dump:
                width, kind = field_of(dump[name])
            elif kind not in ("client", "chat", "ui"):
                problems.append(name)
            out.append(entry_line(indent, name, width, kind, comment))
            continue
        out.append(line)
    if family is not None:
        sys.exit(f"build_exported_vars: the family {family} has no '-- end family' line")
    if problems:
        sys.exit("build_exported_vars: not in the game's variables (fix the list): " + ", ".join(problems))
    if redundant:
        sys.exit("build_exported_vars: quest or achievement tests already carry these whole (the planner gets them "
                 "by name; remove them): " + ", ".join(redundant))
    return "\n".join(out)


def manifest_path(fields: list[tuple[str, int, str]]) -> Path:
    return wire.MANIFEST_DIR / f"{wire.manifest_hash(fields):08x}.json"


def write_manifest(fields: list[tuple[str, int, str]]) -> Path:
    path = manifest_path(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fields": [list(f) for f in fields]}, indent=0, separators=(",", ":")) + "\n")
    return path


def check() -> list[str]:
    fields = wire.parse_exported_vars(LUA.read_text())
    names = [n for n, _, _ in fields]
    problems = []
    if len(set(names)) != len(names):
        problems.append("a name is listed twice")
    if any(w < 1 or w > 32 or (k != "bit" and w != 32) for _, w, k in fields):
        problems.append("a width is out of range (1-32, and 32 for var and client entries)")
    path = manifest_path(fields)
    if not path.is_file():
        problems.append(f"{path.relative_to(ROOT)} is missing: run scripts/build_exported_vars.py")
    elif wire.load_manifest(wire.manifest_hash(fields)) != tuple(fields):
        problems.append(f"{path.relative_to(ROOT)} differs from exported_vars.lua")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", type=Path, default=DUMP, help="the dev plugin's var dump (default: %(default)s)")
    ap.add_argument("--check", action="store_true", help="verify the Lua list against wire_manifests/, write nothing")
    args = ap.parse_args()
    if args.check:
        problems = check()
        for p in problems:
            print("build_exported_vars:", p)
        return 1 if problems else 0
    dump = {row[0]: row for row in json.loads(args.dump.read_text())}
    text = rebuild(LUA.read_text(), dump, carried())
    LUA.write_text(text)
    fields = wire.parse_exported_vars(text)
    path = write_manifest(fields)
    bits = sum(w for _, w, _ in fields)
    print(f"{len(fields)} fields, {bits} bits; manifest {path.relative_to(ROOT)}")
    return 1 if check() else 0


if __name__ == "__main__":
    sys.exit(main())
