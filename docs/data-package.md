# Data package

The planner's tables (about 19 MB under `src/rune_advisor/data/`, 1.7 MB compressed) ship apart from the app, so a
data fix reaches users without a rebuilt, re-signed app. Code: `src/rune_advisor/datapkg.py`; CLI: `radv data`.

## Reading data

Every reader calls `datapkg.path(name)` / `read_text` / `load_json` / `load_toml` / `exists`. A file comes from the
active package when it has one, else from the data bundled in the wheel; with no package installed the planner
reads exactly the bundled files. A packaged file that fails to parse falls back to the bundled copy.
`sources/game_configs.load()` (the planner's) uses the package; `GameConfigs.read()` and `export_api.py` read
bundled data only, on purpose.

The active package is resolved once per process. `apply` and `rollback` change what the *next* start reads, never
what this process has already cached (`@cache` in the modules), so bundled and packaged data cannot mix.
`RUNE_ADVISOR_BUNDLED_DATA=1` forces the bundled data (for the generators).

## Format

`rune-advisor-data-<format>-<version>.tar.xz` plus `<same name>.sig`.

- Members: `manifest.json`, `NOTICE`, and the data files at their relative paths (`game_configs/quests.json.gz`...).
- `manifest.json`: `format` (int), `version` (digits and dots, `2026.11.12` or `2026.11.12.1`; compared numerically),
  `built` (UTC), `files` = {name: {sha256, size}}.
- `.sig`: base64 Ed25519 signature over the whole `.tar.xz`.
- `DATA_FORMAT` (in `datapkg.py`, now 1) is bumped only when the planner's logic needs a different data shape. An app
  takes only packages of its own format, so older apps keep getting fixes for theirs; the index lists the newest
  package per format.

## Trust model

- Public keys are in the app (`datapkg.PUBLIC_KEYS`, empty until the first release key exists). The private key
  lives outside the repo and is given to the build script by path or environment variable.
- Verification comes first: the signature is checked over the raw archive bytes before the archive is opened.
- Unpacking takes regular files only (no links, devices, directories), safe relative names (no absolute, `..`,
  backslash), at most 500 members, 200 MB per member, 600 MB total; every file must be in the manifest with the
  recorded size and sha256, and nothing else may be in the archive. Files are written by the code, never by
  `tar.extract`.
- The index (`<base>/data/index.json`) is not signed and is not trusted: it only names a file, which must be called
  `rune-advisor-data-<format>-<version>.tar.xz`. An attacker who controls the index can at most make the app
  fetch a package that then has to verify. A replayed old (validly signed) package is refused as a downgrade
  (`apply <file> --force` overrides for a local file).
- Compromise of the release key means shipping an app release with a new key in `PUBLIC_KEYS`.

## Installing, updating, rolling back

Packages live under `config.DATA_HOME` (`%LOCALAPPDATA%\rune-advisor\data` on Windows, `$XDG_DATA_HOME/rune-advisor/data`
elsewhere; `RUNE_ADVISOR_DATA_HOME` overrides): `packages/<version>/` and `state.json` = {current, previous}. An install
unpacks to a staging folder beside the others, renames it into place, then switches `state.json` with `os.replace`.
One previous package is kept; older ones are deleted. At start, current is used if its manifest matches the format
and every file is present at the recorded size, else previous, else the bundled data.

Index format: `{"packages": {"1": {"version": "2026.11.12", "file": "rune-advisor-data-1-2026.11.12.tar.xz"}}}`
at `<base>/data/index.json`, the package and its `.sig` beside it.

Settings (`rune_advisor.toml`): `[data] url = "https://..."`, `check_updates = true`; env `RUNE_ADVISOR_DATA_URL`. There is no
default host: with no URL nothing calls out, and `check_updates = false` stops checks even with one.
`datapkg.check_on_launch(settings)` is the hook for the app's start (the first-run prompt, not built yet, will call it); it never raises.

    radv data status          what is read, what is installed, update settings
    radv data check           ask the index whether something newer exists (installs nothing)
    radv data apply [file]    install a local package (needs its .sig beside it) or download the newest
    radv data rollback        back to the previous package, or to the bundled data

## Cutting a release

    uv run python scripts/build_data_package.py --keygen --out ~/keys     # once; add the printed public key to PUBLIC_KEYS
    uv run python scripts/build_data_package.py --key ~/keys/rune-advisor-data.key --version 2026.11.12

It runs the test suite first and refuses to build if it fails (`--skip-tests` exists for the package tests only),
then writes the archive and `.sig` to `dist/data/`. Publish with

    uv run python scripts/publish_data_package.py dist/data/rune-advisor-data-1-2026.11.12.tar.xz [--dry-run]

which uploads to `dl.runeadvisor.com` over sftp (account `rune-dl`, the user's SSH key, directory `/data`; accept
the host key once with a manual `sftp rune-dl@dl.runeadvisor.com`): it checks the package the way an app would
(refusing when `PUBLIC_KEYS` is empty: no app could verify it), uploads the package and signature, then the
index last by rename (the index is what makes the package visible), fetches all three back over HTTPS and
compares, and removes superseded packages (per format the indexed version and the one before it stay). A
published file is never overwritten. Apps find it with `[data] url = "https://dl.runeadvisor.com"`; there is
still no default URL in the app. `NOTICE` in the package carries the wiki attribution and the CC BY-NC-SA 3.0
statement; the data is shared non-commercially under that licence.

## Rebuilding the data

All generators write into `src/rune_advisor/data/`; run them from a checkout, commit the result, then cut a package.
Most need network access to the wiki (CC BY-NC-SA). Order matters where one reads another's output.

| File | Produced by | Notes |
| --- | --- | --- |
| `wiki_rates.json` | LLM extraction of the training guides, then `scripts/embed_wiki_recipes.py`, `link_drop_sources.py`, `validate_wiki_rates.py` | **Manual review**: a model extracts method rows from each guide's source, each with a quote; `validate_wiki_rates.py` keeps only rows whose quote is verbatim in the page and whose numbers appear in the quote; a second pass tags each row (solo, regular training, bought supplies, typical or best-case figure, suspicious with a reason); a person reviews the suspicious rows before the file is replaced |
| `wiki_rates_f2p.json` | same pipeline, free-to-play guides; `scripts/f2p_recipe_mapping.json` is hand-written | **Manual review** |
| `drop_tables.json` | `build_drop_tables.py` | before `link_drop_sources.py`, `build_unfarmable_monsters.py` |
| `unfarmable_monsters.json` | `build_unfarmable_monsters.py` | category lists; check new categories |
| `boss_rates.json` | `build_boss_rates.py` | money making guides; guides go stale, spot-check |
| `collection_logs.json` | `build_collection_logs.py` | wiki log calculator module |
| `equipment.json` | `build_equipment.py` | |
| `recipe_catalogue.json` | `build_recipe_catalogue.py` | |
| `disassembly.json` | `build_disassembly.py` | |
| `shops.json` | `build_shops.py` | |
| `skilling_sim.json` | `build_skilling_sim.py` (wiki Lua via `wiki_lua_to_json.lua`) | check with `check_sim_vs_calculator.py` |
| `mechanics.json` | `build_mechanics.py <scratch>` | same Lua evaluator; check with `scripts/check_sim_vs_calculator.py` and `check_sim_vs_guides.py` |
| `perks.json` | `build_perks.py` | the test fixture `perk_checks.json` guards it |
| `boosts.json` | `build_boosts.py` | the god banner row is added by hand |
| `urns.json` | `build_urns.py` | |
| `elite_outfits.json` | `build_elite_outfits.py` | |
| `invention_blueprints.json` | `build_invention_blueprints.py` | footnoted (gated) blueprints need reading |
| `quest_xp.json` | `build_quest_xp.py` | |
| `achievement_xp.json` | `build_achievement_xp.py` | |
| `toolbelt.json` | `build_toolbelt.py` | |
| `teleports.json` | `build_teleports.py` | |
| `alch_values.json` | `build_alch_values.py` | |
| `item_names.json` | `build_item_names.py` (`--lookup` names unknown ids from the wiki) | |
| `game_configs/*.json.gz` | `import_game_configs.py` from the dev plugin's config dump | needs the beta client; re-run after game updates |
| `lamp_xp.json`, `elite_xp.json`, `skills.json` | one-off wiki extracts, no generator | refresh by hand when the wiki module changes |
| `conversions.json`, `content_rates.json`, `deathtouched_dart.json`, `dnd_resets.json`, `shop_cadence.json`, `shops_extra.json`, `weekly_claims.json`, `xp_rates.toml`, `xp_resources.toml` | hand-written | from the wiki pages named in each file |

Generators read bundled data through the planner's modules; set `RUNE_ADVISOR_BUNDLED_DATA=1` keeps an installed package from
shadowing the tree being regenerated.
