"""Static game configs: every quest and achievement as the client sees them, dumped once with the dev plugin's
"Dump configs" button (plugins/rune_advisor_dev/lib/configdump.lua) and kept here as data
(`data/game_configs/{quests,achievements}.json.gz`, written by `scripts/import_game_configs.py`).

The published exporter sends only *state* per config id (schema 2 snapshots: started/finished, progress, which
requirement tests pass); `expand_snapshot` joins that with these configs into the shape the planner reads
(names, quest points, requirement tests with descriptions and totals). A snapshot that names a config id the
dump doesn't know (a quest released after the last dump) is reported in the player's notes: re-dump on the beta.

Quest entries: id, configName, name, questPoints, members, type, difficulty, deprecated, progressVar
{name, id, bit?, base?, mask?}, startState, endState, preReqQuestPoints, preReqQuests [names],
preReqSkills [{skill, level}], startCoords [[level, x, z]], viaCoord, params {PARAM_NAME: value}.
Achievement entries: id, configName, name, runeScore, members, category, subCategory (game ids), reward,
hiddenType, graceExpiryRunedate, tallyTarget, descriptions [{type, text}], prereqs/requirements (tests: kind,
group, description, complete-at-dump, skills/totalLevels, quest, achievement, vars [{name, id, ...}]/totalValue,
var/bitIndex/bitValue), requiredGroups, requiredSubItems [per group: tests needed], prereqGroupsNeeded,
prereqSubItems.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "game_configs"
FILES = {"quests": "quests.json.gz", "achievements": "achievements.json.gz"}
DUMP_KEYS = {"quests": "config:quests", "achievements": "config:achievements"}

PROGRESS_COMPLETE = -2
PROGRESS_DEPRECATED = -4


def _as_list(value: Any) -> list:
    """The exporter's JSON encoder writes an empty Lua table as {}."""
    return value if isinstance(value, list) else []


# --- Canonical hashes (must match plugins/rune_advisor_export/lib/canon.lua byte for byte) ----------------------
#
# nil -> n, booleans -> t/f, integers and whole floats -> decimal, other floats -> %.14g, strings quoted with
# only backslash and quote escaped, empty containers -> {}, lists -> [a,b], dicts -> {"k":v,...} keys sorted.
# State keys (progress, progressType, complete) are skipped at every depth.

STATE_KEYS = frozenset({"progress", "progressType", "complete"})


def canonical(value: Any) -> str:
    if value is None:
        return "n"
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "n"
        if value.is_integer():
            return str(int(value))
        return "%.14g" % value
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        if not value:
            return "{}"
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(str(k) for k in value if str(k) not in STATE_KEYS)
        if not value:
            return "{}"
        return "{" + ",".join(canonical(k) + ":" + canonical(value[k] if k in value else value[int(k)]) for k in keys) + "}"
    return "n"


def strip_state(value: Any) -> Any:
    """A copy of a config entry without STATE_KEYS at any depth."""
    if isinstance(value, dict):
        return {k: strip_state(v) for k, v in value.items() if str(k) not in STATE_KEYS}
    if isinstance(value, list):
        return [strip_state(v) for v in value]
    return value


def config_hash(entry: dict) -> int:
    """One config entry's hash (the same number lib/canon.lua computes in the client)."""
    return fnv1a32(canonical(entry))


def fingerprint(hashes: dict[str, dict[int, int]]) -> int:
    """The hash of a whole config set: {"quests": {id: hash}, "achievements": {id: hash}}."""
    h = FNV_OFFSET
    for kind in ("quests", "achievements"):
        for cid in sorted(int(i) for i in hashes.get(kind, {})):
            value = hashes[kind].get(cid, hashes[kind].get(str(cid)))
            h = _fnv_text(f"{kind[0]}{cid}:{value};", h)
    return h


def _fnv_text(text: str, seed: int) -> int:
    h = seed
    for b in text.encode("utf-8"):
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFF
    return h


