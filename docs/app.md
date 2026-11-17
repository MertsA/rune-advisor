# Planner app

One desktop window owned by the planner process: starting it opens the window, closing the window ends the
process. The CLI (`radv`) is unchanged.

## Architecture

- `rune_advisor/views.py`: the data behind `radv guide`, `radv next` and `radv focus` (`guide_sections`, `next_picks`,
  `charge_steps`, `session_prep`) plus JSON-able view models (`profile_view`, `guide_view`, `next_view`,
  `focus_view`). `cli.py` prints the first group; the app serialises the second. Both use the same `GuideEntry`,
  `Pick` and `Focus` structures.
- `rune_advisor/app/planner.py`: validates a request (`normalise`, which is also the cache key), builds a `Context`
  with the CLI's own `cli._ctx` (kept per player and context options, dropped on refresh) and calls a view.
- `rune_advisor/app/jobs.py`: background jobs. One planner call runs at a time (a `Context` memoises as it plans).
  Progress is reported between planner steps; cancelling marks the job cancelled at once, stops it at the next
  progress call, and drops a result that arrives late. Results are cached in memory by (view, player, options).
- `rune_advisor/app/server.py`: stdlib `ThreadingHTTPServer` on 127.0.0.1, random port. Every request needs the
  per-launch secret (`X-RA-Secret` header or the `ra_secret` cookie), the Host header must be the server's own
  address, an Origin header (if sent) must be its own origin, and POST bodies must be `application/json`. The
  shell opens `/?s=<secret>`, which sets an HttpOnly SameSite=Strict cookie and redirects to `/`.
  A CSP allows only same-origin scripts and styles. Static files come from `app/static/` only.
- `rune_advisor/app/__init__.py`: `main()` / `run()`. Starts the server on a thread and a pywebview window at its
  URL; when the window closes the server stops and the process returns. pywebview is imported only there.
- `rune_advisor/app/static/`: `index.html`, `style.css`, `app.js`: no build step, no network fetches; planner text
  is inserted as text nodes, never as markup. Light/dark follow `prefers-color-scheme`.
- The last player is remembered server-side in `state/ui.json` (the window's origin changes with the random
  port, so `localStorage` would not survive a restart).

## Running

    uv sync --extra ui            # pywebview; the CLI needs nothing extra
    uv run radv ui                 # or: uv run rune-advisor    (the window)
    uv run radv ui --no-window     # dev: prints http://127.0.0.1:PORT/?s=SECRET, open it in any browser

Needs a display. On Linux the `ui` extra installs pywebview's Qt backend (PyQt6 + QtWebEngine wheels), so
no GTK or WebKitGTK system packages are needed; a bare system may still lack a library Qt links to
(`libxkbfile` on a minimal Arch). On Windows pywebview uses WebView2. Distro packages (AUR, Flatpak) can
depend on the system's GTK/WebKitGTK or Qt instead. The launch was checked headless with
`QT_QPA_PLATFORM=offscreen` (window opens, page loads, closing it ends the process), never on a real desktop
in the sandbox. pywebview logs one "Refused to apply inline style" line: its own injected style, blocked by
our CSP, harmless. Config and state come from the same place as the CLI
(`RUNE_ADVISOR_CONFIG`, `RUNE_ADVISOR_STATE`).

## JSON endpoints

All need the secret. Bodies are JSON; errors are `{"error": "..."}`.

| Method and path | Purpose |
| --- | --- |
| `GET /api/meta` | `{tracks: {name: description}, moods, prefs}` |
| `POST /api/prefs` | `{last_player}`; returns saved prefs |
| `POST /api/jobs` | `{view, player, options, refresh}` -> 202 and a job. `view` is `profile`, `guide`, `next` or `focus`. A cached answer comes back as a job already `done` with `cached: true`. 400 for a bad request |
| `GET /api/jobs/<id>` | `{id, view, state, progress: {done, total, message}, cached, elapsed, result \| error}`; state is `queued`, `running`, `done`, `error` or `cancelled` |
| `POST /api/jobs/<id>/cancel` | cancel a queued or running job |
| `POST /api/cache/clear` | drop cached results and built contexts |

Options: all views take `f2p`, `plugin_only`, `beta`, `group` (list of names). `guide`: `tracks`, `mood`,
`session` (hours), `top`. `next`: `hours`, `mood`, `replan`, `max_replans`. `focus`: `goal` (as for `radv focus`).
Results: `profile` (skills, quest points, notes), `guide` (`tracks[]` with `entries[]`: headline, hours,
play_hours, value, why, left, unsure, steps, tags, confidence, prep), `next` (`picks[]` as entries plus shares
and extra_hours, `prep`), `focus` (the `Focus` tree: options, needs, route, prep, nested sections).

## Not covered yet

Gear, perks, augment, D&Ds, roadmap and gaps views; settings and first-run onboarding; the pywebview launch
itself has not been run on a machine with a display.
