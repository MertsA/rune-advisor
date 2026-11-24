# Web planner (proof of concept)

The planner running in a browser tab, served as static files. Nothing is computed on a server: the page loads
CPython compiled to WebAssembly (Pyodide), the planner package and its data, and plans in a web worker. Player
data comes from the export API, the same way the desktop planner reads it.

Built 2026-11-17 as a proof of concept and run only on this machine; not on runeadvisor.com yet. Since
2026-11-24 it has what serving it needs on our side: tokens derived from the account key, settings stored
encrypted with the token, and the big files split off for a CDN with hash checks.

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
| Worker | `packaging/web/worker.js` | Loads Pyodide, fetches the wheels and the wiki cache with their SRI hashes, writes the two settings files, runs one planner call at a time |
| Planner entry | `src/rune_advisor/app/web.py` | `run(view, body, refresh, progress)` around the desktop app's `Planner`, with a context factory (`rune_advisor.planning`) that reads only the export API |
| Token keys | `site/assets/tokenkeys.js` | Deriving tokens from the account key; sealing and opening the settings blob (AES-GCM). `src/rune_advisor/token_keys.py` is the Python twin the tests compare it with |
| Build | `scripts/build_web.py` | Pinned Pyodide (npm tarball, SHA-256 checked), pinned wheels, this checkout's wheel, the wiki tables; the big files go to `planner-assets/` under hashed names |
| Local hosting | `scripts/serve_web.py` | Static files with the Content-Security-Policy below, and a local API |

A planner call blocks the worker, so the page keeps the jobs and cancels a running one by ending the worker
and starting another (about two seconds, and the loaded player is read again).

## Signing in

The planner page never sees the account key. Without a token it sends the user to `/account/#planner`. The
account page opens the account with the key, derives the account's **web planner token** from it (making it the
first time: `kind: "web"`, read only, one per account; `docs/export-api.md` "Derived tokens"), puts it in
`sessionStorage` and returns to `/planner/`. The token lasts as long as the tab, so each visit needs the key again.
Every tab and browser derives the same token: nothing is revoked on sign-in. If the API refuses the token the
planner goes back to the account page.

The planner token is the bearer value, a dot, and the key to the token's settings blob. Only the part before the
dot is ever sent. If we rotate the token after a leak (`export_api token rotate`), the next sign-in derives its
new value and the account page re-encrypts the settings for it.

The account page shows the planner button only where the page names a planner (`<meta name="planner">`, added
by the build), so the live site is unchanged until that is wanted.

## Settings

`rune_advisor.toml` and `overrides.toml`, the desktop planner's two files, are two text boxes. Saved, they are
sealed in the page with AES-256-GCM under the token's settings key and stored with the token (`/v1/blob`, at most
64 KiB, versioned so two tabs cannot silently undo each other). The server holds bytes it cannot read; every
browser signed in to the account sees the same settings. The planner takes its API address and token from the
page, never from these files. Settings an older build kept in `localStorage` are offered once and moved into the
blob at the next save.

## What differs from the desktop planner

- **Player data is the plugin's export only.** A page cannot read Jagex's hiscores or Adventurer's Log
  (no cross-origin access), so there is no planning for a name without the plugin. Ironman status, which the
  desktop planner takes from the hiscores, is a setting (`ironman = true`, or a `group`).
- **Wiki tables are fixed at build time.** Achievements, their requirements, quests and GE prices are
  fetched by `build_web.py` and shipped as the planner's HTTP cache (`CachedClient.keep_stale`). They are as
  old as the build. They belong in the data package.
- **The quest list** comes from one player's Adventurer's Log at build time. It also belongs in the data
  package.
- **Settings** are the desktop planner's two files as text boxes, synced through the token's blob (above). The
  desktop planner does not read the blob yet.
- **No data package updates**, no archives, no plugin database, no `radv` commands.

## Hosting and Content-Security-Policy

The pages, the worker and Pyodide's small loader (`pyodide.js`: a worker may only import scripts from its own
site) are served by our servers with the rest of the site. The big files (Pyodide's runtime, the wheels, the
wiki tables: about 17 MB) go to `planner-assets/` under names that never change meaning, `pyodide/<version>/`
and `files/<sha256>/<name>`, for a CDN (`--assets-url`, planned: Cloudflare in front of `dl.runeadvisor.com`).
The manifest, from our servers, gives each wheel and the tables an SRI hash, and the worker fetches them with
it: the browser refuses a file that differs from the one built (tried with a tampered file). Pyodide's own
runtime files are loaded by its loader and are not checked.

The site's policy is `default-src 'self'` with the API in `connect-src`. The planner adds `'wasm-unsafe-eval'`
to `script-src`, which allows compiling WebAssembly and nothing else (`eval` stays blocked), and the assets'
host to `script-src` and `connect-src` when they come from a CDN (`scripts/serve_web.py` `csp()`).

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

First visit downloads about 17.5 MB: Pyodide 13.5 MB, wheels 3.3 MB (since 2026-11-24 without typer, rich and
click), wiki tables 0.6 MB. Served compressed the WebAssembly file shrinks further; nothing here measures that.

2026-11-24 on the desktop: the guide for the same character takes 124 s end to end (`radv guide`, start-up
included). Features added since the measurements above had pushed it to over 13 minutes before c38b948 brought
it back; the browser was not measured again.

## Not done

- Not deployed. The servers' side is still to set up: the CDN with its cache rules and CORS, a name to upload
  to, and the planner's CSP.
- Only Chromium was driven (headless). Firefox and Safari are untested.
- The wiki tables and the quest list are fixed at build time; they belong in the data package. The page now says
  the day they were fetched.
- No settings form (two text boxes); the desktop planner does not sync settings yet.
- The privacy policy does not mention the planner page, its token, the settings blobs or the CDN.