@dataclass
class GameConfigs:
    quests: dict[int, dict] = field(default_factory=dict)
    achievements: dict[int, dict] = field(default_factory=dict)

    @classmethod
    def from_dumps(cls, dumps: dict[str, Any]) -> "GameConfigs":
        """From the dev plugin's rows (`plugin_db.dumps`) or the archive's config_*.json contents."""
        return cls(quests={q["id"]: q for q in _as_list(dumps.get(DUMP_KEYS["quests"]))},
                   achievements={a["id"]: a for a in _as_list(dumps.get(DUMP_KEYS["achievements"]))})

    @classmethod
    def read(cls, directory: Path = DATA_DIR) -> "GameConfigs | None":
        paths = {kind: directory / name for kind, name in FILES.items()}
        if not all(p.exists() for p in paths.values()):
            return None
        return cls.from_dumps({DUMP_KEYS[kind]: json.loads(gzip.open(p, "rt", encoding="utf-8").read())
                               for kind, p in paths.items()})

    def write(self, directory: Path = DATA_DIR) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for kind, name in FILES.items():
            entries = list(getattr(self, kind).values())
            with gzip.open(directory / name, "wt", encoding="utf-8") as f:
                json.dump(entries, f, separators=(",", ":"), ensure_ascii=False)

    def hashes(self) -> dict[str, dict[int, int]]:
        return {"quests": {i: config_hash(q) for i, q in self.quests.items()},
                "achievements": {i: config_hash(a) for i, a in self.achievements.items()}}

    @property
    def fingerprint(self) -> int:
        if not hasattr(self, "_fingerprint"):
            self._fingerprint = fingerprint(self.hashes())
        return self._fingerprint

    def stripped(self) -> "GameConfigs":
        """The same set without the per-player state keys (progress, complete...), which the fingerprint ignores
        too: what may be shared once a set is confirmed, and what `content_hash` covers."""
        return GameConfigs(quests={i: strip_state(q) for i, q in self.quests.items()},
                           achievements={i: strip_state(a) for i, a in self.achievements.items()})

    def content_hash(self) -> str:
        """SHA-256 of the whole set (state keys left out), hex. Unlike the 32-bit fingerprint it can't be
        collided on purpose, so equal hashes from different accounts mean byte-identical content."""
        s = self.stripped()
        text = json.dumps({"quests": [s.quests[i] for i in sorted(s.quests)],
                           "achievements": [s.achievements[i] for i in sorted(s.achievements)]},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def diff_counts(self, base: "GameConfigs") -> dict[str, int]:
        """How many quests/achievements this set adds or changes (`*_added`) and drops (`*_removed`) vs `base`."""
        mine, theirs = self.hashes(), base.hashes()
        out: dict[str, int] = {}
        for kind, short in (("quests", "q"), ("achievements", "a")):
            out[f"{short}_added"] = sum(1 for i, h in mine[kind].items() if theirs[kind].get(i) != h)
            out[f"{short}_removed"] = sum(1 for i in theirs[kind] if i not in mine[kind])
        return out

    def compose(self, added: dict[str, list[dict]], removed: dict[str, list[int]]) -> "GameConfigs":
        """A new set: this one minus `removed` ids plus `added` entries (replacing by id)."""
        out = GameConfigs(quests=dict(self.quests), achievements=dict(self.achievements))
        for kind in ("quests", "achievements"):
            table = getattr(out, kind)
            for cid in removed.get(kind, []) or []:
                table.pop(int(cid), None)
            for entry in added.get(kind, []) or []:
                if isinstance(entry, dict) and isinstance(entry.get("id"), int):
                    table[entry["id"]] = entry
        return out

    def quest_by_name(self, name: str) -> dict | None:
        return next((q for q in self.quests.values() if q.get("name") == name and not q.get("deprecated")), None)

    def achievement_by_name(self, name: str) -> dict | None:
        return next((a for a in self.achievements.values() if a.get("name") == name), None)

    def quest_var_owners(self) -> dict[str, str]:
        """Quest progress var name -> quest name, for tying achievements that test it to the quest."""
        return {q["progressVar"]["name"]: q["name"] for q in self.quests.values()
                if isinstance(q.get("progressVar"), dict) and q["progressVar"].get("name")}


@lru_cache(maxsize=1)
def load() -> GameConfigs | None:
    """The planner's configs: the active data package's, else the bundled ones (the export API reads `read()`)."""
    from .. import datapkg
    return GameConfigs.read(datapkg.path("game_configs"))


def expand_snapshot(snap: dict[str, Any], configs: GameConfigs) -> tuple[dict[str, Any], list[str]]:
    """A schema-2 snapshot (state by config id) joined with the configs into the schema-1 shape:
    quests with names and types, achievements with names, RuneScore and requirement tests carrying the
    snapshot's pass/fail and var totals. Returns the expanded snapshot and notes about ids the configs lack."""
    notes: list[str] = []
    unknown_quests, unknown_achievements, stale = [], [], []
    quests = []
    for q in _as_list(snap.get("quests")):
        cfg = configs.quests.get(q.get("id"))
        if cfg is None:
            unknown_quests.append(q.get("id"))
            continue
        progress = None
        if isinstance(q.get("progress"), (int, float)) and isinstance(cfg.get("progressVar"), dict):
            progress = {"var": cfg["progressVar"].get("name"), "value": q["progress"],
                        "start": cfg.get("startState"), "finish": cfg.get("endState")}
        quests.append({"id": cfg["id"], "configName": cfg.get("configName"), "name": cfg.get("name"),
                       "questPoints": cfg.get("questPoints", 0), "members": cfg.get("members"),
                       "type": cfg.get("type"), "difficulty": cfg.get("difficulty"),
                       "started": q.get("started"), "finished": q.get("finished"), "canStart": q.get("canStart"),
                       "progress": progress})
    achievements = []
    for a in _as_list(snap.get("achievements")):
        cfg = configs.achievements.get(a.get("id"))
        if cfg is None:
            unknown_achievements.append(a.get("id"))
            continue
        progress = a.get("progress", 0)
        if progress == PROGRESS_DEPRECATED:
            continue
        completed = progress == PROGRESS_COMPLETE
        entry = {"id": cfg["id"], "configName": cfg.get("configName"), "name": cfg.get("name"),
                 "runeScore": cfg.get("runeScore", 0), "progress": progress, "completed": completed,
                 "locked": progress == -1, "grace": progress == -3}
        if not completed:
            tests = [dict(t) for t in _as_list(cfg.get("requirements"))]
            states = _as_list(a.get("tests"))
            if states and len(states) != len(tests):
                stale.append(cfg.get("name"))
                states = []
            for t, state in zip(tests, states):
                t["complete"] = bool(state[0]) if state else bool(t.get("complete"))
                if t.get("kind") == "varTotal" and len(state) > 1:
                    t["currentValue"] = state[1]
            if not states:
                for t in tests:
                    t["complete"] = bool(t.get("complete"))  # as of the dump
            entry["requirements"] = tests
            entry["requiredGroups"] = cfg.get("requiredGroups")
            entry["groupNeeds"] = {g: n for g, n in enumerate(_as_list(cfg.get("requiredSubItems")))
                                   if isinstance(n, int)}
        achievements.append(entry)
    if unknown_quests:
        notes.append(f"{len(unknown_quests)} quest id(s) missing from the game configs dump (re-dump on the beta): "
                     + ", ".join(str(i) for i in unknown_quests[:8]))
    if unknown_achievements:
        notes.append(f"{len(unknown_achievements)} achievement id(s) missing from the game configs dump (re-dump): "
                     + ", ".join(str(i) for i in unknown_achievements[:8]))
    if stale:
        notes.append("requirement tests changed since the configs dump (pass/fail taken from the dump) for: "
                     + ", ".join(stale[:8]))
    out = dict(snap)
    out["quests"], out["achievements"] = quests, achievements
    return out, notes


# --- Deriving state from vars alone (what a vars-only export, or an HTTP receiver, would do) ---------------

RUNEDATE_EPOCH = date(2002, 2, 27)  # RuneDate: days since RuneScape's release


def quest_state_from_var(cfg: dict, value: int | None) -> str | None:
    """"not started" / "started" / "finished" from a quest's master var, or None without a var."""
    if value is None or not isinstance(cfg.get("progressVar"), dict):
        return None
    if value >= (cfg.get("endState") or 0):
        return "finished"
    if value >= (cfg.get("startState") or 0):
        return "started"
    return "not started"


@dataclass
class Derived:
    """State derived from var values plus skills, with every achievement's tests resolved."""
    quests: dict[int, str | None] = field(default_factory=dict)           # quest id -> state
    tests: dict[int, list[bool | None]] = field(default_factory=dict)      # achievement id -> per test (None: unknown)
    complete: dict[int, bool | None] = field(default_factory=dict)         # achievement id -> all required groups met
    missing_vars: dict[str, set[str]] = field(default_factory=dict)        # var name -> achievement/quest names needing it


def derive(configs: GameConfigs, values: dict[str, Any], skills: dict[str, int], today: date | None = None) -> Derived:
    """Resolve every quest's and achievement's state from var values (by name), skill levels (SkillType names)
    and the configs. Achievement tests referencing other achievements use their derived completion."""
    d = Derived()
    today = today or date.today()
    quests_by_name: dict[str, dict] = {}
    for q in configs.quests.values():
        if q.get("progressVar"):
            name = q["progressVar"].get("name")
            d.quests[q["id"]] = quest_state_from_var(q, values.get(name))
            if name not in values:
                d.missing_vars.setdefault(name, set()).add(q.get("name", ""))
        if not q.get("deprecated"):
            quests_by_name[q.get("name", "")] = q
    by_name = {a.get("name", ""): a for a in configs.achievements.values()}

    def test_passes(a: dict, t: dict, seen: frozenset) -> bool | None:
        kind = t.get("kind")
        if kind == "skill":
            levels = [skills.get(s) for s in t.get("skills") or []]
            if any(v is None for v in levels):
                return None
            return sum(levels) >= (t.get("totalLevels") or 0)
        if kind == "quest":
            q = configs.quests.get(t.get("questId")) if t.get("questId") is not None else quests_by_name.get(t.get("quest") or "")
            state = d.quests.get(q["id"]) if q else None
            return None if state is None else state == "finished"
        if kind == "achievement":
            # By id when the dump has it (names collide: two "Relight My Fire"); a sub-achievement in its
            # grace period counts as complete for the achievement testing it, as the API does.
            sub = (configs.achievements.get(t.get("achievementId")) if t.get("achievementId") is not None
                   else by_name.get(t.get("achievement") or ""))
            if sub is None:
                return None
            if today is not None and in_grace(sub, today):
                return True
            return resolve(sub, seen)
        if kind == "varTotal":
            total = 0
            for v in t.get("vars") or []:
                if v.get("name") not in values:
                    d.missing_vars.setdefault(v.get("name", "?"), set()).add(a.get("name", ""))
                    return None
                total += values[v["name"]] or 0
            return total >= (t.get("totalValue") or 0)
        if kind == "varBit":
            v = t.get("var") or {}
            if v.get("name") not in values:
                d.missing_vars.setdefault(v.get("name", "?"), set()).add(a.get("name", ""))
                return None
            value = values[v["name"]] or 0
            return ((int(value) >> int(t.get("bitIndex") or 0)) & 1) == (t.get("bitValue") if t.get("bitValue") is not None else 1)
        return None

    def resolve(a: dict, seen: frozenset = frozenset()) -> bool | None:
        aid = a["id"]
        if aid in d.complete:
            return d.complete[aid]
        if aid in seen:
            return None
        seen = seen | {aid}
        tests = _as_list(a.get("requirements"))
        results = [test_passes(a, t, seen) for t in tests]
        d.tests[aid] = results
        if not tests:
            d.complete[aid] = None
            return None
        needs = _as_list(a.get("requiredSubItems"))
        groups: dict[int, list[bool | None]] = {}
        for t, r in zip(tests, results):
            groups.setdefault(t.get("group", 0), []).append(r)
        met: list[bool | None] = []
        for g, flags in groups.items():
            need = needs[g] if g < len(needs) and isinstance(needs[g], int) else len(flags)
            passed = sum(1 for f in flags if f)
            if passed >= need:
                met.append(True)
            elif any(f is None for f in flags) and passed + sum(1 for f in flags if f is None) >= need:
                met.append(None)
            else:
                met.append(False)
        required = a.get("requiredGroups") or len(groups)
        if sum(1 for m in met if m) >= required:
            d.complete[aid] = True
        elif sum(1 for m in met if m is not False) >= required:
            d.complete[aid] = None
        else:
            d.complete[aid] = False
        return d.complete[aid]

    for a in configs.achievements.values():
        resolve(a)
    return d


def in_grace(cfg: dict, today: date) -> bool:
    expiry = cfg.get("graceExpiryRunedate")
    return isinstance(expiry, int) and expiry >= 0 and (today - RUNEDATE_EPOCH).days < expiry


# --- Packed var layout: only the bits any quest or achievement test can read ----------------------------
#
# Each watched var contributes a field to one blob, laid out in var-id order (deterministic from the configs):
#   "count"  a quest progress var or var-total operand: `width` bits holding min(max(value, 0), 2^width - 1),
#            where width fits the largest end state / total value any test needs (saturation loses nothing a
#            test can observe: sum >= total still holds, and a quest past its end state is finished). A VarBit
#            operand of a multi-var total ("Hunter outfit": five 1-bit pieces summed against 5; port scroll
#            pieces: 3-bit counters summed against 16) is capped at its own field width;
#   "bits"   only tested through var-bit tests: the tested bit positions, in order;
#   "mixed"  both (3 vars in the 2026-10-31 configs, all step bitfields whose whole value is also compared
#            against a threshold: a miniquest's master var with end state = all steps, "any step done"
#            totals): the whole field, unsaturated (the VarBit's width, else 32 bits).
# Bits are written LSB-first from bit offset 0; the blob is little-endian bytes. A 32-bit FNV-1a hash of the
# layout description travels with the payload so a receiver can tell the sending plugin derived the same
# layout from the same configs (the plugin computes it in Lua the same way).

FNV_OFFSET, FNV_PRIME = 0x811C9DC5, 0x01000193


def fnv1a32(text: str) -> int:
    h = FNV_OFFSET
    for b in text.encode("utf-8"):
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFF
    return h


@dataclass
class VarField:
    name: str
    id: int
    mode: str              # "count" | "bits" | "mixed"
    width: int             # bits in the blob
    max_value: int = 0     # count: saturation ceiling (2^width - 1 >= largest need)
    bits: list[int] = field(default_factory=list)  # bits mode: tested bit positions, ascending
    base: str = ""         # the VarPlayer the server transmits (a VarBit's base var, else the var itself)


@dataclass
class VarLayout:
    fields: list[VarField]

    @property
    def total_bits(self) -> int:
        return sum(f.width for f in self.fields)

    def description(self) -> str:
        return ";".join(f"{f.id}:{f.mode}:{f.width}:{','.join(map(str, f.bits))}" for f in self.fields)

    @property
    def hash(self) -> int:
        return fnv1a32(self.description())

    def fields_by_base(self) -> dict[str, list[int]]:
        """Base var name -> indices of the fields it holds. VarTransmit reports the base var, so this is the
        lookup for "which fields may have changed": repack those, compare with what was last sent."""
        out: dict[str, list[int]] = {}
        for i, f in enumerate(self.fields):
            out.setdefault(f.base, []).append(i)
        return out

    def pack(self, values: dict[str, Any]) -> bytes:
        acc, nbits = 0, 0
        for f in self.fields:
            raw = values.get(f.name)
            raw = int(raw) if isinstance(raw, (int, float)) else 0
            if f.mode == "count":
                v = min(max(raw, 0), (1 << f.width) - 1)
            elif f.mode == "bits":
                u = raw & 0xFFFFFFFF
                v = 0
                for i, pos in enumerate(f.bits):
                    v |= ((u >> pos) & 1) << i
            else:
                v = raw & ((1 << f.width) - 1)
            acc |= v << nbits
            nbits += f.width
        return acc.to_bytes((nbits + 7) // 8, "little")

    def unpack(self, blob: bytes) -> dict[str, int]:
        acc = int.from_bytes(blob, "little")
        out, offset = {}, 0
        for f in self.fields:
            v = (acc >> offset) & ((1 << f.width) - 1)
            offset += f.width
            if f.mode == "bits":
                u = 0
                for i, pos in enumerate(f.bits):
                    u |= ((v >> i) & 1) << pos
                v = u
            elif f.mode == "mixed" and f.width == 32 and v & 0x80000000:
                v -= 1 << 32  # back to the signed value the API returns
            out[f.name] = v
        return out


def var_layout(configs: GameConfigs) -> VarLayout:
    """The packed layout implied by the configs: every var a quest or achievement test reads, with the bits
    those tests can observe."""
    need: dict[int, dict] = {}

    def entry(v: dict) -> dict:
        n = need.setdefault(v["id"], {"name": v.get("name"), "max": 0, "bits": set(), "kinds": set(), "field": None,
                                      "base": (v.get("base") if v.get("bit") and v.get("base") else v.get("name")) or ""})
        mask = v.get("mask")
        if v.get("bit") and isinstance(mask, int) and mask > 0:
            n["field"] = (mask >> ((mask & -mask).bit_length() - 1)).bit_length()  # the VarBit's own width
        return n

    for q in configs.quests.values():
        pv = q.get("progressVar")
        if isinstance(pv, dict) and pv.get("id") is not None:
            n = entry(pv)
            n["max"] = max(n["max"], q.get("endState") or 0)
            n["kinds"].add("count")
    for a in configs.achievements.values():
        for t in _as_list(a.get("requirements")) + _as_list(a.get("prereqs")):
            if t.get("kind") == "varTotal":
                for v in t.get("vars") or []:
                    if v.get("id") is None:
                        continue
                    n = entry(v)
                    n["max"] = max(n["max"], t.get("totalValue") or 0)
                    n["kinds"].add("count")
            elif t.get("kind") == "varBit" and (t.get("var") or {}).get("id") is not None:
                n = entry(t["var"])
                n["bits"].add(int(t.get("bitIndex") or 0))
                n["kinds"].add("bits")
    fields = []
    for vid in sorted(need):
        n = need[vid]
        if n["kinds"] == {"count"}:
            width = max(1, n["max"].bit_length())
            if n["field"]:
                width = min(width, n["field"])
            fields.append(VarField(n["name"], vid, "count", width, (1 << width) - 1, base=n["base"]))
        elif n["kinds"] == {"bits"}:
            bits = sorted(n["bits"])
            fields.append(VarField(n["name"], vid, "bits", len(bits), bits=bits, base=n["base"]))
        else:
            width = n["field"] or 32
            fields.append(VarField(n["name"], vid, "mixed", width, bits=sorted(n["bits"]), base=n["base"]))
    return VarLayout(fields)


# --- Base-var manifest: the same bits, organised the way the server transmits them ---------------------
#
# VarTransmit reports base vars, so the plugin works per base var: read (or take from the event) the 32-bit
# value, cut out the ranges tests can read, saturate the counters, pack the tested flag bits, and write
# them at that base var's slot in the blob. The manifest lists, per base var (name order):
#   ranges  [{start, stop, size}]  bits start..stop of the base var, emitted as `size` bits; size smaller
#           than the span means a counter saturated at 2^size - 1
#   bits    [positions]           single tested flag bits, packed in order after the ranges
# Merge rules (so no tested bit is ever lost to saturation): a range that overlaps another range or contains
# a tested bit is emitted at its full span, unsaturated; a range nested inside a full-span range is dropped;
# a tested bit inside a full-span range is dropped (the range carries it). The unpack side rebuilds every
# field of `var_layout` from the base slots, so `derive` runs on the result unchanged.


@dataclass
class BaseRange:
    start: int
    stop: int
    size: int

    @property
    def span(self) -> int:
        return self.stop - self.start + 1


@dataclass
class BaseEntry:
    base: str
    ranges: list[BaseRange] = field(default_factory=list)
    bits: list[int] = field(default_factory=list)

    @property
    def width(self) -> int:
        return sum(r.size for r in self.ranges) + len(self.bits)


def _field_span(f: VarField, v: dict) -> tuple[int, int]:
    """Bit range of a field within its base var: a VarBit's mask span, else the whole 32 bits."""
    mask = v.get("mask") if v.get("bit") else None
    if isinstance(mask, int) and mask > 0:
        start = (mask & -mask).bit_length() - 1
        return start, start + (mask >> start).bit_length() - 1
    return 0, 31


@dataclass
class BaseManifest:
    entries: list[BaseEntry]
    layout: VarLayout
    placement: dict[str, tuple[str, str, int, int]]  # field name -> (base, "range"|"bit", start, stop)

    @property
    def total_bits(self) -> int:
        return sum(e.width for e in self.entries)

    def description(self) -> str:
        return ";".join(f"{e.base}:" + ",".join(f"{r.start}-{r.stop}/{r.size}" for r in e.ranges) + ":"
                        + ",".join(map(str, e.bits)) for e in self.entries)

    @property
    def hash(self) -> int:
        return fnv1a32(self.description())

    def pack(self, base_values: dict[str, Any]) -> bytes:
        acc, nbits = 0, 0
        for e in self.entries:
            raw = base_values.get(e.base)
            u = (int(raw) if isinstance(raw, (int, float)) else 0) & 0xFFFFFFFF
            for r in e.ranges:
                v = (u >> r.start) & ((1 << r.span) - 1)
                if r.size < r.span:
                    v = min(v, (1 << r.size) - 1)
                acc |= v << nbits
                nbits += r.size
            for b in e.bits:
                acc |= ((u >> b) & 1) << nbits
                nbits += 1
        return acc.to_bytes((nbits + 7) // 8, "little")

    def unpack(self, blob: bytes) -> dict[str, int]:
        """Field values (by field name, as `var_layout` fields see them) rebuilt from the base slots."""
        acc = int.from_bytes(blob, "little")
        slots: dict[str, int] = {}  # base -> reconstructed base-var bits (saturated ranges widened in place)
        offset = 0
        for e in self.entries:
            u = 0
            for r in e.ranges:
                v = (acc >> offset) & ((1 << r.size) - 1)
                offset += r.size
                u |= v << r.start
            for b in e.bits:
                u |= ((acc >> offset) & 1) << b
                offset += 1
            slots[e.base] = u
        return self.fields_from_bases(slots)

    def fields_from_bases(self, bases: dict[str, int]) -> dict[str, int]:
        """Field values from (the known bits of) each base var's 32-bit value."""
        out = {}
        for f in self.layout.fields:
            base, kind, start, stop = self.placement[f.name]
            u = bases.get(base, 0)
            v = (u >> start) & ((1 << (stop - start + 1)) - 1)
            if f.mode == "count":
                v = min(v, (1 << f.width) - 1)
            elif f.mode == "bits":
                v &= sum(1 << b for b in f.bits)  # only the tested bits, like VarLayout.unpack
            elif f.mode == "mixed" and f.width == 32 and v & 0x80000000:
                v -= 1 << 32
            out[f.name] = v
        return out


def base_manifest(configs: GameConfigs, layout: VarLayout | None = None) -> BaseManifest:
    layout = layout or var_layout(configs)
    info: dict[str, dict] = {}
    for a in configs.achievements.values():
        for t in _as_list(a.get("requirements")) + _as_list(a.get("prereqs")):
            for v in (t.get("vars") or ([t["var"]] if t.get("var") else [])):
                info[v["name"]] = v
    for q in configs.quests.values():
        pv = q.get("progressVar")
        if isinstance(pv, dict):
            info[pv["name"]] = pv
    per_base: dict[str, dict] = {}
    placement: dict[str, tuple[str, str, int, int]] = {}
    for f in layout.fields:
        start, stop = _field_span(f, info[f.name])
        pb = per_base.setdefault(f.base, {"ranges": [], "bits": set()})
        if f.mode == "bits":
            pb["bits"] |= {start + b for b in f.bits}
        else:
            size = f.width if f.mode == "count" else stop - start + 1
            pb["ranges"].append(BaseRange(start, stop, min(size, stop - start + 1)))
        placement[f.name] = (f.base, "range", start, stop)
    entries = []
    for base in sorted(per_base):
        ranges, bits = per_base[base]["ranges"], per_base[base]["bits"]
        ranges.sort(key=lambda r: (r.start, -r.span))
        # Overlaps and contained tested bits: emit at full span (no saturation) so every bit survives.
        for i, r in enumerate(ranges):
            clash = any(b for b in bits if r.start <= b <= r.stop) or any(
                o is not r and o.start <= r.stop and r.start <= o.stop for o in ranges)
            if clash:
                r.size = r.span
        kept: list[BaseRange] = []
        for r in ranges:
            if any(k.size == k.span and k.start <= r.start and r.stop <= k.stop and (k.start, k.stop) != (r.start, r.stop) for k in kept):
                continue  # nested in a full-span range already kept
            if any((k.start, k.stop) == (r.start, r.stop) for k in kept):
                continue  # the same span twice (two fields over identical bits)
            kept.append(r)
        bit_list = sorted(b for b in bits if not any(k.size == k.span and k.start <= b <= k.stop for k in kept))
        entries.append(BaseEntry(base, kept, bit_list))
        for name, (fb, kind, start, stop) in placement.items():
            if fb == base and kind == "range" and not any(k.start <= start and stop <= k.stop for k in kept):
                placement[name] = (fb, "bit", start, stop)  # a pure bitfield: rebuilt from the bits list
    return BaseManifest(entries, layout, placement)
