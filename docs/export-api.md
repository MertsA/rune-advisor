# Export API

The HTTP service the published exporter plugin posts its state to, so the planner can read a character
without the client's `PluginData.db` file. Server: `src/rune_advisor/export_api.py` (Python, standard library
only; it reuses `rune_advisor.wire` for the protocol and `sources/game_configs.py` to derive quests and
achievements). Plugin side: `plugins/rune_advisor_export/lib/wire.lua` (`httpTransport`) and
`lib/settings.lua` (the token panel). Planner side: `rune_advisor.api_client` and `radv pull`.

## Why Python

The receiving half of the wire protocol (layout from the game configs, canonical bytes, FNV-1a hashes,
delta application) already existed in Python as `rune_advisor.wire.replay`, the Lua and Python layouts have to
hash identically (`test_wire_protocol_replay`), and the planner that consumes the result is Python. Anything
else would mean a second implementation of the layout to keep in step. The server needs no framework: three
routes, small JSON bodies, one process behind nginx.

## URL and auth

- Base URL in the plugin manifest: `https://api.runeadvisor.com` (`urls.api`), with
  `httpAccess` as a *required* permission (optional until 2026-11-11: it had to be re-granted every session, and
  the plugin does nothing without it). The plugin posts to `<base>/v1/state`.
