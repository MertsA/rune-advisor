"""Replay the exporter's wire-protocol posts (plugins/rune_advisor_export/lib/wire.lua) the way the HTTP API
would receive them: rebuild the layout from the configs and the full post's directories, check every hash,
apply deltas, and hand back the final state as var/field values the planner's derivation understands.

Layout (must match lib/manifest.lua bit for bit; both hash their description with FNV-1a 32):
  base entries (game_configs.base_manifest, name order), hand-listed fields (each at its own width, from the
  directory's `fields`: [name, width] pairs; plugins up to 0.4.3 sent a `named` list, 32 bits each), skills (32
  bits each, SkillType order from the directory's `skills`), members (1 bit).
Manifest: the hand-listed fields (plugins/rune_advisor_export/lib/exported_vars.lua: name, width, kind), fixed per
plugin version. Since 0.5.0 a full post names it by hash (`manifest`, FNV-1a 32 of `manifest_description`); the
server holds every released manifest in wire_manifests/<hash>.json (scripts/build_exported_vars.py writes them). A
field the client lacks is sent as 0 and listed in the full post's `absent` (indexes into the manifest).
Canonical bytes: every slot at its width, LSB-first; then per inventory name (the post's `inventories`
order): u32 item count (0xFFFFFFFF = never loaded this session: the server keeps what it last knew), then by
item id: u32 id, u32 count. State hash: FNV-1a seeded with
FNV_OFFSET ^ layout hash. Delta body: u16 slot count, each u16 index + value at the slot's width; u16 item
count, each u8 inventory index, u32 item id, u32 count (0 = removed; item id 0xFFFFFFFF: the inventory is now
known, possibly empty, where the server had "unknown").
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
import sys
from array import array
from typing import Any

from .sources.game_configs import FNV_OFFSET, BaseEntry, BaseManifest, GameConfigs, base_manifest, fnv1a32


@dataclass
class WireSlot:
    kind: str    # "base" | "named" | "skill" | "members"
    name: str
    width: int
    entry: BaseEntry | None = None


MANIFEST_DIR = Path(__file__).parent / "wire_manifests"
FIELD_KINDS = ("var", "bit", "client", "chat", "ui")
_ENTRY = re.compile(r'^\s*\{ "([A-Za-z0-9_]+)", (\d+), "(var|bit|client|chat|ui)" \},')


def manifest_description(fields: list[tuple[str, int]]) -> str:
    """What a manifest's hash is the hash of (lib/manifest.lua `describeFields`): "NAME/width,..." in wire order."""
    return ",".join(f"{n}/{w}" for n, w, *_ in fields)


def manifest_hash(fields: list[tuple[str, int]]) -> int:
    return fnv1a32(manifest_description(fields))


def parse_exported_vars(text: str) -> list[tuple[str, int, str]]:
    """The entries of lib/exported_vars.lua in order: (name, width, kind)."""
    return [(m.group(1), int(m.group(2)), m.group(3)) for line in text.splitlines() if (m := _ENTRY.match(line))]


@cache
def load_manifest(h: int) -> tuple[tuple[str, int, str], ...] | None:
    """A released manifest by hash (name, width, kind per field), or None when this server doesn't have it."""
    path = MANIFEST_DIR / f"{h & 0xFFFFFFFF:08x}.json"
    if not path.is_file():
        return None
    fields = tuple((n, int(w), k) for n, w, k in json.loads(path.read_text())["fields"])
    return fields if manifest_hash(fields) == h else None


@dataclass
class WireLayout:
    slots: list[WireSlot]
    fields: list[tuple[str, int]]  # the hand-listed fields: (name, width)
    skills: list[str]
    inventories: list[str]
    manifest: BaseManifest

    configs_fingerprint: int | None = None
    legacy: bool = False  # a 0.4.x layout: a `named` list, every name 32 bits, described as "|named:"

    @classmethod
    def build(cls, configs: GameConfigs, fields: list[tuple[str, int]], skills: list[str], inventories: list[str],
              legacy: bool = False) -> "WireLayout":
        manifest = base_manifest(configs)
        slots = [WireSlot("base", e.base, e.width, e) for e in manifest.entries]
        slots += [WireSlot("named", n, w) for n, w in fields]
        slots += [WireSlot("skill", s, 32) for s in skills]
        slots.append(WireSlot("members", "members", 1))
        return cls(slots, [(n, w) for n, w in fields], list(skills), list(inventories), manifest, configs.fingerprint,
                   legacy)

    @classmethod
    def build_named(cls, configs: GameConfigs, named: list[str], skills: list[str], inventories: list[str]) -> "WireLayout":
        """A layout as plugins up to 0.4.3 described it: every hand-listed name 32 bits."""
        return cls.build(configs, [(n, 32) for n in named], skills, inventories, legacy=True)

    @property
    def named(self) -> list[str]:
        return [n for n, _ in self.fields]

    @property
    def manifest_hash(self) -> int:
        return manifest_hash(self.fields)

    def description(self) -> str:
        middle = ("|named:" + ",".join(self.named) if self.legacy
                  else "|fields:" + ",".join(f"{n}/{w}" for n, w in self.fields))
        return self.manifest.description() + middle + "|skills:" + ",".join(self.skills) + "|members"

    @property
    def hash(self) -> int:
        # A layout never changes after `build`, and its description is ~100 KB of text: hashing it on every
        # post was two thirds of a post's cost (2026-11-13), so it is done once.
        cached = self.__dict__.get("_hash")
        if cached is None:
            cached = self.__dict__["_hash"] = fnv1a32(self.description())
        return cached

    @property
    def total_bits(self) -> int:
        cached = self.__dict__.get("_bits")
        if cached is None:
            cached = self.__dict__["_bits"] = sum(s.width for s in self.slots)
        return cached


