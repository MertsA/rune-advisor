# Rune Advisor

A planning helper for RuneScape 3. It reads your character's data and suggests what to do next (quests,
achievements, levels, gear, collection logs) with an estimate of the hours each goal costs for *your*
account, ironman and Group Ironman accounts included. Site: <https://runeadvisor.com>.

**Status.** Not released. The in-game plugin is awaiting Jagex's moderation. The planner runs as a
command-line tool; there is no installer yet. The planner's data tables live in a separate repository that is
not public yet (see "Data" below), so a fresh clone of this repository cannot run the planner today.

## What is here
- `src/rune_advisor/`: the planner (`radv`), a desktop window for it (`rune_advisor.app`, `docs/app.md`), and
  the export API (`rune_advisor.export_api`, `docs/export-api.md`), the small service behind
  `api.runeadvisor.com` that passes a character's state from the plugin to the planner.
- `plugins/rune_advisor_export/`: the in-game plugin, for the official RuneScape plugin API (Lua). It posts
  your own character's state to the export API once you give it a token; its `README.md` says exactly what
  is sent. `plugins/tests/` is a mock of the plugin API for its tests.
- `site/`: the public site as plain files; the privacy policy and terms are written in `docs/policy/`.
- `scripts/`: the scripts that build the data tables from the RuneScape Wiki, and checks of the planner's
  simulations against the wiki's calculators.

## Where a character's data comes from
1. **Hiscores**: skills, experience, RuneScore (any player).
2. **Adventurer's Log** (RuneScape Platform API): quests and the game's quest point total (any player), and
   the recent events feed (members).
3. **The in-game plugin**, through the export API: quest and achievement progress, bank and other storage,
   currencies. Optional; without it the planner works from the public sources with less detail.
4. **`overrides.toml`**: manual corrections and settings the game does not expose (`overrides.example.toml`).

## Running it
```
git clone --recurse-submodules <this repository>
uv sync
uv run radv guide YourName             # what to do next, per track
uv run radv next YourName --hours 2    # a session packed from the guide
uv run radv focus YourName "Thieving:99"
uv run radv profile YourName
uv run pytest
```
The plugin's tests need `lua5.4`: `lua5.4 plugins/rune_advisor_export/tests/run_wire.lua`.
`scripts/link-dev-plugins.sh` links the plugin into the plugin beta client for development.

## Data
The tables the planner reads (drop tables, shops, recipes, equipment, rates and so on, most of them built
from the RuneScape Wiki) are a separate work under a different licence, kept in their own repository and
included here as a git submodule at `src/rune_advisor/data/`. They also ship to installed planners as signed
data packages, so a data fix needs no new release of the program (`docs/data-package.md`).

## Acknowledgements
Rune Advisor stands on the work of the RuneScape community, above all the editors of the
[RuneScape Wiki](https://runescape.wiki/). This is thanks, beyond what any licence requires:

- **The game data.** Drop tables, shops, recipes, equipment, quest and achievement requirements, boosts and much
  else are built from the wiki's pages and data tables.
- **How the game works.** The planner's simulations implement mechanics that wiki editors worked out, measured
  and documented, and they are checked against the wiki's own calculators: the perk calculator, the Mining rate
  and Woodcutting chance calculators, the Smithing rate, Fishing chance, Thieving chance, Farming yield and
  Archaeology hotspot calculators, and the Necromancy ritual calculator. The code here is our own; the
  understanding behind it is theirs.
- **How to play well.** The training guides, money making guides and ironman guides are where most of the
  planner's methods and rates start, and where its idea of a sensible strategy comes from.

Thanks also to Weird Gloop for hosting the wiki and keeping its data open to tools like this one, and to Jagex
for the official plugin API and the public player data APIs the planner reads.

## Jagex
Created using intellectual property belonging to Jagex Limited under the terms of Jagex's Fan Content Policy.
This content is not endorsed by or affiliated with Jagex.

## Licence
GPL-3.0-or-later (`LICENSE`). Game data built from the RuneScape Wiki is CC BY-NC-SA 3.0, and the plugins are
distributed on the Plugin Hub under Jagex's terms: `NOTICE.md` and `plugins/LICENSE.md` say what applies where.