- Token: `Authorization: Bearer <token>`, a `submit` token for the plugin, created by the account's owner with
  `POST /v1/tokens` (see Accounts below; or by the operator with
  `python -m rune_advisor.export_api token new <owner>`) and pasted into the plugin's settings layer (the settings
  icon on the plugin in the Plugin Hub's Manage tab; `"hasSettingsIcon": true`, `Event.SettingsLayerReady`).
  The plugin checks it first (`GET /v1/players` with the pasted token, ~2 s after the last edit) and saves it (the client's
  PersistentDB, `settings:apiToken`) only when the API answers 200: a mistyped or cut-short token shows
  "Token not recognised" instead of being stored, a revoked one "That token was revoked", and while the API
  is unreachable (or httpAccess isn't granted) nothing can be saved, which the panel says. Once saved, a
  later "unknown" from the server only holds the plugin off (`retry_after`); only an explicit "revoked"
  makes it delete the token. Emptying the field doesn't either: the client fires the field's content-changed
  hook with "" when the panel opens (2026-11-09), so it only cancels a pasted token that hasn't been checked
  yet. An accepted token posts the full state at once. When the exporter has no token or no httpAccess it
  says so once in the console ("Rune Advisor: not posting: ...") instead of staying silent. The plugin is HTTP-only: without
  a saved token and httpAccess nothing is sent or saved anywhere, and PersistentDB holds only the token, the
  last server-requested interval and the last acknowledged state. The same token goes in `rune_advisor.toml` for
  the planner:

  ```toml
  [api]
  url = "https://api.runeadvisor.com"
  token = "..."
  ```
  (or `RUNE_ADVISOR_API_URL` / `RUNE_ADVISOR_API_TOKEN`.) The design note of 2026-10-31 had the token in the URL
  path; a header keeps it out of nginx access logs, and the API documents `HTTPRequest:AddHeader`.
- Tokens are stored hashed (SHA-256) in the SQLite store; `token add <owner> <token>` registers a value
  minted elsewhere (so a localhost server accepts the token a plugin already holds for the public one); `token list` shows prefixes, `token revoke`
  takes the token or its first 6 characters.

## Routes

| Route | Auth | Purpose |
|---|---|---|
| `POST /v1/state` | `submit` token | the plugin's post: JSON `{player, kind: full\|delta, layout, base, new, seq, body: base64}`; a full post adds `manifest` (a hash), `absent` (when some), `skills`, `inventories`, `configs`, `plugin` (plugins up to 0.4.3: `named` instead of `manifest`/`absent`) |
| `POST /v1/configs` | `submit` token | a part of the config difference (Config sync below); the result is private to the account until confirmed |
| `GET /v1/players` | `submit` or `read` token | characters this token may see (a limited read token sees its own); the plugin uses it to check a pasted token |
| `GET /v1/snapshot` + `X-Player` | `read` token | the schema-2 snapshot the planner reads (quests, achievement tests, skills, vars, item log, inventories with names) |
| `GET /v1/state` + `X-Player` | `read` token | the raw stored record (hashes, layout directories, base64 canonical bytes) |
| `GET /v1/configs`, `/v1/configs/<fp>` | any token | the global config generations plus the caller's own pending ones |
| `POST /v1/accounts` | none | body `{captcha}`; `201 {account, key}`, the key shown once |
| `GET /v1/account` | account key | id, created and last-active days, character and token counts, quotas |
| `DELETE /v1/account` | account key | delete the account, its tokens and every character's state |
| `GET /v1/tokens` | account key | live tokens: `id`, `prefix`, `label`, `scopes`, `characters`, `created`, `last_used` (days) |
| `POST /v1/tokens` | account key | body `{label, scopes: ["submit"\|"read"], characters: [names]}`; `201` with the token, shown once |
| `DELETE /v1/tokens` + `X-Token-Id` | account key | revoke a token |
| `DELETE /v1/players` + `X-Player` | account key | delete one character's state |
| `GET`/`POST /v1/consent` + `X-Player` | account key, or the submit token that posted the character | read / set the character's consent to analysis |
| `GET /v1/health` | none | `{"ok": true}` after a real database read; `503 {"ok": false}` when the read fails |
| `OPTIONS` on any route | none | CORS preflight: 204 for the site's origin, 403 otherwise (see CORS) |

Nothing that says whose request it is travels in a URL (the user's rule, 2026-11-15: keep it out of access
logs by design instead of cleaning the logs): web server access logs record request lines, and they should
not say which character or which account's token an address asked about. The character is named in the
`X-Player` request header (the name percent-encoded as UTF-8), a token's id in `X-Token-Id`, keys and tokens
in `Authorization`, the rest in the body. A name or an id in the path is no route, and a request with a query
string answers 400, so a client built to put something there fails at its first try. The one path parameter
is `/v1/configs/<fingerprint>`, a version of the game's definitions. Both headers are in the CORS allowed
headers.

## Accounts, scopes and quotas

There is no login. An account is a random key (`raa_...`, 256 bits) that `POST /v1/accounts` returns once; the
store keeps only its SHA-256, so a lost key cannot be recovered, only replaced by making a new account. The key
manages tokens and deletes data; it never posts or reads a snapshot. Tokens are what plugins and planners hold:

- `submit`: `POST /v1/state`, `POST /v1/configs`, `GET /v1/players`, and `GET`/`DELETE /v1/posted` (below). A
  submit token cannot read a snapshot or the raw state (403): a leaked plugin token exposes nothing that was
  posted beyond character names, though it can delete what it posted.
- **Deleting what a token posted** (2026-11-13), for someone who lost the account key but has the token in
  their plugin: `GET /v1/posted` returns `{token: {label, prefix, created, last_used}, posted: [{player,
  last_post}]}`; `DELETE /v1/posted` deletes those characters and revokes the token (or its plugin would post
  them again), answering `{deleted: [names], revoked: true}`. With the account key the same is
  `DELETE /v1/tokens/posted` with the token's id in `X-Token-Id`. What a token "posted" is the `token_posts` link, which joins the token's id
  to stored characters' ids inside the token's own account: never a name. A character renamed in the game, or
  a name another player once had, gives a token no hold over a stored character it did not post for, in its
  own account or anyone else's.
- **Consent to analysis** (2026-11-14): a stored character carries its player's opt-in to having its data
  analysed for planner improvements (column `states.consent`: the UTC day it was given, NULL = not given, the
  default; deleted with the character). `GET`/`POST /v1/consent` with the character in `X-Player`
  (`{consent: true|false}` in, `{consent, consent_since}` out) works with the account key for any character
  of the account, and with a submit token for the characters that token has posted for (the `token_posts`
  link, as for deleting: 404 otherwise, and for a character that has not posted yet). `GET /v1/account`
  (`players`) and `GET /v1/posted` (`posted`) return `consent` and `consent_since` per character. The plugin
  stores nothing of it: every accepted `POST /v1/state` reply carries the character's `consent`, so its
  settings show the server's answer and a change made on the site appears at the next post. Consent changes
  nothing about what is collected or stored. It is never copied onto data: an analysis takes its input from
  `Store.analysis_states()` when it runs, which yields only characters consenting at that moment, each as
  `{id, record}` with the stored character's random id in place of its name and no account. So withdrawing
  excludes what is already stored from every later analysis and giving it includes it. Any analysis code,
  and any history kept later, must go through that one gate. Each change is noted in the deletion log
  (`{"consent": <character id>, "value": <day or null>}`) and re-applied in order after a restore, so a
  backup cannot bring back a consent withdrawn since; restored from the accounts copy (no states), every
  character starts without consent. `stats` prints `consenting_players`.
- `read`: `GET /v1/players`, `GET /v1/snapshot`, `GET /v1/state`. With `characters` set, it sees
  only those (matched case-insensitively); other characters answer 404 and are left out of the listing, so a group
  mate gets one character, not the account. A read token cannot post (403).
- A token with the wrong scope gets `403 {accepted: false, reason}`. Unknown or revoked tokens keep the
  plugin-facing `401` body (`token`: "unknown" / "revoked", `retry_after`) unchanged.
- What is stored per token: id, SHA-256, first 6 characters, label, scopes, characters, created day, last-used
  day, revoked day, and (table `token_posts`, 2026-11-13) which stored characters it has posted for with the
  day of the last post: a link between the token's id and the character's id, written at most once a day per
  pair, gone when the token is revoked or the character deleted, and left out of the accounts backup.
  `GET /v1/tokens` returns it as `posted: [{player, last_post}]` per token, and `GET /v1/account` returns
  the account's characters as `players: [{player, last_post, posts}]`, so the account page can show which
  token belongs to which plugin before one is revoked. Per account: id, key hash, created day, last-active day. No IP addresses and no user agents
  are stored anywhere; the account-creation limit lives in memory and is gone on restart.
- Revoking a token (or deleting its account) leaves a bare revoked hash for 90 days so a plugin still using it
  is told "revoked" and stops (without the row it would hear "unknown", keep the token and retry forever);
  after that the sweep deletes the row. Bare (`export_api.BARE`, since 2026-11-13) means the token's SHA-256,
  the day it was revoked and a random row id: no account, label, characters, prefix, scopes, created or
  last-used day. `stats` counts these rows as `revoked_tokens`, so a store with no accounts can show some; the
  sweep also strips rows revoked by an older server, which kept the prefix, scopes and days.

Account creation is guarded by a per-IP limit (settings `account_ip_limit`, default 5 per `account_ip_window`
seconds, default a day; counted per successful creation; an IPv6 client counts as its /64, `limit_key`, since
2026-11-13: per exact address it had 2^64 to rotate through) and a pluggable captcha. When `serve` is given the
Cloudflare Turnstile secret it verifies the body's `captcha` (the Turnstile response) with Cloudflare's
siteverify, passing the caller's address, and refuses with 403 if it fails or Cloudflare is unreachable;
without a secret the no-op verifier accepts everything (tests, self-hosting). **The secret comes from a file,
never a setting** (2026-11-13): `--turnstile-secret-file PATH`, or `RUNE_ADVISOR_API_TURNSTILE_SECRET_FILE`,
or, with neither, the systemd credential `turnstile_secret` (`LoadCredential=turnstile_secret:<path>` in the
unit, read from `$CREDENTIALS_DIRECTORY/turnstile_secret`). One trailing newline is dropped. A file that is
named but missing, unreadable or empty stops the start (`Turnstile secret: ... not starting without the bot
check`). The value is read once at start (a rotation is a new file and a restart), is kept in memory only,
and appears in no output: `settings show` prints `turnstile_secret = set (from ..., read when the service last
started)` or `unset`, and `settings set turnstile_secret` is refused with a pointer to the file. At start the
service logs ` INFO account creation: Turnstile check on (secret from ...)` or `... no bot check ...`. Behind nginx the client address is the last `X-Forwarded-For` entry,
only when `settings set trust_proxy 1` (otherwise a forged header would dodge the limit); nginx should send
`proxy_set_header X-Forwarded-For $remote_addr;` or `$proxy_add_x_forwarded_for`.

Quotas per account, live-reloaded: `max_characters` (default 10; a post that would add an 11th character gets
`403`, existing characters keep posting) and `max_tokens` (default 20 live tokens; `POST /v1/tokens` gets `403`).
Inactivity: an account with no post and no read (any use of one of its tokens) for `retention_days` (default
365) is deleted with its tokens and states by a sweep that runs at startup and every 6 hours (`sweep` runs it
by hand). Managing tokens with the account key does not count as activity.

Admin CLI (same store): `token new <owner> [--scopes submit,read] [--label L] [--characters A,B]` (defaults to
both scopes; makes the account `<owner>` when missing, with no key), `account key <owner>` issues a key to such an
account, `account list|delete`, `token list|revoke|add`, `settings show|set|unset|chosen` (numbers only; the Turnstile secret is a file, see above; `settings.json`
holds only the keys that were set, so the rest follow the code's defaults: `unset` returns one to its default,
`chosen` lists what the file sets), `stats`, `sweep`, `migrate`, `check` (below).

Planner: `[api]` in `rune_advisor.toml` takes `token = "..."` and/or `tokens = ["...", "..."]` (read tokens); a
character is looked up under each in turn, so a group mate's one-character token joins the planner's own.

Replies are JSON and every one carries `interval`: the seconds the plugin should wait between posts.
`200 {accepted: true, hash, interval}` for a post the server applied; `409 {accepted: false, reason, interval}`
when the state is out of step (no state yet, base hash differs, the hash after applying differs, layout
differs): the plugin then resends the full state. `401 {token: "revoked" | "unknown", retry_after}` for a
refused token: "revoked" makes the plugin delete its copy, show "Token revoked by the server: paste a new one"
in the panel and post nothing until a new one is pasted (no more full dumps for a dead token); "unknown" (a typo, a missing
header) holds off for `retry_after` seconds and keeps the token. Anything else (5xx, timeouts, no response):
the plugin keeps its acknowledged state and retries the same delta after ~60 s; if the server had applied it
after all, the next base check fails and the full state follows. The prefix (`/export_api`) is accepted and
may also be stripped by nginx.

## Design rule: the server never chooses what player data the plugin sends

Everything a reviewer needs to know about what leaves the client must be readable in the shipped plugin:
the named var list in `lib/exported_vars.lua`, the inventory allow-list, the layout derivation from the game's own quest
and achievement definitions, and `configdump.lua`'s entry shapes. The server may say *when* and *whether*
the plugin posts (`interval`, `retry_after`, a revoked token) and may name static game configs it lacks
(the 428 handshake), but it never gets to pick player vars, inventories or anything else to collect. A
server-driven var list was considered and rejected on 2026-11-01: a plugin whose data collection can change
after review sidesteps moderation. Adding a var family for the planner is therefore always a plugin release.

## Load control

`<data-dir>/settings.json` holds `post_interval` (seconds between posts, default 10, allowed 3..3600) and
`retry_after` (seconds an unknown token waits, default 300). The server re-reads the file whenever it changes,
so `python -m rune_advisor.export_api --data-dir D settings set post_interval 300` slows every plugin down within
one post; `settings set post_interval 5` speeds them up. The plugin clamps the value to the same range,
applies a shorter interval at once, and remembers the last one in PersistentDB (`settings:postInterval`) so
a fresh login starts at the server's cadence. `python -m rune_advisor.export_api stats [--minutes 10]` prints how
many characters posted in that window (and totals), to choose an interval: a handful of users can post every
5 s, thousands every 5 minutes. Tokens are revoked with `token revoke` (or `DELETE /v1/tokens` with `X-Token-Id`), which keeps the entry marked
`revoked` so the server can tell a plugin still using it to forget it. Quotas, retention and the account-creation
guards are settings in the same file (see Accounts).

**Under load** (2026-11-13, `export_api.Load`). The service is one process, so its work is one core's.
- Every reply's `interval` is `post_interval`, stretched while the service is loaded: when requests used more
  than 60% of a core over a 10 s window (or any was told busy) it grows by half, up to `auto_interval_max`
  (default 300 s); under 30% it shrinks by a sixth, back to `post_interval`. `settings set auto_interval 0`
  turns this off. Plugins follow the interval within one post, so load falls in proportion. This is within
  the design rule above: the server may say *when* the plugin posts, never *what*. Each change is logged
  (` INFO load: requests took N% of a core, M told busy; post interval now X s`).
- More than 24 requests in hand: `503 {accepted: false, reason: "busy...", retry_after: 60, interval}` with
  `Retry-After`, sent before any token or database work. The plugin retries a failed post after about a
  minute. `GET /v1/health` is exempt.
- More than 256 open connections: closed unanswered. The listen backlog is 128.
- Per token, per minute, in memory: state posts `12 + 2 x 60/interval`, config parts 300, snapshot/state reads
  60; beyond that `429` with `retry_after: 60`.
- Cost of a post (a character with a 1,500-item bank, in process): about 2.3 ms since the layout hash is
  cached and inventories are packed as 32-bit words (it was 22.6 ms); a snapshot read about 24 ms.
  `scripts/load_check.py` measures a whole server: it starts a throwaway one on loopback, runs stages of
  simulated plugins that behave like the real one, and prints `key = value` results (exit 0/1/2).
- SQLite runs in WAL mode with `synchronous=NORMAL`: no fsync per post; a power cut may lose the last commits
  (the plugin resends its full state on the resulting 409) but does not corrupt the file.

## Protocol notes

- Delta item entries with item id `0xFFFFFFFF` mean "this inventory is now known (possibly empty)": without
  it an inventory loaded empty (the spike harness, the metal bank with only placeholders) had no delta
  representation and the hashes diverged. Found by replaying the 2026-11-01 beta posts through the server
  (post 51, the bank opening, hashed differently; `MELEE_SPIKE_HARNESS` as a known empty set reproduced the
  plugin's hash).
- At login the plugin seeds inventories it hasn't loaded yet from the last acknowledged state, so the first
  delta of a session doesn't report the bank emptied.
- Inventories a full post marks unknown stay as the server last saw them (`known_invs` in the record).
- Hand-listed fields (plugin 0.5.0): `lib/exported_vars.lua` is a static manifest, every variable written out
  as `{ name, width, kind }` in wire order. Each travels at its width: a VarBit as wide as its mask (an item log
  count is 8 or 16 bits), a whole variable or a client variable 32 bits, never capped; about 17,600 bits for
  ~1,545 fields, where 0.4.x spent 32 bits a name. `scripts/build_exported_vars.py` fills in widths, kinds and
  the generated family blocks (every variable with a prefix, at build time) from the dev plugin's var dump, and
  writes the server's copy, `src/rune_advisor/wire_manifests/<hash>.json` (FNV-1a 32 of `NAME/width,...`); every
  released manifest stays there, so any stored state or history entry can be read. A full post names its
  manifest by hash and the server refuses one it lacks (400: the plugin is newer than the server; deploy the
  server first). The layout's description says `|fields:NAME/width,...` (0.4.x: `|named:NAME,...`), so the two
  can't be mistaken for each other. Only 32-bit fields are read back as signed.
- An entry the client lacks (the name is gone, or it is no longer a VarBit of that width after a game update)
  keeps its slot, sent as 0, and the full post lists it in `absent` (indexes into the manifest; only on full
  posts, since the set is worked out at login). The record keeps the list and the snapshot leaves those vars
  out. The next plugin version corrects the entry. So the layout depends only on the plugin version and the
  game configs: the server never sees a field list it doesn't already hold.
- Item names: the wire carries ids; `data/item_names.json` (from the player's own exports,
  `scripts/build_item_names.py`) names them in snapshots. Unknown ids show as `Unknown item <id>` with a note.
- Skill levels in snapshots are xp-derived and capped at the skill's maximum, as `GetLevelUncapped` reports.
- Quest state is derived from the master var; replayable quests (Dimension of Disaster) read as "started"
  while the API says finished (the known quirk; the journal QP var reconciles it in the planner).

## Config sync (game updates without a release)

The plugin's layout is derived at login from the quest and achievement configs the client has, so a game
update that adds or changes a tested var changes the layout hash. Rather than freezing a layout in the plugin
(no data resource type exists: custom resources are PNG and Ogg only, and the sandbox has no file I/O), the
server learns new config sets from the first client to see them:

1. On the login walk the plugin also hashes every quest and achievement config (`lib/canon.lua`: a canonical
   text with state keys left out, FNV-1a 32) and the whole set (`fingerprint`). The full post carries the
   fingerprint and the plugin version.
2. A fingerprint the server doesn't know answers `428 {need: "configs", base: <fingerprint of the newest set
   it has>, known: {quests: {id: hash}, achievements: {id: hash}}}` (about 22 KB, once per game update).
3. The plugin (`lib/configsync.lua`) walks its configs again as jobs, building entries (`lib/configdump.lua`,
   now shared with the dev plugin) only for ids whose hash differs or the server lacks, and posts
   `{fingerprint, base, part, parts, added: {quests, achievements}, removed: {quests, achievements}}` to
   `/v1/configs`, 150 entries a part, the removed ids with the first part. Nothing else travels: the server
   composes base minus removed plus added and accepts the set only if it hashes to the claimed fingerprint
   (`GameConfigs.compose`, `fingerprint`); any disagreement comes back as 409 with the map again.
4. The plugin then posts the full state, which the server decodes with the learned set.

A set learned this way is **pending**: it decodes posts and answers reads for the account that uploaded it, and
for nobody else. Only the packaged baseline and **global** (confirmed) generations are shared. Why: the
fingerprint is 32 bits, so a client could craft a different set with a known fingerprint; scoped to its
uploader, a collision only hurts the attacker (widening the fingerprint was considered and dropped for that reason).

- Lookup for (account, fingerprint): global first, else that account's pending set. A second account with the
  same fingerprint gets its own 428, uploads its own difference, and gets its own reference.
- Content is stored once, keyed by the SHA-256 of the set without per-player state keys
  (`GameConfigs.content_hash`); `<data-dir>/pending_configs/<hash>/` holds the files and the SQLite tables
  `config_contents` (fingerprint, base, day, +/- counts), `config_refs` (account, hash, upload time) and
  `config_confirmed` the metadata. The number of accounts referencing a hash is the confirmation signal: several
  unrelated accounts uploading byte-identical content is what a real game update looks like.
  Intended rule, not built (the user, 2026-11-15): a pending set confirms itself once more than 20 accounts
  reference the same content for a fingerprint and no account references different content for it, so a
  game-day update needs no manual dump; until then `configs confirm` is by hand. The privacy policy (section
  2, "The game's own definitions") covers using uploaded definitions for every player and for development
  (they can replace the dev plugin's manual dump as the source of `data/game_configs/`), with no consent
  flag: they are game data, not player data.
- The 428/409 `base` is the newest global generation by time learned or confirmed, or the caller's own pending
  set if that is newer. Another account's pending set is never offered.
- Limits: 2 pending sets per account (a new upload replaces the oldest), 3 unfinished uploads per account in
  memory (oldest dropped), 20,000 entries per upload, unfinished uploads forgotten after 5 minutes. Pending
  references go with the account (`DELETE /v1/account`, the expiry sweep); content nobody references is removed.
- `GET /v1/configs` answers `{baseline, generations, pending}` (global plus the caller's own; `pending` is the
  caller's); `GET /v1/configs/<fingerprint>` (decimal or `0x` hex) 404s for anything else.
- Operator: `configs list` (status, fingerprint, hash prefix, day, number of accounts, +/- quests and
  achievements against the base), `configs confirm <fp> [--hash H]` (needs `--hash` when different contents
  share the fingerprint; writes the set without state keys to `generations/<fp>/` and drops the pending
  references) and `configs drop <fp>`. A running server notices a confirmation at its next lookup (it compares
  the `generations/` mtime and the `config_confirmed` table), so no restart. Directories under `generations/`
  from before pending sets existed count as confirmed.
- The planner: a snapshot carries `configs`, the fingerprint it was decoded with. When the data package's
  fingerprint differs, `api_client` fetches `GET /v1/configs/0x<fp>` with the token that served the snapshot,
  checks that it hashes to `fp`, caches it under `state/api_configs/<fp>/`, and `apply_plugin_snapshot` expands
  the snapshot with it, so a game update needs no data package. The planner can only fetch a set its account
  can see: its own pending one or a global one.

The Lua and Python hashes must agree byte for byte: `test_config_hashes_match_between_lua_and_python` checks
every entry of the mock dump, and the dev plugin's "Dump configs" now also writes `config:hashes` so a beta
dump can be checked against the package dump's hashes. A post without a fingerprint (an older plugin) is
decoded with the package dump.

## Deploy (NixOS, behind nginx)

Two things the sketch below does not show (2026-11-13):

- **Socket activation.** `serve` takes over a listening socket passed by the service manager (systemd's
  `LISTEN_PID` / `LISTEN_FDS`, file descriptor 3; `export_api.inherited_socket`) and then ignores `--listen`.
  With a `.socket` unit for 127.0.0.1:8765, connections made while the service restarts wait in systemd's
  socket and the new process answers them, so a deploy gives no 502. Without the variables it binds `--listen`.
  (Binding before the slow start-up step, tried first, still left about a second of refused connections while
  Python started.)
- **The deletion log.** `--deletions DIR` (before the subcommand) or `RUNE_ADVISOR_API_DELETIONS`: a directory
  outside the data dir (so restoring an old data dir does not restore an old log) that is backed up like
  everything else. Each requested deletion (an account, a character, a revoked
  token) is also appended to `DIR/deletions.jsonl` as identifiers of ours only (random account ids, token
  hashes, a stored character's UUID: nothing that came from the plugin, so the log is not plugin data); the sweep applies the log again at
  every start and drops entries after 90 days (`DELETION_LOG_DAYS`, the age of the oldest backup), so a
  restored backup cannot bring back what was deleted since. A restore must not count on the live log having
  survived: before starting the service on restored data, run `--deletions DIR deletions merge FILE...` with
  the log copy from every backup newer than the one restored (and the live log, if it is still there under
  another path). Entries are added once each and applied at once. A deletion record therefore lives 90 days in
  the live log plus up to the backups' 90 days in the copies. It logs ` WARNING deletion log: N deletion(s) ...
  applied again` when it had to. Off when no place is given.

**Two backups, split by whose data it is** (the user, 2026-11-13). Jagex's terms for plugins (EULA Schedule 4
s6.5) want a player's data deleted from every copy within a month of a request, and character states are the
only plugin data in the store. They are also the one thing a restore can do without: a plugin whose state is
missing is told to send the full state and the character is back at its next post. So:
- the **full backup** (the database as it is, with states) is kept for a short time, under a month;
- the **accounts backup** is kept long (90 days): `export_api --data-dir D accounts-copy OUT.sqlite3` writes a
  copy without any character state, with read tokens limited to named characters turned into bare revoked
  hashes (a copy without the names could only make them unlimited; their owners make new ones), vacuumed. It
  only reads `D`, which may be the live data dir, and prints `key = value` lines and `accounts-copy: ok`
  (exit 0; 2 when there is no database). `check` passes on the copy (`players = 0`).
  `generations/` and the deletion log go with both. `settings.json` is in neither (2026-11-13): it is operator
  configuration, declared in the server's own config and applied at unit start with `settings set`, so a
  restore is deploy, restore the data, start. `check` and a start are correct without the file (the defaults
  apply; a test covers it). No setting is harmed by being set again at every start; `post_interval` is the
  one the operator may change by hand in an emergency, so it is left undeclared and falls back to its default
  after a restore.
A restore uses the newest full backup when one is recent enough, else the accounts backup; either way
`deletions merge` runs before the service starts. Later, history kept by the server falls under the short
backup too. The API's copy of a history stays the durable one; the copy a player's planner has synced is the
backup of last resort, spread over the users' own machines. It is never uploaded back into the API. What the
planner must get right when history is built: an API that answers with an empty or shorter history (a
restore) must not make the planner overwrite the richer one it holds.

**Data requests by hand** (an email to `privacy@`; expected to be rare). `export_api --data-dir D --deletions L
request ...`, with the same `--deletions` place as the service so the deletion reaches the log:
- `request find --player NAME` lists the accounts holding a character of that name (case-insensitive): account
  id, stored spelling, day of the last post, never the state. `request find --secret VALUE` says which account
  a key or token belongs to and what kind it is; `--account ID` shows that account's dates and counts.
- `request export --out FILE [--player NAME] [--account ID | --secret VALUE] [--yes]` writes what is stored
  to one JSON file (mode 0600), never to the terminal: the account's dates, its live tokens as the account page
  shows them (no token, no hash) and each character's snapshot; with `--player`, that character only. A state
  that cannot be decoded is handed over as its stored record. After sending the file to the person, delete it:
  keep the correspondence, not a second copy of the data.
- `request delete-player --player NAME [--account ID] [--yes]` and
  `request delete-account (--account ID | --secret KEY) [--yes]`. Without `--yes` these and `export` print
  what they would do. A name held by several accounts needs the account. `--secret -` reads the value from standard input
  (keeps it out of the shell history).
- `request delete --secret VALUE [--choice account|characters|none]` is the command for a key or token the
  person sent. It prints the account's characters (name, last post, which tokens posted for them) and tokens
  (first six characters, label, scopes, what they post for or may read), then suggests what the secret covers
  and asks `delete account / characters / none? [suggestion]` (Enter takes the suggestion; `a`, `c`, `n` or
  the word chooses; `--choice` answers in advance). The suggestion: the account key, the account; a plugin's
  token, the account when it posted for every character the account holds, else the characters it posted for
  (none if it posted for none); a read token, none (its "characters" are the ones it may read). The operator
  may always choose more than the suggestion. Deleting characters by a plugin's token leaves the token live
  and says so.
- `request find` with `--account` or `--secret` prints the same report of characters and tokens.
- `delete-player` and `delete-account` are the operator's own decision by name or account id; with a token as
  `--secret` they refuse and point at `request delete` (the account key is accepted). A character name alone
  proves nothing, anyone can type one, and names change hands in the game.
- Exit 0 done or shown, 1 nothing matched, 2 ambiguous or refused. Lines start `request: `.

The production service has run this way since 2026-11-13. Its real configuration is kept apart from this
repository, builds this package from a pinned commit and runs pytest as its check; the sketch below is the
shape, not a copy. In short: the service listens on localhost with no path prefix, nginx terminates TLS for the
API's own hostname and forwards everything to it:

```nix
systemd.services.rune-advisor-export-api = {
  wantedBy = [ "multi-user.target" ];
  serviceConfig = {
    ExecStart = "${runeAdvisor}/bin/python -m rune_advisor.export_api --data-dir /var/lib/rune-advisor serve --listen 127.0.0.1:8765 --prefix ''";
    StateDirectory = "rune-advisor";
    User = "rune-advisor";  # a static user in the real deployment, plus systemd sandboxing
    Restart = "on-failure";
  };
};
services.nginx.virtualHosts."api.runeadvisor.com" = {
  forceSSL = true; enableACME = true;
  locations."/" = {
    proxyPass = "http://127.0.0.1:8765";
    extraConfig = ''
      client_max_body_size 8m;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    '';
  };
};
```

`runeAdvisor` is this package with its dependencies (`cryptography` is imported by the server through the
data accessor; httpx, typer and rich are only needed by the planner; all are in nixpkgs; `buildPythonPackage`
from the repo, or uv2nix). `--prefix` defaults to `/export_api` (what a localhost test server and its plugin
manifest use); production passes an empty prefix. The data dir holds `export_api.sqlite3`, `settings.json`,
`generations/` and `pending_configs/`. A data dir from before SQLite (`tokens.json`, `players/`) is imported
when the service first starts on it: owners become accounts and their tokens read+submit tokens; the old
files stay for a rollback. Production never had such a dir (the first deployment's was empty); the import has
run on a scratch copy of the user's localhost dir, `state/export_api/`. Create a token with
`python -m rune_advisor.export_api --data-dir /var/lib/rune-advisor token new andrew`, or turn on
`trust_proxy` and let users make accounts themselves.

The service's log (the journal in production) has one line per request, `INFO 127.0.0.1 "GET
/v1/snapshot/<player>" 200`, with character names replaced by the literal `<player>` (`loggable_path`), so the
journal holds no names or client addresses; errors are ` ERROR unhandled error`, ` ERROR expiry sweep failed` and ` ERROR health: the store did not
answer`, each with a traceback.

**CORS** (2026-11-13): pages on one origin may call the API from a browser, `https://runeadvisor.com` by
default (`serve --cors-origin ORIGIN`, `''` for none): the site's account page, where a pasted account key
lists tokens and deletes data. The origin is compared as an exact string (no `www`, no wildcard). A preflight
`OPTIONS` from it answers 204 with `Access-Control-Allow-Methods: GET, POST, DELETE`, `-Allow-Headers:
Authorization, Content-Type, X-Player, X-Token-Id`, `-Max-Age: 600`; replies to it carry `Access-Control-Allow-Origin`; every reply
carries `Vary: Origin`; `OPTIONS` from anywhere else is 403. No credentials are allowed: tokens travel in the
Authorization header the page sets, so no other site can use a visitor's access. nginx must add no CORS
headers of its own.

**Verifying a data dir** (backups, restores): `python -m rune_advisor.export_api --data-dir D check` opens the
directory read-only and writes nothing (the database as an immutable file when no `-wal` lies beside it, as in
a `.backup` copy; through the WAL otherwise). It runs SQLite's integrity check, checks the tables, that live
tokens and states belong to accounts, that state records and `settings.json` parse, and that every pending and
confirmed config set is on disk and hashes to its name. Output: `key = value` counts (`accounts`, `players`,
`tokens`, `revoked_tokens` as in `stats`, then `generations`, `pending_sets`), a `problem: ...` line per
problem, and a last line `check: ok` or `check: FAILED (N problem(s))`. Exit 0 ok, 1 problems, 2 no database
there (or a usage error). A backup job can run it on the copy it is about to keep.

## First run against the real client (localhost, before deploying)

The unknowns are all client-side (HTTPRequest's behaviour on 409/428/401, body sizes, the token-check GET),
so run the server on this machine and point the beta plugin at it before touching the box:

1. `uv run python -m rune_advisor.export_api --data-dir /tmp/rune-advisor-api serve --listen 127.0.0.1:8765`
   and `... --data-dir /tmp/rune-advisor-api token new andrew` (the manifest allows plain HTTP URLs).
2. In the linked beta copy of `plugins/rune_advisor_export/plugin.json`, set `urls.api` to
   `http://127.0.0.1:8765/export_api` for the test (don't commit it), `plugins_refresh`, `pon rune_advisor_export`,
   paste the token in the panel: it should read "Token saved" after a couple of seconds.
3. Watch the server log for the full post (~25 KB) and the deltas, then `uv run radv pull YourName` with
   `RUNE_ADVISOR_API_URL=http://127.0.0.1:8765/export_api RUNE_ADVISOR_API_TOKEN=...` and compare with a fresh
   `scripts/archive_snapshot.py YourName --beta` (quests, tests, skills, inventories the client has loaded).
4. To exercise the 428 handshake, start the server with an older dump: copy `data/game_configs/` to a scratch
   directory, delete one achievement from `achievements.json.gz`, and run `serve --configs <scratch>`; the
   plugin should upload that one entry and the server log "configs ... learned".
5. Check the login walk stays under the tick budget (no "callback exceeded" errors in the console) now that
   it also hashes every config.

## Checks

- `uv run pytest -k export_api`: the mock posts through the service and over real HTTP; the Lua side
  `plugins/rune_advisor_export/tests/run_http.lua` (settings layer, bearer header, 409/428/401/timeout handling)
  and `run_wire.lua` (the protocol against `mock.fakeServer()`, replayed by `rune_advisor.wire`).
- First real run (2026-11-01, beta client against a localhost server): token check, full post and deltas
  accepted; the derived snapshot agreed with the plugin's archived API-read snapshot on every quest, every
  achievement completion, all 8,869 open achievement tests and every skill's xp. The client crashes along
  the way were all one thing: handing a client API function to `pcall` as a value (call it inside a closure instead).
- `scripts/check_wire.py` replays posts recorded by the old local stand-in (archives before 2026-11-01 only).
