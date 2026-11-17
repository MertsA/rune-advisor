"""Vars-only export smoke test: compare the state derived from vars (sources/game_configs.derive) with the
state the plugin API reports, from the dev plugin's "Smoke vars" dump (smoke:vars, smoke:state).

Answers three questions before the exporter moves to posting vars: how big is the var payload, does the
derived quest/achievement state match the API's, and which watched vars read nil through the API.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .sources.game_configs import GameConfigs, _as_list, base_manifest, derive, in_grace, var_layout

PROGRESS_COMPLETE, PROGRESS_GRACE = -2, -3


@dataclass
class Report:
    var_count: int = 0
    null_count: int = 0
    bytes_by_name: int = 0
    bytes_compact: int = 0  # as [[id, value], ...]
    # Nil-reading vars that something depends on: name -> {"ids": ..., "quests": [...], "achievements": [...],
    # "open": n (achievements not complete in the API), "baseValue": ...}
    nulls_that_matter: dict[str, dict] = field(default_factory=dict)
    nulls_unused: list[str] = field(default_factory=list)
    quests_compared: int = 0
    quest_mismatches: list[tuple[str, str, str]] = field(default_factory=list)  # (quest, derived, api)
    tests_by_kind: Counter = field(default_factory=Counter)  # (kind, "agree"|"disagree"|"unknown") -> n
    test_mismatches: list[tuple[str, int, str, str, bool | None, bool]] = field(default_factory=list)
    achievements_compared: int = 0
    completion_mismatches: list[tuple[str, bool | None, str]] = field(default_factory=list)  # (name, derived, api)
    grace_explained: list[str] = field(default_factory=list)
    skill_level_kind: dict[str, int] = field(default_factory=dict)  # "level"/"uncapped" -> tests agreeing
    proxy_mismatches: list[dict] = field(default_factory=list)  # stored-proxy read != fresh lookup by name
    fresh_nil: int = 0  # fresh lookups that returned nil although the immediate read had a value
    lua: dict | None = None  # the sandbox's Lua version and packing functions (smoke:state.lua)
    # Base-var manifest check: fields rebuilt from the base vars' values vs the API's own per-field reads.
    base_vars_read: int = 0
    base_fields_agree: int = 0
    base_fields_disagree: list[tuple[str, str, int, int]] = field(default_factory=list)  # (field, base, from base, api)
    base_blob_bytes: int = 0

    def lines(self) -> list[str]:
        out = ([f"sandbox Lua: {self.lua}"] if self.lua else []) + [
               f"watched vars: {self.var_count}, nil through the API: {self.null_count} "
               f"({len(self.nulls_that_matter)} referenced by a quest or achievement test)",
               f"payload: {self.bytes_by_name / 1024:.0f} KB by name, {self.bytes_compact / 1024:.0f} KB as [id, value] pairs"]
        if self.proxy_mismatches or self.fresh_nil:
            out.append(f"stored-proxy reads disagreeing with a fresh lookup by name: {len(self.proxy_mismatches)}, "
                       f"fresh lookups nil where the immediate read had a value: {self.fresh_nil}")
            out += [f"  {m.get('name')} (id {m.get('id')}, bit {m.get('bit')}): proxy {m.get('immediate')}, fresh {m.get('fresh')}"
                    for m in self.proxy_mismatches[:10]]
        for name, info in sorted(self.nulls_that_matter.items()):
            refs = info["quests"] + info["achievements"]
            out.append(f"  nil: {name} (id {info['ids']}, base {info.get('base')}={info.get('baseValue')}, "
                       f"{info['open']} open achievement(s)) <- {', '.join(refs[:4])}{'...' if len(refs) > 4 else ''}")
        if self.base_vars_read:
            out.append(f"base-var manifest: {self.base_vars_read} base vars read, blob {self.base_blob_bytes} bytes; fields rebuilt from "
                       f"base values agree with the API's field reads: {self.base_fields_agree}, disagree: {len(self.base_fields_disagree)}")
            out += [f"  {name} in {base}: from base {a}, API {b}" for name, base, a, b in self.base_fields_disagree[:15]]
        out.append(f"quests: {self.quests_compared} compared, {len(self.quest_mismatches)} mismatches")
        out += [f"  {q}: derived {d}, API {a}" for q, d, a in self.quest_mismatches[:15]]
        for kind in ("skill", "quest", "achievement", "varTotal", "varBit"):
            agree, disagree, unknown = (self.tests_by_kind[(kind, k)] for k in ("agree", "disagree", "unknown"))
            out.append(f"tests {kind}: {agree} agree, {disagree} disagree, {unknown} unknown")
        out += [f"  {name} test {i} ({kind}, {desc!r}): derived {d}, API {a}" for name, i, kind, desc, d, a in self.test_mismatches[:25]]
        if self.skill_level_kind:
            out.append("skill tests agree with " + ", ".join(f"{k}: {n}" for k, n in self.skill_level_kind.items()))
        out.append(f"achievements: {self.achievements_compared} compared, {len(self.completion_mismatches)} completion mismatches"
                   + (f" ({len(self.grace_explained)} explained by grace)" if self.grace_explained else ""))
        out += [f"  {name}: derived {d}, API {a}" for name, d, a in self.completion_mismatches[:25]]
        return out


def compare(configs: GameConfigs, vars_dump: dict[str, Any], state: dict[str, Any], today: date | None = None) -> Report:
    today = today or date.today()
    r = Report()
    values: dict[str, Any] = dict(vars_dump.get("values") or {})
    nulls = _as_list(vars_dump.get("nulls"))
    r.var_count = int(vars_dump.get("count") or (len(values) + len(nulls)))
    r.null_count = len(nulls)
    r.proxy_mismatches = _as_list(vars_dump.get("proxyMismatches"))
    r.fresh_nil = int(vars_dump.get("freshNil") or 0)
    r.lua = state.get("lua") if isinstance(state.get("lua"), dict) else None
    base_values = vars_dump.get("baseValues")
    if isinstance(base_values, dict) and base_values:
        layout = var_layout(configs)
        manifest = base_manifest(configs, layout)
        blob = manifest.pack(base_values)
        rebuilt = manifest.unpack(blob)
        r.base_vars_read, r.base_blob_bytes = len(base_values), len(blob)
        expected = layout.unpack(layout.pack(values))  # the API's field reads, saturated the same way
        for f in layout.fields:
            if f.name not in values or f.base not in base_values:
                continue
            if rebuilt.get(f.name) == expected.get(f.name):
                r.base_fields_agree += 1
            else:
                r.base_fields_disagree.append((f.name, f.base, rebuilt.get(f.name), expected.get(f.name)))
    r.bytes_by_name = len(json.dumps(values, separators=(",", ":")))
    ids = {}
    for q in configs.quests.values():
        if isinstance(q.get("progressVar"), dict):
            ids[q["progressVar"].get("name")] = q["progressVar"].get("id")
    for a in configs.achievements.values():
        for t in _as_list(a.get("requirements")) + _as_list(a.get("prereqs")):
            for v in (t.get("vars") or ([t["var"]] if t.get("var") else [])):
                ids[v.get("name")] = v.get("id")
    r.bytes_compact = len(json.dumps([[ids.get(n, 0), v] for n, v in values.items()], separators=(",", ":")))

    api_state = {a["id"]: a for a in _as_list(state.get("achievements"))}
    api_quests = {q["id"]: q for q in _as_list(state.get("quests"))}
    # Who needs each nil var (requirements only: prerequisites gate visibility, not completion).
    for n in nulls:
        name = n.get("name")
        quests = [q.get("name", "") for q in configs.quests.values()
                  if isinstance(q.get("progressVar"), dict) and q["progressVar"].get("name") == name]
        achievements = [a.get("name", "") for a in configs.achievements.values()
                        if any(name in [v.get("name") for v in (t.get("vars") or ([t["var"]] if t.get("var") else []))]
                               for t in _as_list(a.get("requirements")))]
        if quests or achievements:
            open_ = sum(1 for a in configs.achievements.values() if a.get("name") in achievements
                        and (api_state.get(a["id"], {}).get("progress") != PROGRESS_COMPLETE))
            r.nulls_that_matter[name] = {"ids": n.get("id"), "base": n.get("base"), "baseValue": n.get("baseValue"),
                                         "quests": quests, "achievements": achievements, "open": open_}
        else:
            r.nulls_unused.append(name)

    skills_state = state.get("skills") or {}
    for level_kind in ("uncapped", "level"):
        skills = {name: s.get(level_kind) for name, s in skills_state.items() if isinstance(s, dict) and s.get(level_kind) is not None}
        d = derive(configs, values, skills, today)
        agree = 0
        for aid, results in d.tests.items():
            api_tests = api_state.get(aid, {}).get("tests")
            cfg_tests = _as_list(configs.achievements[aid].get("requirements"))
            if not api_tests or len(api_tests) != len(results):
                continue
            for t, res, api in zip(cfg_tests, results, api_tests):
                if t.get("kind") == "skill" and res is not None and res == bool(api):
                    agree += 1
        r.skill_level_kind[level_kind] = agree
    best = max(r.skill_level_kind, key=r.skill_level_kind.get) if r.skill_level_kind else "uncapped"
    skills = {name: s.get(best) for name, s in skills_state.items() if isinstance(s, dict) and s.get(best) is not None}
    d = derive(configs, values, skills, today)

    for qid, q in api_quests.items():
        cfg = configs.quests.get(qid)
        derived = d.quests.get(qid)
        if cfg is None or derived is None:
            continue
        api = "finished" if q.get("finished") else "started" if q.get("started") else "not started"
        r.quests_compared += 1
        if derived != api:
            r.quest_mismatches.append((cfg.get("name", str(qid)), derived, api))

    for aid, results in d.tests.items():
        cfg = configs.achievements[aid]
        api = api_state.get(aid)
        if api is None:
            continue
        api_tests = api.get("tests")
        cfg_tests = _as_list(cfg.get("requirements"))
        if api_tests and len(api_tests) == len(results):
            for i, (t, res, flag) in enumerate(zip(cfg_tests, results, api_tests)):
                kind = t.get("kind", "?")
                if res is None:
                    r.tests_by_kind[(kind, "unknown")] += 1
                elif res == bool(flag):
                    r.tests_by_kind[(kind, "agree")] += 1
                else:
                    r.tests_by_kind[(kind, "disagree")] += 1
                    r.test_mismatches.append((cfg.get("name", ""), i, kind, t.get("description") or "", res, bool(flag)))
        derived = d.complete.get(aid)
        if derived is None or not cfg_tests:
            continue
        r.achievements_compared += 1
        progress = api.get("progress")
        api_complete = progress == PROGRESS_COMPLETE
        if derived != api_complete:
            label = "COMPLETE" if api_complete else "GRACE" if progress == PROGRESS_GRACE else str(progress)
            if progress == PROGRESS_GRACE and not derived and in_grace(cfg, today):
                r.grace_explained.append(cfg.get("name", ""))
            else:
                r.completion_mismatches.append((cfg.get("name", ""), derived, label))
    return r