UNKNOWN = 0xFFFFFFFF


@dataclass
class WireState:
    slots: list[int]
    invs: dict[str, dict[int, int]]  # an inventory the plugin hasn't loaded is absent (unknown), never empty


class _Reader:
    def __init__(self, data: bytes):
        self.acc = int.from_bytes(data, "little")
        self.offset = 0

    def read(self, width: int) -> int:
        v = (self.acc >> self.offset) & ((1 << width) - 1)
        self.offset += width
        return v


class _Writer:
    def __init__(self):
        self.acc, self.n = 0, 0

    def write(self, value: int, width: int) -> None:
        self.acc |= (value & ((1 << width) - 1)) << self.n
        self.n += width

    def bytes(self) -> bytes:
        return self.acc.to_bytes((self.n + 7) // 8, "little")


def _words(values: list[int]) -> int:
    """32-bit values, little-endian, as one integer (what writing each with `_Writer.write(v, 32)` builds)."""
    words = array("I", values)
    if sys.byteorder == "big":
        words.byteswap()
    return int.from_bytes(words.tobytes(), "little")


def canonical(layout: WireLayout, state: WireState) -> bytes:
    """The state as bytes: the slots bit-packed in layout order, then per inventory a 32-bit count (UNKNOWN for
    one never loaded) and its (item id, count) pairs sorted by id. The inventory part is all 32-bit words, so it
    is packed in one go and shifted behind the slots instead of written bit by bit."""
    w = _Writer()
    for slot, value in zip(layout.slots, state.slots):
        w.write(value, slot.width)
    mask = 0xFFFFFFFF
    values: list[int] = []
    for name in layout.inventories:
        items = state.invs.get(name)
        if items is None:
            values.append(UNKNOWN)
            continue
        values.append(len(items) & mask)
        for item_id in sorted(items):
            values.append(item_id & mask)
            values.append(items[item_id] & mask)
    acc = w.acc | (_words(values) << w.n)
    return acc.to_bytes((w.n + 32 * len(values) + 7) // 8, "little")


def hash_canonical(layout: WireLayout, data: bytes) -> int:
    """The state hash of canonical bytes (so a caller that needs both packs the state once)."""
    return fnv1a32_bytes(data, FNV_OFFSET ^ layout.hash)


def state_hash(layout: WireLayout, state: WireState) -> int:
    return hash_canonical(layout, canonical(layout, state))


def fnv1a32_bytes(data: bytes, seed: int = FNV_OFFSET) -> int:
    h = seed
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def unpack_full(layout: WireLayout, body: bytes) -> WireState:
    """The inverse of `canonical`. Raises ValueError for a body that ends before its own counts say it should
    (it used to read zeros past the end, and a count of billions would have looped that long)."""
    bits = layout.total_bits
    r = _Reader(body[:(bits + 7) // 8])  # the slots only: every read shifts the whole number it reads from
    slots = [r.read(s.width) for s in layout.slots]
    # The rest is 32-bit words starting at a bit offset: shift once, then read them as an array.
    count = max(0, (len(body) * 8 - bits) // 32)
    words = array("I")
    words.frombytes((int.from_bytes(body[bits // 8:], "little") >> (bits % 8)).to_bytes(count * 4 + 4, "little")[:count * 4])
    if sys.byteorder == "big":
        words.byteswap()
    invs: dict[str, dict[int, int]] = {}
    at = 0
    for name in layout.inventories:
        if at >= count:
            raise ValueError("the body ends before its inventories do")
        n = words[at]
        at += 1
        if n == UNKNOWN:
            continue
        end = at + 2 * n
        if end > count:
            raise ValueError("the body ends inside an inventory")
        invs[name] = dict(zip(words[at:end:2], words[at + 1:end:2]))
        at = end
    return WireState(slots, invs)


def apply_delta(layout: WireLayout, state: WireState, body: bytes) -> tuple[int, int]:
    r = _Reader(body)
    n_slots = r.read(16)
    for _ in range(n_slots):
        i = r.read(16)
        state.slots[i] = r.read(layout.slots[i].width)
    n_items = r.read(16)
    for _ in range(n_items):
        inv = layout.inventories[r.read(8)]
        item_id = r.read(32)
        count = r.read(32)
        items = state.invs.setdefault(inv, {})
        if item_id == UNKNOWN:  # "now known": the (possibly empty) set exists from here on
            continue
        if count:
            items[item_id] = count
        else:
            items.pop(item_id, None)
    return n_slots, n_items


def base_value(entry: BaseEntry, slot: int) -> int:
    """A base entry's slot value back to (the known bits of) the base var's 32-bit value."""
    u = 0
    for r in entry.ranges:
        v = slot & ((1 << r.size) - 1)
        slot >>= r.size
        u |= v << r.start
    for b in entry.bits:
        u |= (slot & 1) << b
        slot >>= 1
    return u


def values(layout: WireLayout, state: WireState) -> tuple[dict[str, int], dict[str, int], dict[str, int], bool]:
    """(field values for game_configs.derive, named var values, skill xp by SkillType name, members). Named values
    are as sent: unsigned, each within its width."""
    bases = {}
    named, skills, members = {}, {}, False
    for slot, value in zip(layout.slots, state.slots):
        if slot.kind == "base":
            bases[slot.name] = base_value(slot.entry, value)
        elif slot.kind == "named":
            named[slot.name] = value
        elif slot.kind == "skill":
            skills[slot.name] = value
        else:
            members = bool(value)
    return layout.manifest.fields_from_bases(bases), named, skills, members


@dataclass
class Post:
    number: int
    kind: str
    bytes: int
    accepted_by_plugin: bool
    verdict: str  # "accepted" | "rejected: <why>"


@dataclass
class Replay:
    layout: WireLayout | None = None
    state: WireState | None = None
    # What the server keeps across posts: inventories a later full post marked unknown stay as last seen.
    known_invs: dict[str, dict[int, int]] = field(default_factory=dict)
    posts: list[Post] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.errors and all((p.verdict == "accepted") == p.accepted_by_plugin for p in self.posts)


def replay(entries: dict[str, Any], configs: GameConfigs, player: str) -> Replay:
    """Play the recorded posts for one player in order, as the server would."""
    out = Replay()
    posts = []
    for key, value in entries.items():
        parts = key.split(":")
        if len(parts) == 4 and parts[0] == "wire" and parts[1] == player and parts[2] == "post" and isinstance(value, dict):
            posts.append((int(parts[3]), value))
    server_hash = None
    for number, header in sorted(posts):
        kind = header.get("kind")
        body = base64.b64decode(header.get("body") or "")
        plugin_accepted = bool(header.get("accepted"))
        verdict = "accepted"
        try:
            if kind == "full":
                if "named" in header:  # plugins up to 0.4.3: the lists in every full post
                    layout = WireLayout.build_named(configs, header.get("named") or [], header.get("skills") or [],
                                                    header.get("inventories") or [])
                else:
                    fields = load_manifest(header.get("manifest")) if isinstance(header.get("manifest"), int) else None
                    layout = (WireLayout.build(configs, [(n, w) for n, w, _ in fields], header.get("skills") or [],
                                               header.get("inventories") or []) if fields else None)
                if layout is None:
                    verdict = "rejected: manifest unknown"
                elif layout.hash != header.get("layout"):
                    verdict = f"rejected: layout hash {layout.hash:#010x} != plugin's {header.get('layout'):#010x}"
                else:
                    state = unpack_full(layout, body)
                    h = state_hash(layout, state)
                    if h != header.get("new"):
                        verdict = f"rejected: state hash {h:#010x} != plugin's {header.get('new'):#010x}"
                    else:
                        out.layout, out.state, server_hash = layout, state, h
                        out.known_invs.update({k: dict(v) for k, v in state.invs.items()})
            elif kind == "delta":
                if out.layout is None or out.state is None:
                    verdict = "rejected: no full state yet"
                elif header.get("base") != server_hash:
                    verdict = "rejected: base hash is not the server's current state"
                else:
                    trial = WireState(list(out.state.slots), {k: dict(v) for k, v in out.state.invs.items()})
                    apply_delta(out.layout, trial, body)
                    h = state_hash(out.layout, trial)
                    if h != header.get("new"):
                        verdict = f"rejected: state after the delta hashes to {h:#010x}, plugin said {header.get('new'):#010x}"
                    else:
                        out.state, server_hash = trial, h
                        out.known_invs.update({k: dict(v) for k, v in trial.invs.items()})
            else:
                verdict = f"rejected: unknown kind {kind!r}"
        except Exception as e:  # a malformed post is a finding, not a crash
            verdict = f"rejected: {type(e).__name__}: {e}"
        out.posts.append(Post(number, kind or "?", len(body), plugin_accepted, verdict))
        if (verdict == "accepted") != plugin_accepted:
            out.errors.append(f"post {number}: replay says {verdict}, the plugin's transport said "
                              f"{'accepted' if plugin_accepted else 'rejected'}")
    return out
