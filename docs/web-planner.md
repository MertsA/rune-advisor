# Web planner (proof of concept)

The planner running in a browser tab, served as static files. Nothing is computed on a server: the page loads
CPython compiled to WebAssembly (Pyodide), the planner package and its data, and plans in a web worker. Player
data comes from the export API, the same way the desktop planner reads it.

This is a proof of concept, built 2026-11-17 and run only on this machine. It is not on runeadvisor.com.

## Try it

    uv run scripts/build_web.py --api http://127.0.0.1:8766 --quest-list-from YourName
    uv run scripts/serve_web.py --copy-character YourName

`build_web.py` writes `dist/web/`: the site plus `/planner/`. `serve_web.py` serves it at
http://127.0.0.1:8765/planner/ and starts an export API of its own on port 8766 with a throwaway data folder
(`dist/web-dev/api`) and one account. The account's key is in `dist/web-dev/account-key.txt`.
`--copy-character` copies a character's stored state from the API named in `rune_advisor.toml` into that
account, so there is something real to plan for. `--quest-list-from` names any player whose Adventurer's Log
supplies the list of quests that exist today (the same for everyone; only the titles are kept).

## How it fits together

| Piece | Where | What it does |
| --- | --- | --- |
| Page | `packaging/web/index.html`, `planner.js` | The desktop app's `app.js` unchanged, plus a page script that answers its `/api/...` calls (`window.raTransport`): job table, result cache, cancel, settings |
| Worker | `packaging/web/worker.js` | Loads Pyodide and the wheels from the same site, unpacks the wiki cache, writes the two settings files, runs one planner call at a time |
| Planner entry | `src/rune_advisor/app/web.py` | `run(view, body, refresh, progress)` around the desktop app's `Planner`, with a context factory that reads only the export API |
| Build | `scripts/build_web.py` | Pinned Pyodide (npm tarball, SHA-256 checked), pinned wheels, this checkout's wheel, the wiki tables |
| Local hosting | `scripts/serve_web.py` | Static files with the Content-Security-Policy below, and a local API |

A planner call blocks the worker, so the page keeps the jobs and cancels a running one by ending the worker
and starting another (about two seconds, and the loaded player is read again).

## Signing in

The planner page never sees the account key. Without a token it sends the user to `/account/#planner`. The
account page opens the account with the key as it always did, then makes a read token labelled "Web planner",
revokes the ones it made that way before, puts the token in `sessionStorage` and returns to `/planner/`. The
token lasts as long as the tab, so each visit needs the key again. If the API refuses the token the planner
goes back to the account page.

Consequences as built: one planner tab per account at a time (a second sign-in revokes the first tab's
token), and the account page shows the planner button only where the page names a planner
(`<meta name="planner">`, added by the build), so the live site is unchanged until that is wanted.

## What differs from the desktop planner

- **Player data is the plugin's export only.** A page cannot read Jagex's hiscores or Adventurer's Log
  (no cross-origin access), so there is no planning for a name without the plugin. Ironman status, which the
  desktop planner takes from the hiscores, is a setting (`ironman = true`, or a `group`).
- **Wiki tables are fixed at build time.** Achievements, their requirements, quests and GE prices are
  fetched by `build_web.py` and shipped as the planner's HTTP cache (`CachedClient.keep_stale`). They are as
  old as the build. They belong in the data package.
- **The quest list** comes from one player's Adventurer's Log at build time. It also belongs in the data
  package.
- **Settings** are the desktop planner's two files, `rune_advisor.toml` and `overrides.toml`, as text boxes
  kept in the browser's `localStorage`. Nothing syncs them.
- **No data package updates**, no archives, no plugin database, no `radv` commands.

## Content-Security-Policy

The site's policy is `default-src 'self'` with the API in `connect-src`. The planner needs one addition:
`script-src 'self' 'wasm-unsafe-eval'`, which allows compiling WebAssembly and nothing else (`eval` stays
blocked; the page was tested with it blocked). The worker and everything it fetches are same-origin.

## Measurements

One character (a maxed-ish ironman with a full bank), this machine, 2026-11-17.

| | Desktop (CPython 3.14) | Chromium (Pyodide 314.0.8) |
| --- | --- | --- |
| Start to "ready" | - | 1.8 s (files from loopback) |
| Load the player (profile) | 2.0 s | 4.0 s |
| Guide, all tracks | 80 s | 125 s |
| Next session, 2 h | 35 s | 54 s |
| Focus on one goal | 0.2 s | 1.0 s |

The browser runs the same Python about 1.55 times slower. Both columns are after three caching fixes made
the same day (what the player owns was being recomputed tens of thousands of times): before them the desktop
guide took 134 s and the browser's 207 s. The fixes change no output: the guide, session and profile results
are byte-identical with `PYTHONHASHSEED=0`. Without a fixed seed two runs of the same code differ in a few
tie-breaks between equally rated monsters, which is an ordering bug of its own.

A profile of the guide (`cProfile`, desktop) says the time is spent re-deriving facts about one player
(holdings merged across inventories, owned equipment, discovered blueprints), not on tables that could be
precomputed into the data package. More caching of that kind is the next speed-up; compiled helpers come after.

First visit downloads 18 MB: Pyodide 13.5 MB, wheels 3.8 MB (the planner and its data are 3.0 MB of that),
wiki tables 0.6 MB. Served compressed the WebAssembly file shrinks further; nothing here measures that.

## Not done

- Not deployed, not behind a CDN, no cache headers or versioned file names.
- Only Chromium was driven (headless). Firefox and Safari are untested.
- No sync of settings between browsers; no settings form (two text boxes).
- The page does not say how old the wiki tables are.
- `rune_advisor.cli` is imported for its context builder, which pulls typer, rich and click into the
  download. Moving the builder out of `cli.py` would drop them.
- The privacy policy does not mention the planner page or its token.
