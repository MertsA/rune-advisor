"""The export API: the HTTP service the published exporter plugin posts its state to (docs/export-api.md).

    python -m rune_advisor.export_api serve --data-dir /var/lib/rune-advisor [--listen 127.0.0.1:8765] [--prefix /export_api]
    python -m rune_advisor.export_api token new <owner> [--scopes submit,read] [--label L] [--characters A,B]
                                                          # prints the token once; the store keeps its SHA-256
    python -m rune_advisor.export_api token add <owner> <token>  # register a token minted elsewhere (mirror a deployment)
    python -m rune_advisor.export_api token list | revoke <token-or-prefix>
    python -m rune_advisor.export_api account list | key <account> | delete <account>
    python -m rune_advisor.export_api configs list | confirm <fp> [--hash H] | drop <fp>   # config sets clients uploaded
    python -m rune_advisor.export_api sweep | migrate        # expire inactive accounts now / import a pre-SQLite data dir
    python -m rune_advisor.export_api settings show | set post_interval 5 | set retry_after 300
    python -m rune_advisor.export_api stats [--minutes 10]   # characters that posted recently, to pick an interval
    python -m rune_advisor.export_api check                  # verify a data dir read-only (backups): exit 0 / 1 / 2

Load control: every reply carries `interval`, the seconds the plugin should wait between posts (settings.json
`post_interval`, default 10, changeable while the service runs: a few users can post every 5 s, thousands every
5 min). A refused token answers 401 with `token`: "revoked" (the plugin forgets it and stops posting) or
"unknown" plus `retry_after` seconds (settings `retry_after`, default 300). Revoked tokens stay in the store
marked `revoked` (as bare hashes for 90 days), so the server can tell the two apart.

Accounts: an account is a random key (`raa_...`); the store keeps its SHA-256 only. `POST /v1/accounts` makes one
(per-IP limit in memory, Turnstile when `serve` is given its secret in a file) and the key manages the account's tokens. A
token has scopes: `submit` (the plugin) or `read` (the planner), optionally limited to named characters.

The plugin posts wire-protocol messages (plugins/rune_advisor_export/lib/wire.lua): a full state once, then deltas
against the last acknowledged state, each with the layout hash and the state hashes before and after. The
server keeps one state per (token owner, character), plays the same hash checks as `rune_advisor.wire.replay`
(a mismatch answers 409 and the plugin resends the full state) and, on request, turns the state into the
schema-2 snapshot the planner reads (quests and achievement tests derived from the var values with the game
configs, inventories named from data/item_names.json).

Config sync: a full post names the fingerprint of the quest/achievement configs its layout came from. The
server knows the package dump's set and every set uploaded since (<data-dir>/generations/<fingerprint>/). An
unknown fingerprint answers 428 with the hashes of every config the server knows (`known`) and the fingerprint
that map describes (`base`); the plugin replies to POST /v1/configs with only the difference (`added` entries,
`removed` ids, in `part`/`parts`), the server composes base + difference, accepts it only if it hashes to the
claimed fingerprint, and the plugin resends the full state. So a game update never needs a plugin or server
release: the first client to see it teaches the server.

Routes (under the prefix nginx forwards, e.g. /export_api; the prefix may also be stripped upstream):
    POST   /v1/state                  submit token: the plugin's post (JSON; Authorization: Bearer <token>)
    POST   /v1/configs                submit token: a part of the config difference (see above)
    GET    /v1/configs                any token: fingerprints the server knows
    GET    /v1/configs/<fingerprint>  any token: that generation's configs ({quests: [...], achievements: [...]})
    GET    /v1/players                submit or read token: characters it may see
    GET    /v1/snapshot               read token: the planner's snapshot for the character named in X-Player
    GET    /v1/state                  read token: the raw stored state (hash, seq, layouts, base64 canonical bytes)
    POST   /v1/accounts               no auth: {captcha} -> 201 {account, key}, the key shown once
    GET    /v1/account                account key: id, dates, counts, quotas
    DELETE /v1/account                account key: delete the account, its tokens and every character's state
    GET    /v1/tokens                 account key: the live tokens (id, prefix, label, scopes, characters, dates)
    POST   /v1/tokens                 account key: {label, scopes, characters} -> 201 with the token, shown once
    DELETE /v1/tokens                 account key: revoke the token whose id is in X-Token-Id
    DELETE /v1/players                account key: delete the state of the character named in X-Player
    GET    /v1/posted                 submit token: the characters this token has posted for
    DELETE /v1/posted                 submit token: delete those characters and revoke the token
    DELETE /v1/tokens/posted          account key: the same for the account's token named in X-Token-Id
    GET    /v1/consent                account key or submit token: {consent, consent_since} for the character in X-Player
    POST   /v1/consent                the same: {consent: true|false} sets it (see "Consent" below)
    GET    /v1/health                 no auth: {"ok": true}
Nothing that says whose request it is travels in a URL, so no access log in front of the service records it:
a character's name goes in the `X-Player` request header (percent-encoded UTF-8), a token's id in
`X-Token-Id`, keys and tokens in `Authorization`, everything else in the body. A name or an id in the path is
404, and a request with a query string is refused (400): no route takes one, and refusing it means a client
built to put something there fails the first time it is tried. The one path parameter left is a config
fingerprint (`/v1/configs/<fingerprint>`), which names a version of the game, not a player.

Consent: a stored character carries its player's answer to one question, whether its data may be analysed to
improve the planner for everyone (off until given; the day it was given is kept, on the character's row, so it
is deleted with the character). It is changed with the account key for any character of the account, or with a
submit token for the characters that token has posted for (the plugin's settings, or the account page opened
with the plugin's token). Every accepted post's reply carries the character's `consent`, so a change made on
the site shows in the plugin at its next post. Consent changes nothing about what is collected or stored. It
is read when an analysis runs, never recorded on the data: `Store.analysis_states` is the one gate any analysis
goes through, and it hands out states without the character's name or its account. So withdrawing excludes
what is stored from every later analysis and giving it includes it. Changes are noted in the deletion log, so
a restore from a backup cannot bring back a consent that was withdrawn since.

Storage: <data-dir>/export_api.sqlite3 (accounts, tokens, states; WAL) plus settings.json and generations/.
One process, one connection, a re-entrant lock around every change. Standard library only, so the deploy is
this package and a systemd unit. A data dir from before SQLite (tokens.json, players/) is imported once when
the database is first created (`migrate` repeats it by hand); the old files are left alone.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import os
import socket
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from . import wire
from .sources.game_configs import PROGRESS_COMPLETE, GameConfigs, _as_list, derive, in_grace
from .sources.plugin_db import SKILLTYPE_TO_NAME
from .xp import level_at_xp, level_cap

log = logging.getLogger("rune_advisor.export_api")

MAX_BODY_BYTES = 8 << 20  # a full post for a high-level character is ~25 KB; a config part can be a few MB on a big update
# Live-reloaded numbers (settings.json): load control, per-account quotas, inactivity expiry, and the guards on
# account creation (accounts per IP per window; trust_proxy 1 reads the client address from X-Forwarded-For).
DEFAULT_SETTINGS = {"post_interval": 10.0, "retry_after": 300.0, "max_characters": 10.0, "max_tokens": 20.0,
                    "retention_days": 365.0, "account_ip_limit": 5.0, "account_ip_window": 86400.0, "trust_proxy": 0.0,
                    # Under load the server lengthens the interval it hands out, up to auto_interval_max seconds
                    # (auto_interval 0 = never: only post_interval counts). See `Load`.
                    "auto_interval": 1.0, "auto_interval_max": 300.0}
ON_OFF_SETTINGS = ("trust_proxy", "auto_interval")
# The Turnstile secret is not a setting: `serve` reads it from a file the service manager provides (a systemd
# credential), so it is never on a command line, in settings.json, in a backup or in any output.
TURNSTILE_CREDENTIAL = "turnstile_secret"  # the credential's name under $CREDENTIALS_DIRECTORY
TURNSTILE_FILE_ENV = "RUNE_ADVISOR_API_TURNSTILE_SECRET_FILE"  # or a path given explicitly (--turnstile-secret-file)
SCOPES = ("submit", "read")
ACCOUNT_KEY_PREFIX = "raa_"
MAX_LABEL, MAX_LIMITED_CHARACTERS = 60, 50
MIN_INTERVAL, MAX_INTERVAL = 3.0, 3600.0  # the plugin clamps to the same range
SCHEMA_VERSION = 2
PROGRESS_GRACE, PROGRESS_READY = -3, 0
# Config sets learned from uploads stay private to the uploading account until the operator confirms them.
MAX_PENDING_PER_ACCOUNT = 2  # a new upload beyond this replaces the account's oldest pending set
MAX_STAGED_PER_ACCOUNT = 3  # unfinished (multi-part) uploads held in memory per account
MAX_STAGED_ENTRIES = 20000  # config entries one unfinished upload may hold
STAGING_SECONDS = 300.0  # an unfinished upload is forgotten after this long
# A full post's own lists: bounds on what it may name, and how many built layouts are kept in memory (they are
# rebuilt when needed again). The hand-listed fields are not among them: the post names a manifest by hash.
MAX_FIELDS = 4096  # names in a 0.4.x post's `named` list
MAX_LIST_NAMES = 255  # skills, and inventories (a delta names an inventory in one byte)
LIST_NAME = re.compile(r"[A-Za-z0-9_]{1,100}")
LAYOUT_CACHE = 64


class ApiError(Exception):
    def __init__(self, status: int, reason: str, token: str | None = None, extra: dict[str, Any] | None = None):
        super().__init__(reason)
        self.status, self.reason, self.token, self.extra = status, reason, token, extra or {}


# --- Item names ----------------------------------------------------------------------------------------------

@dataclass
class ItemNames:
    by_id: dict[int, dict[str, str]] = field(default_factory=dict)
    stamp: float = 0.0  # the file's mtime when loaded (0: not from the file)

    @staticmethod
    def _stamp() -> float:
        try:
            return Path(str(importlib_resources.files("rune_advisor.data").joinpath("item_names.json"))).stat().st_mtime
        except OSError:
            return 0.0

    @classmethod
    def load(cls) -> "ItemNames":
        try:
            raw = json.loads(importlib_resources.files("rune_advisor.data").joinpath("item_names.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        return cls({int(k): v for k, v in raw.get("items", {}).items()}, cls._stamp())

    def fresh(self) -> "ItemNames":
        """These names, or the file's when it changed since they were loaded: `scripts/build_item_names.py`
        names new ids while the service runs, and a snapshot should show them without a restart."""
        return type(self).load() if self.stamp and self._stamp() != self.stamp else self

    def name(self, item_id: int) -> str | None:
        entry = self.by_id.get(item_id)
        return entry["name"] if entry else None

    def config(self, item_id: int) -> str | None:
        entry = self.by_id.get(item_id)
        return entry.get("config") or None if entry else None

    def by_config(self) -> dict[str, str]:
        return {v["config"]: v["name"] for v in self.by_id.values() if v.get("config")}


# --- Storage -------------------------------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def _today() -> str:
    """Today as a UTC date: tokens and accounts keep day precision only, so the store says little about habits."""
    return datetime.now(timezone.utc).date().isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, key_hash TEXT UNIQUE, created TEXT NOT NULL, last_active TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, account TEXT NOT NULL, prefix TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '', scopes TEXT NOT NULL, characters TEXT, created TEXT NOT NULL,
    last_used TEXT, revoked TEXT);
CREATE INDEX IF NOT EXISTS tokens_account ON tokens (account);
CREATE TABLE IF NOT EXISTS states (
    account TEXT NOT NULL, player TEXT NOT NULL, updated REAL, seq INTEGER, posts INTEGER, record TEXT NOT NULL,
    id TEXT, consent TEXT, PRIMARY KEY (account, player));
CREATE TABLE IF NOT EXISTS token_posts (
    token TEXT NOT NULL, character TEXT NOT NULL, last_post TEXT NOT NULL, PRIMARY KEY (token, character));
CREATE TABLE IF NOT EXISTS config_contents (
    hash TEXT PRIMARY KEY, fp INTEGER NOT NULL, base INTEGER NOT NULL, created TEXT NOT NULL,
    q_added INTEGER NOT NULL, q_removed INTEGER NOT NULL, a_added INTEGER NOT NULL, a_removed INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS config_contents_fp ON config_contents (fp);
CREATE TABLE IF NOT EXISTS config_refs (
    account TEXT NOT NULL, hash TEXT NOT NULL, uploaded REAL NOT NULL, PRIMARY KEY (account, hash));
CREATE INDEX IF NOT EXISTS config_refs_hash ON config_refs (hash);
CREATE TABLE IF NOT EXISTS config_confirmed (
    fp INTEGER PRIMARY KEY, hash TEXT NOT NULL, base INTEGER NOT NULL, confirmed REAL NOT NULL,
    q_added INTEGER NOT NULL, q_removed INTEGER NOT NULL, a_added INTEGER NOT NULL, a_removed INTEGER NOT NULL);
"""
TOMBSTONE_DAYS = 90  # a revoked token's hash is kept this long so a plugin still using it is told "revoked"
# What a revoked token's row is reduced to: the hash and the day it was revoked, nothing about whose it was or how
# it was used (the row id is random). Applied on revoking, on deleting the account, and to older rows by the sweep.
# Deletions must outlive a restore: a backup taken before a deletion brings the deleted data back. So each
# requested deletion (an account, one character, a revoked token) is also noted in a small log in a directory of
# its own, outside the data dir (`--deletions DIR` / RUNE_ADVISOR_API_DELETIONS), so that putting an old data dir
# back does not put an old log back, and the sweep applies the log again: after a restore, whatever was deleted
# since the backup is deleted once more. The log is backed up like everything else. A restore cannot count on
# the live log having survived, so it merges every copy it can find (`deletions merge FILE...`: the live one if
# it is still there, and the copies in every backup newer than the one restored) before the service starts.
# The log holds identifiers of ours only: random account ids, token hashes, and a stored character's random id
# (a UUID given when its state is first saved). Nothing in it comes from the plugin: no name, no hash of one. Entries are dropped from the live log after DELETION_LOG_DAYS, the age of the oldest
# backup that could hold the deleted data; copies of the log inside backups live as long as those backups do.
DELETIONS_ENV = "RUNE_ADVISOR_API_DELETIONS"
DELETION_LOG_DAYS = 90
BARE = "account = '', prefix = '', label = '', scopes = '', characters = NULL, last_used = NULL"
PLAYER_HEADER = "X-Player"  # the character a request is about, percent-encoded: kept out of URLs and so out of logs
TOKEN_ID_HEADER = "X-Token-Id"  # the token (by its id, not its value) an account-key request is about: likewise
CORS_ORIGIN = "https://runeadvisor.com"  # the site's account page; `serve --cors-origin` changes it, '' turns CORS off


@dataclass(frozen=True)
class TokenInfo:
    """What a valid token may do: its account, scopes, and (for reading) the characters it is limited to."""
    id: str
    account: str
    scopes: frozenset[str]
    characters: tuple[str, ...] | None = None  # None: every character of the account

    def can_read(self, player: str) -> bool:
        return "read" in self.scopes and (self.characters is None or player.lower() in {c.lower() for c in self.characters})


class Store:
    """Accounts, tokens, settings and per-character states: SQLite in <data-dir>/export_api.sqlite3 plus
    settings.json (re-read live) and the config generations on disk. One connection under one re-entrant lock:
    the service holds the lock across a whole post, the store methods take it again."""

    def __init__(self, data_dir: Path, deletions: Path | None = None):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        # The deletion log lives outside the data dir on purpose (see `_note_deletion`); off when no place is given.
        place = deletions or (Path(os.environ[DELETIONS_ENV]) if os.environ.get(DELETIONS_ENV) else None)
        self.deletions_file = place / "deletions.jsonl" if place else None
        self.lock = threading.RLock()
        self._settings_cache: tuple[float, dict[str, float], dict[str, Any]] | None = None
        self._posted_today: dict[tuple[str, str, str], str] = {}  # (token, account, player) -> day noted (`note_post`)
        fresh = not self.db_path.exists()
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        # WAL with synchronous=NORMAL: no fsync per post. A power cut can lose the last commits but cannot corrupt
        # the file, and a lost post only means the plugin's next delta gets a 409 and it sends the full state.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        # Every stored character has a random id of ours (a UUID), so that records about it (the deletion log)
        # need nothing derived from its name. Stores from before the column get one per row here.
        if "id" not in {r["name"] for r in self.db.execute("PRAGMA table_info(states)")}:
            self.db.execute("ALTER TABLE states ADD COLUMN id TEXT")
        for r in self.db.execute("SELECT account, player FROM states WHERE id IS NULL").fetchall():
            self.db.execute("UPDATE states SET id = ? WHERE account = ? AND player = ?", (str(uuid.uuid4()), r["account"], r["player"]))
        # Stores from before consent (2026-11-14): every stored character starts without it.
        if "consent" not in {r["name"] for r in self.db.execute("PRAGMA table_info(states)")}:
            self.db.execute("ALTER TABLE states ADD COLUMN consent TEXT")
        if fresh and self.has_legacy_files():  # a data dir from before SQLite: carry it over once
            self.migrate_legacy()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "export_api.sqlite3"

    def close(self) -> None:
        self.db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self.db.execute(sql, args).rowcount

    @contextmanager
    def _tx(self) -> Iterator[None]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    # Settings (re-read whenever the file changes, so `settings set` takes effect without a restart)
    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def _settings(self) -> tuple[dict[str, float], dict[str, Any]]:
        try:
            mtime = self.settings_path.stat().st_mtime
        except FileNotFoundError:
            return dict(DEFAULT_SETTINGS), {}
        if self._settings_cache is None or self._settings_cache[0] != mtime:
            try:
                raw = json.loads(self.settings_path.read_text())
            except (json.JSONDecodeError, OSError):
                raw = {}
            merged = dict(DEFAULT_SETTINGS)
            for key in DEFAULT_SETTINGS:
                if isinstance(raw.get(key), (int, float)) and not isinstance(raw.get(key), bool):
                    merged[key] = float(raw[key])
            merged["post_interval"] = min(MAX_INTERVAL, max(MIN_INTERVAL, merged["post_interval"]))
            self._settings_cache = (mtime, merged, raw)
        return dict(self._settings_cache[1]), self._settings_cache[2]

    def settings(self) -> dict[str, float]:
        return self._settings()[0]

    def set_setting(self, key: str, value: float) -> dict[str, float]:
        if key == TURNSTILE_CREDENTIAL:
            raise ValueError(f"{key} is not a setting: give `serve` the secret in a file (the systemd credential "
                             f"'{TURNSTILE_CREDENTIAL}', --turnstile-secret-file or ${TURNSTILE_FILE_ENV}), then restart")
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"unknown setting {key!r} (one of {', '.join(DEFAULT_SETTINGS)})")
        if isinstance(value, str):
            raise ValueError(f"{key} is a number")
        if key == "post_interval" and not MIN_INTERVAL <= value <= MAX_INTERVAL:
            raise ValueError(f"post_interval must be {MIN_INTERVAL:g}..{MAX_INTERVAL:g} seconds")
        if key in ON_OFF_SETTINGS and value not in (0, 1):
            raise ValueError(f"{key} is 0 or 1")
        if value <= 0 and key not in ON_OFF_SETTINGS:
            raise ValueError("must be positive")
        return self._write_settings(key, float(value))

    def unset_setting(self, key: str) -> dict[str, float]:
        """Take a setting out of the file, so it follows the default again (also after a later release changes it)."""
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"unknown setting {key!r} (one of {', '.join(DEFAULT_SETTINGS)})")
        return self._write_settings(key, None)

    def _write_settings(self, key: str, value: float | None) -> dict[str, float]:
        """The file holds only what was set, never a copy of the defaults: a default changed by a release must
        reach a server that never set that value (the production server kept retention_days 180 on 2026-11-13
        because the file had every value in it). Anything else an older file held (a Turnstile secret from when that was a
        setting, unknown keys) is dropped at the first change."""
        with self.lock:
            _, raw = self._settings()
            chosen = {k: float(raw[k]) for k in DEFAULT_SETTINGS
                      if isinstance(raw.get(k), (int, float)) and not isinstance(raw.get(k), bool)}
            if value is None:
                chosen.pop(key, None)
            else:
                chosen[key] = value
            _write_atomic(self.settings_path, json.dumps(chosen, indent=2))
            self._settings_cache = None
            return self.settings()

    def chosen_settings(self) -> set[str]:
        """The settings the file sets (the rest follow the defaults)."""
        raw = self._settings()[1]
        return {k for k in DEFAULT_SETTINGS if isinstance(raw.get(k), (int, float)) and not isinstance(raw.get(k), bool)}

    def note_turnstile(self, source: str) -> None:
        """Record where the running service got its Turnstile secret from ("" = it has none), for `settings
        show`, which runs as another process and cannot see the service's credential. Never the value."""
        self._x("INSERT OR REPLACE INTO meta (key, value) VALUES ('turnstile_source', ?)", (source,))

    def settings_lines(self) -> dict[str, str]:
        """Every setting for display; text settings only say whether they are set."""
        lines = {k: f"{v:g}" for k, v in self.settings().items()}
        rows = self._q("SELECT value FROM meta WHERE key = 'turnstile_source'")
        source = rows[0]["value"] if rows else ""
        lines[TURNSTILE_CREDENTIAL] = f"set (from {source}, read when the service last started)" if source else "unset"
        return lines

    # Accounts
    def ensure_account(self, account: str) -> None:
        """An account without a key: what `token new <owner>` and the migration make, managed from the CLI."""
        today = _today()
        self._x("INSERT OR IGNORE INTO accounts (id, key_hash, created, last_active) VALUES (?, NULL, ?, ?)",
                (account, today, today))

    def create_account(self) -> tuple[str, str]:
        """A new account: (id, key). Only the key's SHA-256 is stored, so the key is shown once."""
        account, key = secrets.token_hex(8), ACCOUNT_KEY_PREFIX + secrets.token_urlsafe(32)
        today = _today()
        self._x("INSERT INTO accounts (id, key_hash, created, last_active) VALUES (?, ?, ?, ?)",
                (account, _token_hash(key), today, today))
        return account, key

    def set_account_key(self, account: str) -> str:
        """Issue (or replace) the account's key: for accounts made by `token new` or the migration."""
        self.ensure_account(account)
        key = ACCOUNT_KEY_PREFIX + secrets.token_urlsafe(32)
        self._x("UPDATE accounts SET key_hash = ? WHERE id = ?", (_token_hash(key), account))
        return key

    def account_by_key(self, key: str) -> str | None:
        rows = self._q("SELECT id FROM accounts WHERE key_hash = ?", (_token_hash(key),))
        return rows[0]["id"] if rows else None

    def account_info(self, account: str) -> dict[str, Any] | None:
        rows = self._q("SELECT id, created, last_active FROM accounts WHERE id = ?", (account,))
        if not rows:
            return None
        return {"id": account, "created": rows[0]["created"], "last_active": rows[0]["last_active"],
                "characters": self.count_states(account), "tokens": len(self.tokens_of(account))}

    def accounts(self) -> list[dict[str, Any]]:
        return [dict(self.account_info(r["id"]) or {}, has_key=r["key_hash"] is not None)
                for r in self._q("SELECT id, key_hash FROM accounts ORDER BY created, id")]

    # The deletion log (see DELETIONS_ENV above)
    def _note_deletion(self, **entry: str) -> None:
        if self.deletions_file is None:
            return
        try:
            self.deletions_file.parent.mkdir(parents=True, exist_ok=True)
            with self.lock, open(self.deletions_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"day": _today(), **entry}) + "\n")
        except OSError:
            log.exception("could not note a deletion in %s", self.deletions_file)  # the deletion itself stands

    def merge_deletions(self, files: list[Path]) -> int:
        """Add the entries of other copies of the deletion log (from backups newer than the data being restored)
        to this one, each entry once. Returns how many were new. The next sweep applies them."""
        if self.deletions_file is None:
            raise ValueError("no deletion log place is set (--deletions)")
        with self.lock:
            have = self.deletions_file.read_text(encoding="utf-8").splitlines() if self.deletions_file.exists() else []
            seen, added = set(have), 0
            for file in files:
                for line in file.read_text(encoding="utf-8").splitlines():
                    try:
                        valid = isinstance(json.loads(line), dict)
                    except ValueError:
                        valid = False
                    if valid and line not in seen:
                        seen.add(line)
                        have.append(line)
                        added += 1
            if added:
                self.deletions_file.parent.mkdir(parents=True, exist_ok=True)
                _write_atomic(self.deletions_file, "".join(line + "\n" for line in have))
        return added

    def reapply_deletions(self, today: date | None = None) -> int:
        """Apply the deletion log to the store again and drop entries older than DELETION_LOG_DAYS. Returns how
        many deletions had to be redone: more than 0 means the store was restored from an older backup."""
        if self.deletions_file is None or not self.deletions_file.exists():
            return 0
        today = today or datetime.now(timezone.utc).date()
        cutoff = (today - timedelta(days=DELETION_LOG_DAYS)).isoformat()
        kept: list[str] = []
        answers: dict[str, str | None] = {}  # stored character id -> its last logged consent (a day, or None)
        redone = 0
        with self.lock:
            lines = self.deletions_file.read_text(encoding="utf-8").splitlines()
            for line in lines:
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict) or str(entry.get("day", "")) < cutoff:
                        continue
                except ValueError:
                    continue
                kept.append(line)
                if "token" in entry:
                    redone += self._x(f"UPDATE tokens SET revoked = ?, created = ?, {BARE} WHERE hash = ? AND revoked IS NULL",
                                      (entry["day"], entry["day"], entry["token"]))
                elif "consent" in entry:  # a change of consent: only the character's last one counts (below)
                    answers[entry["consent"]] = entry.get("value")
                elif "character" in entry:
                    redone += self._x("DELETE FROM states WHERE id = ?", (entry["character"],))
                elif "account" in entry and self._q("SELECT 1 FROM accounts WHERE id = ?", (entry["account"],)):
                    self.delete_account(entry["account"], note=False)
                    redone += 1
            for character, value in answers.items():
                redone += self._x("UPDATE states SET consent = ? WHERE id = ? AND consent IS NOT ?", (value, character, value))
            if len(kept) != len(lines):
                _write_atomic(self.deletions_file, "".join(line + "\n" for line in kept))
        if redone:
            log.warning("deletion log: %d deletion(s) made after this data was saved were applied again "
                        "(the store was restored from a backup)", redone)
        return redone

    def delete_account(self, account: str, note: bool = True) -> dict[str, int]:
        """Remove the account and every character's state. Its tokens stay as bare revoked hashes (`BARE`: no
        account, prefix, label, scopes, characters or dates of use) for TOMBSTONE_DAYS, so a plugin still holding
        one is told to forget it and stops posting. `stats` counts them as `revoked_tokens`."""
        with self._tx():
            characters = self._x("DELETE FROM states WHERE account = ?", (account,))
            self._x("DELETE FROM config_refs WHERE account = ?", (account,))
            self._gc_pending()
            tokens = len(self.tokens_of(account))
            today = _today()
            self._x(f"UPDATE tokens SET revoked = COALESCE(revoked, ?), created = COALESCE(revoked, ?), {BARE} "
                    "WHERE account = ?", (today, today, account))
            self._x("DELETE FROM accounts WHERE id = ?", (account,))
            self._gc_token_posts()
        if note:
            self._note_deletion(account=account)
        return {"characters": characters, "tokens": tokens}

    def sweep(self, today: date | None = None, retention_days: float | None = None) -> list[str]:
        """Delete accounts with no post and no read for `retention_days` (settings, default 365), and forget
        revoked token hashes older than TOMBSTONE_DAYS. Returns the deleted account ids."""
        today = today or datetime.now(timezone.utc).date()
        self.reapply_deletions(today)
        days = retention_days if retention_days is not None else self.settings()["retention_days"]
        cutoff = (today - timedelta(days=days)).isoformat()
        with self.lock:
            stale = [r["id"] for r in self._q("SELECT id FROM accounts WHERE last_active < ?", (cutoff,))]
            for account in stale:
                self.delete_account(account, note=False)  # a restored copy expires again by itself
            self._x("DELETE FROM tokens WHERE revoked IS NOT NULL AND revoked < ?",
                    ((today - timedelta(days=TOMBSTONE_DAYS)).isoformat(),))
            self._gc_token_posts()  # also after the deletion log was applied again above
            # Rows revoked before tombstones were stripped this far (2026-11-13) kept a prefix, scopes and dates.
            self._x(f"UPDATE tokens SET created = revoked, {BARE} WHERE revoked IS NOT NULL AND "
                    "(account != '' OR prefix != '' OR label != '' OR scopes != '' OR characters IS NOT NULL "
                    "OR last_used IS NOT NULL OR created != revoked)")
        if stale:
            log.info("expired %d inactive account(s)", len(stale))
        return stale

    # Tokens
    def _insert_token(self, account: str, token: str, label: str, scopes: Iterable[str],
                      characters: Iterable[str] | None, replace: bool = False) -> str:
        self.ensure_account(account)
        sql = ("INSERT INTO tokens (id, hash, account, prefix, label, scopes, characters, created) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
        if replace:  # re-adding a value un-revokes it and reassigns it
            sql += (" ON CONFLICT(hash) DO UPDATE SET account = excluded.account, label = excluded.label, "
                    "prefix = excluded.prefix, created = excluded.created, last_used = NULL, "
                    "scopes = excluded.scopes, characters = excluded.characters, revoked = NULL")
        for _ in range(5):
            try:
                self._x(sql, (secrets.token_hex(4), _token_hash(token), account, token[:6], label, ",".join(sorted(scopes)),
                              None if characters is None else json.dumps(list(characters)), _today()))
                return self._q("SELECT id FROM tokens WHERE hash = ?", (_token_hash(token),))[0]["id"]
            except sqlite3.IntegrityError:  # an id collision (the hash is unique by construction); try another
                continue
        raise RuntimeError("could not allocate a token id")

    def create_token(self, account: str, label: str, scopes: Iterable[str], characters: Iterable[str] | None) -> tuple[str, str]:
        """Mint a token for an account: (id, token). The token is shown once; the store keeps its SHA-256."""
        token = secrets.token_urlsafe(32)
        return self._insert_token(account, token, label, scopes, characters), token

    def new_token(self, owner: str, label: str = "", scopes: Iterable[str] = SCOPES,
                  characters: Iterable[str] | None = None) -> str:
        """Admin CLI: a token for account `owner` (made when missing); both scopes unless asked otherwise."""
        return self.create_token(owner, label, scopes, characters)[1]

    def add_token(self, owner: str, token: str, label: str = "", scopes: Iterable[str] = SCOPES) -> None:
        """Register an existing token value for `owner`: one minted by another deployment, so a plugin that
        already holds it can post here too (the localhost server mirrors the public one's token). Re-adding a
        revoked token un-revokes it."""
        if len(token) < 16:
            raise ValueError("a token must be at least 16 characters")
        self._insert_token(owner, token, label, scopes, None, replace=True)

    def lookup(self, token: str) -> tuple[TokenInfo | None, str]:
        """(info, status): status "ok", "revoked" (the row is kept so the plugin can be told) or "unknown"."""
        rows = self._q("SELECT * FROM tokens WHERE hash = ?", (_token_hash(token),))
        if not rows:
            return None, "unknown"
        row = rows[0]
        if row["revoked"]:
            return None, "revoked"
        characters = json.loads(row["characters"]) if row["characters"] else None
        return TokenInfo(row["id"], row["account"], frozenset(row["scopes"].split(",")),
                         tuple(characters) if characters is not None else None), "ok"

    def owner(self, token: str) -> tuple[str | None, str]:
        info, status = self.lookup(token)
        return (info.account if info else None), status

    def touch(self, info: TokenInfo) -> None:
        """Note a use of a token: its last-used day and the account's last activity (what the expiry sweep reads)."""
        today = _today()
        self._x("UPDATE tokens SET last_used = ? WHERE id = ? AND (last_used IS NULL OR last_used != ?)",
                (today, info.id, today))
        self._x("UPDATE accounts SET last_active = ? WHERE id = ? AND last_active != ?", (today, info.account, today))

    def revoke(self, token_or_prefix: str) -> int:
        """Mark a token revoked: its bare hash is kept, so a plugin still using it is told to forget it."""
        today = _today()
        with self.lock:
            where, args = "revoked IS NULL AND (hash = ? OR (prefix != '' AND prefix = ?))", (_token_hash(token_or_prefix), token_or_prefix[:6])
            hashes = [r["hash"] for r in self._q(f"SELECT hash FROM tokens WHERE {where}", args)]
            count = self._x(f"UPDATE tokens SET revoked = ?, created = ?, {BARE} WHERE {where}", (today, today, *args))
            self._gc_token_posts()
        for h in hashes:
            self._note_deletion(token=h)
        return count

    def revoke_id(self, account: str, token_id: str) -> bool:
        today = _today()
        with self.lock:
            rows = self._q("SELECT hash FROM tokens WHERE id = ? AND account = ? AND revoked IS NULL", (token_id, account))
            done = self._x(f"UPDATE tokens SET revoked = ?, created = ?, {BARE} WHERE id = ? AND account = ? AND revoked IS NULL",
                           (today, today, token_id, account)) > 0
            self._gc_token_posts()
        if done and rows:
            self._note_deletion(token=rows[0]["hash"])
        return done

    @staticmethod
    def _token_row(r: sqlite3.Row) -> dict[str, Any]:
        out = {"id": r["id"], "prefix": r["prefix"], "owner": r["account"], "label": r["label"],
               "scopes": r["scopes"].split(",") if r["scopes"] else [],
               "characters": json.loads(r["characters"]) if r["characters"] else None,
               "created": r["created"], "last_used": r["last_used"]}
        if r["revoked"]:
            out["revoked"] = r["revoked"]
        return out

    def tokens_of(self, account: str) -> list[dict[str, Any]]:
        """The account's live tokens, each with the characters it has posted for (`posted`: name and day)."""
        posted: dict[str, list[dict[str, str]]] = {}
        for r in self._q("SELECT tp.token, s.player, tp.last_post FROM token_posts tp JOIN states s ON s.id = tp.character "
                         "WHERE s.account = ? ORDER BY s.player", (account,)):
            posted.setdefault(r["token"], []).append({"player": r["player"], "last_post": r["last_post"]})
        return [{**self._token_row(r), "posted": posted.get(r["id"], [])} for r in self._q(
            "SELECT * FROM tokens WHERE account = ? AND revoked IS NULL ORDER BY created, id", (account,))]

    # Which token posts for which character: so the owner of several tokens can tell which plugin is which
    # before revoking one. The link is between two ids of ours (the token's and the stored character's), with
    # the day of the last post; it goes when either end does.
    def note_post(self, token_id: str, account: str, player: str) -> None:
        today = _today()
        seen = (token_id, account, player)
        if self._posted_today.get(seen) == today:
            return  # once a day per token and character is enough: posts come every few seconds
        with self.lock:
            rows = self._q("SELECT id FROM states WHERE account = ? AND player = ?", (account, player))
            if rows:
                self._x("INSERT INTO token_posts (token, character, last_post) VALUES (?, ?, ?) "
                        "ON CONFLICT(token, character) DO UPDATE SET last_post = excluded.last_post",
                        (token_id, rows[0]["id"], today))
                self._posted_today[seen] = today

    def posted_by(self, token_id: str) -> list[dict[str, Any]]:
        """The stored characters a live token has posted for: [{id, player, last_post}].

        This is what a token proves control of, so it is followed by id and never by name. A token and the
        characters it posted are in one account, and the join requires it: a character of the same name in
        another account (names can be changed and reused in the game) is a different stored character with a
        different id, which no token of this account has ever posted for."""
        return [{"id": r["id"], "player": r["player"], "last_post": r["last_post"]} for r in self._q(
            "SELECT s.id, s.player, tp.last_post FROM token_posts tp "
            "JOIN tokens t ON t.id = tp.token AND t.revoked IS NULL "
            "JOIN states s ON s.id = tp.character AND s.account = t.account "
            "WHERE tp.token = ? ORDER BY s.player", (token_id,))]

    def delete_posted(self, account: str, token_id: str) -> list[str] | None:
        """Delete every character this token has posted for and revoke the token (its plugin is told so and
        stops posting, or the data would be back at its next post). Returns the deleted names; None when the
        token is not a live token of the account."""
        with self.lock:
            if not self._q("SELECT 1 FROM tokens WHERE id = ? AND account = ? AND revoked IS NULL", (token_id, account)):
                return None
            names = [p["player"] for p in self.posted_by(token_id)]
            for name in names:
                self.delete_state(account, name)
            self.revoke_id(account, token_id)
        return names

    # Consent to analysis (module docstring): the day it was given, on the stored character's row.
    def consent_of(self, account: str, player: str) -> dict[str, Any] | None:
        """{consent, consent_since} for a stored character, None when the account holds no such character."""
        rows = self._q("SELECT consent FROM states WHERE account = ? AND player = ?", (account, player))
        return {"consent": rows[0]["consent"] is not None, "consent_since": rows[0]["consent"]} if rows else None

    def set_consent(self, account: str, player: str, consent: bool) -> dict[str, Any] | None:
        """Give or withdraw a stored character's consent; giving it again keeps the day it was first given.
        A change is noted in the deletion log (the character's id and the new value), so that a restore from
        an older backup ends up with the player's latest answer, not the one in the backup."""
        with self.lock:
            rows = self._q("SELECT id, consent FROM states WHERE account = ? AND player = ?", (account, player))
            if not rows:
                return None
            value = (rows[0]["consent"] or _today()) if consent else None
            if value != rows[0]["consent"]:
                self._x("UPDATE states SET consent = ? WHERE id = ?", (value, rows[0]["id"]))
                self._note_deletion(consent=rows[0]["id"], value=value)
            return self.consent_of(account, player)

    def analysis_states(self) -> Iterator[dict[str, Any]]:
        """The stored states an analysis may read, as of now: the gate every analysis goes through. Only
        characters whose consent is given at this moment, and each without its name or its account: `id` is
        the stored character's random id (to tell characters apart within one run), `record` the stored record
        with the name taken out. Asked afresh for every analysis and never copied onto the data, which is what
        makes a change of mind cover what is already stored."""
        for r in self._q("SELECT id, record FROM states WHERE consent IS NOT NULL ORDER BY id"):
            record = json.loads(r["record"])
            record.pop("player", None)
            yield {"id": r["id"], "record": record}

    def _gc_token_posts(self) -> None:
        """Drop links whose token is no longer live or whose character is no longer stored."""
        self._x("DELETE FROM token_posts WHERE token NOT IN (SELECT id FROM tokens WHERE revoked IS NULL) "
                "OR character NOT IN (SELECT id FROM states)")
        self._posted_today.clear()

    def list_tokens(self) -> list[dict[str, Any]]:
        return [self._token_row(r) for r in self._q("SELECT * FROM tokens ORDER BY created, id")]

    def alive(self) -> bool:
        """Whether the database answers a read (the health route): no counts, nothing about its contents."""
        try:
            self._q("SELECT 1 FROM accounts LIMIT 1")
            return True
        except sqlite3.Error:
            log.exception("health: the store did not answer")
            return False

    def stats(self, minutes: float = 10) -> dict[str, Any]:
        """How busy the API is: characters that posted in the last `minutes`, all characters, live tokens."""
        count = lambda sql, *args: self._q(sql, args)[0]["n"]
        return {"recent_players": count("SELECT COUNT(*) AS n FROM states WHERE updated >= ?", time.time() - minutes * 60),
                "players": count("SELECT COUNT(*) AS n FROM states"), "minutes": minutes,
                "accounts": count("SELECT COUNT(*) AS n FROM accounts"),
                "tokens": count("SELECT COUNT(*) AS n FROM tokens WHERE revoked IS NULL"),
                "revoked_tokens": count("SELECT COUNT(*) AS n FROM tokens WHERE revoked IS NOT NULL"),
                "consenting_players": count("SELECT COUNT(*) AS n FROM states WHERE consent IS NOT NULL"), **self.settings()}

    # Config generations. Global: the packaged baseline (the service holds it), directories under generations/<fp>/
    # (written before pending sets existed, or by `configs confirm`) and their config_confirmed rows. Pending: sets
    # learned from an upload, stored once by content hash under pending_configs/<hash>/ and referenced per account.
    def generation_dir(self, fp: int) -> Path:
        return self.data_dir / "generations" / f"{fp:08x}"

    def pending_dir(self, content: str) -> Path:
        return self.data_dir / "pending_configs" / content

    def generations(self) -> list[int]:
        folder = self.data_dir / "generations"
        return sorted(int(p.name, 16) for p in folder.iterdir() if p.is_dir()) if folder.exists() else []

    def confirmed(self) -> dict[int, float]:
        """Global generations other than the baseline -> when they were learned (confirmed, or the directory's
        mtime for those from before pending sets: they count as confirmed)."""
        out = {fp: self.generation_dir(fp).stat().st_mtime for fp in self.generations()}
        out.update({r["fp"]: r["confirmed"] for r in self._q("SELECT fp, confirmed FROM config_confirmed")})
        return out

    def confirmed_stamp(self) -> tuple:
        """Changes whenever a generation is confirmed (by another process too): the service reloads on a change."""
        folder = self.data_dir / "generations"
        mtime = folder.stat().st_mtime_ns if folder.exists() else 0
        row = self._q("SELECT COUNT(*) AS n, COALESCE(MAX(confirmed), 0) AS t FROM config_confirmed")[0]
        return mtime, row["n"], row["t"]

    def load_generation(self, fp: int) -> GameConfigs | None:
        return GameConfigs.read(self.generation_dir(fp))

    def load_pending(self, content: str) -> GameConfigs | None:
        return GameConfigs.read(self.pending_dir(content))

    def pending_rows(self, fp: int, account: str | None = None) -> list[sqlite3.Row]:
        """Pending sets with this fingerprint, newest upload first: the account's own, or any (account None)."""
        if account is None:
            return self._q("SELECT r.hash, MAX(r.uploaded) AS uploaded FROM config_refs r JOIN config_contents c "
                           "ON c.hash = r.hash WHERE c.fp = ? GROUP BY r.hash ORDER BY uploaded DESC", (fp,))
        return self._q("SELECT r.hash, r.uploaded FROM config_refs r JOIN config_contents c ON c.hash = r.hash "
                       "WHERE r.account = ? AND c.fp = ? ORDER BY r.uploaded DESC", (account, fp))

    def pending_of(self, account: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT c.fp, r.hash, r.uploaded FROM config_refs r JOIN config_contents c "
                                         "ON c.hash = r.hash WHERE r.account = ? ORDER BY r.uploaded DESC", (account,))]

    def add_pending(self, account: str, configs: GameConfigs, base: GameConfigs, now: float | None = None) -> str:
        """Keep a set an account uploaded, visible to that account only. Identical content is stored once and
        referenced per account; beyond MAX_PENDING_PER_ACCOUNT the account's oldest reference goes. Returns the hash."""
        content, now = configs.content_hash(), now if now is not None else time.time()
        with self._tx():
            if not self._q("SELECT 1 FROM config_contents WHERE hash = ?", (content,)):
                folder = self.pending_dir(content)
                tmp = folder.with_name(folder.name + ".tmp")
                shutil.rmtree(tmp, ignore_errors=True)
                configs.write(tmp)
                shutil.rmtree(folder, ignore_errors=True)
                os.replace(tmp, folder)
                d = configs.diff_counts(base)
                self._x("INSERT INTO config_contents (hash, fp, base, created, q_added, q_removed, a_added, a_removed) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (content, configs.fingerprint, base.fingerprint, _today(),
                         d["q_added"], d["q_removed"], d["a_added"], d["a_removed"]))
            self._x("INSERT INTO config_refs (account, hash, uploaded) VALUES (?, ?, ?) "
                    "ON CONFLICT (account, hash) DO UPDATE SET uploaded = excluded.uploaded", (account, content, now))
            for r in self._q("SELECT hash FROM config_refs WHERE account = ? ORDER BY uploaded DESC LIMIT -1 OFFSET ?",
                             (account, MAX_PENDING_PER_ACCOUNT)):
                self._x("DELETE FROM config_refs WHERE account = ? AND hash = ?", (account, r["hash"]))
            self._gc_pending()
        return content

    def _gc_pending(self) -> None:
        """Remove pending content no account references any more, rows and files (and stray directories)."""
        for r in self._q("SELECT hash FROM config_contents WHERE hash NOT IN (SELECT hash FROM config_refs)"):
            self._x("DELETE FROM config_contents WHERE hash = ?", (r["hash"],))
        folder = self.data_dir / "pending_configs"
        if folder.exists():
            keep = {r["hash"] for r in self._q("SELECT hash FROM config_contents")}
            for p in folder.iterdir():
                if p.name not in keep:
                    shutil.rmtree(p, ignore_errors=True)

    def config_rows(self) -> list[dict[str, Any]]:
        """Every generation for the operator: global ones (baseline excluded) and pending contents."""
        rows: list[dict[str, Any]] = []
        confirmed = {r["fp"]: dict(r) for r in self._q("SELECT * FROM config_confirmed")}
        for fp, learned in sorted(self.confirmed().items(), key=lambda kv: kv[1]):
            r = confirmed.get(fp, {})
            rows.append({"status": "global", "fp": fp, "hash": r.get("hash"), "day": date.fromtimestamp(learned).isoformat(),
                         "accounts": None, "base": r.get("base"),
                         **{k: r.get(k) for k in ("q_added", "q_removed", "a_added", "a_removed")}})
        for r in self._q("SELECT c.*, (SELECT COUNT(*) FROM config_refs f WHERE f.hash = c.hash) AS accounts "
                         "FROM config_contents c ORDER BY c.created, c.fp, c.hash"):
            rows.append({"status": "pending", "fp": r["fp"], "hash": r["hash"], "day": r["created"], "accounts": r["accounts"],
                         "base": r["base"], **{k: r[k] for k in ("q_added", "q_removed", "a_added", "a_removed")}})
        return rows

    def confirm_config(self, fp: int, content: str | None = None) -> str:
        """Promote a pending set to global. `content` (a hash or unique prefix) picks between different sets that
        share a fingerprint. Raises ValueError when nothing, or no single set, matches. Returns the hash."""
        with self._tx():
            if fp in self.confirmed():
                raise ValueError(f"{fp:08x} is already global")
            rows = self._q("SELECT * FROM config_contents WHERE fp = ?", (fp,))
            if content:
                rows = [r for r in rows if r["hash"].startswith(content.lower())]
            if not rows:
                raise ValueError(f"no pending set {fp:08x}" + (f" with hash {content}" if content else ""))
            if len(rows) > 1:
                raise ValueError(f"{len(rows)} different sets share {fp:08x}; pick one with --hash: "
                                 + ", ".join(r["hash"][:12] for r in rows))
            row = rows[0]
            configs = self.load_pending(row["hash"])
            if configs is None:
                raise ValueError("the pending set's files are missing")
            tmp = self.data_dir / f".confirm-{fp:08x}"  # renamed into place, so a running server never reads half a set
            shutil.rmtree(tmp, ignore_errors=True)
            configs.stripped().write(tmp)  # no uploader's progress in a shared set
            self.generation_dir(fp).parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, self.generation_dir(fp))
            self._x("INSERT INTO config_confirmed (fp, hash, base, confirmed, q_added, q_removed, a_added, a_removed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (fp, row["hash"], row["base"], time.time(), row["q_added"], row["q_removed"], row["a_added"], row["a_removed"]))
            self._x("DELETE FROM config_refs WHERE hash IN (SELECT hash FROM config_contents WHERE fp = ?)", (fp,))
            self._gc_pending()
        return row["hash"]

    def drop_config(self, fp: int) -> int:
        """Forget every pending set with this fingerprint. Returns how many account references went."""
        with self._tx():
            n = self._x("DELETE FROM config_refs WHERE hash IN (SELECT hash FROM config_contents WHERE fp = ?)", (fp,))
            self._gc_pending()
        return n

    # States
    def load_state(self, owner: str, player: str) -> dict[str, Any] | None:
        rows = self._q("SELECT record FROM states WHERE account = ? AND player = ?", (owner, player))
        return json.loads(rows[0]["record"]) if rows else None

    def save_state(self, owner: str, player: str, record: dict[str, Any]) -> None:
        # The id is set once, when the character is first stored; a later save keeps it.
        self._x("INSERT INTO states (account, player, updated, seq, posts, record, id) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account, player) DO UPDATE SET updated = excluded.updated, seq = excluded.seq, "
                "posts = excluded.posts, record = excluded.record",
                (owner, player, record.get("updated"), record.get("seq"), record.get("posts"), json.dumps(record),
                 str(uuid.uuid4())))

    def delete_state(self, owner: str, player: str) -> bool:
        with self.lock:
            rows = self._q("SELECT id FROM states WHERE account = ? AND player = ?", (owner, player))
            if self._x("DELETE FROM states WHERE account = ? AND player = ?", (owner, player)) == 0:
                return False
            self._gc_token_posts()
        self._note_deletion(character=rows[0]["id"])
        return True

    def find_account_players(self, account: str) -> list[dict[str, Any]]:
        """An account's stored characters: [{id, player, last_post (day)}], by name."""
        return [{"id": r["id"], "player": r["player"],
                 "last_post": datetime.fromtimestamp(r["updated"], timezone.utc).date().isoformat() if r["updated"] else None}
                for r in self._q("SELECT id, player, updated FROM states WHERE account = ? ORDER BY player", (account,))]

    def find_player(self, name: str) -> list[dict[str, Any]]:
        """Every account holding a character of this name (case-insensitive): for a data request made by
        email, where the name is all the person can give. Returns the account id, the stored spelling and the
        day of the last post, nothing of the state itself."""
        rows = self._q("SELECT account, player, updated FROM states WHERE player = ? COLLATE NOCASE ORDER BY account", (name,))
        return [{"account": r["account"], "player": r["player"],
                 "last_post": datetime.fromtimestamp(r["updated"], timezone.utc).date().isoformat() if r["updated"] else None}
                for r in rows]

    def account_of_secret(self, secret: str) -> tuple[str | None, str]:
        """(account, what the secret is) for an account key or a live token: how a person proves which account
        is theirs. A read token limited to named characters is reported as "limited read token": it was handed
        to someone for those characters and does not speak for the account."""
        if (account := self.account_by_key(secret)) is not None:
            return account, "account key"
        info, status = self.lookup(secret)
        if info is None:
            return None, "revoked token" if status == "revoked" else "unknown"
        if info.characters is not None:
            return info.account, "limited read token"
        return info.account, "token"

    def count_states(self, owner: str) -> int:
        return self._q("SELECT COUNT(*) AS n FROM states WHERE account = ?", (owner,))[0]["n"]

    def players(self, owner: str) -> list[dict[str, Any]]:
        return [{"player": r["player"], "updated": r["updated"], "seq": r["seq"], "posts": r["posts"]}
                for r in self._q("SELECT player, updated, seq, posts FROM states WHERE account = ? ORDER BY player", (owner,))]

    # Migration from the JSON-file store (tokens.json, players/<owner>/<player>.json)
    def has_legacy_files(self) -> bool:
        return (self.data_dir / "tokens.json").exists() or (self.data_dir / "players").is_dir()

    def migrate_legacy(self) -> dict[str, int]:
        """One-shot import of a data dir written before SQLite: each tokens.json owner becomes an account (id =
        the owner name), each token a token with both scopes (so plugins and planners keep working), revoked ones
        stay revoked, and players/<owner>/*.json states move across. Idempotent; the old files are left in place."""
        counts = {"accounts": 0, "tokens": 0, "states": 0}
        before = {r["id"] for r in self._q("SELECT id FROM accounts")}
        legacy_tokens = self.data_dir / "tokens.json"
        with self._tx():
            if legacy_tokens.exists():
                for h, e in json.loads(legacy_tokens.read_text()).items():
                    self.ensure_account(e["owner"])
                    counts["tokens"] += self._x(
                        "INSERT OR IGNORE INTO tokens (id, hash, account, prefix, label, scopes, characters, created, revoked) "
                        "VALUES (?, ?, ?, ?, 'migrated', 'read,submit', NULL, ?, ?)",
                        (h[:12], h, e["owner"], e.get("prefix") or "", (e.get("created") or _today())[:10],
                         (e.get("revoked") or "")[:10] or None))
            folder = self.data_dir / "players"
            for path in sorted(folder.glob("*/*.json")) if folder.is_dir() else []:
                try:
                    rec = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    log.warning("migration: skipping unreadable %s", path)
                    continue
                owner, player = unquote(path.parent.name), rec.get("player") or unquote(path.stem)
                self.ensure_account(owner)
                counts["states"] += self._x(
                    "INSERT OR IGNORE INTO states (account, player, updated, seq, posts, record) VALUES (?, ?, ?, ?, ?, ?)",
                    (owner, player, rec.get("updated"), rec.get("seq"), rec.get("posts"), json.dumps(rec)))
            self._x("INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_migrated', ?)", (_today(),))
        counts["accounts"] = len({r["id"] for r in self._q("SELECT id FROM accounts")} - before)
        return counts


# --- Account creation guards -----------------------------------------------------------------------------------

class Captcha:
    """Decides whether an account creation came from a person. Pluggable: tests and self-hosters use the no-op."""

    def verify(self, response: str | None, ip: str | None) -> bool:
        return True


class TurnstileCaptcha(Captcha):
    """Cloudflare Turnstile, verified server-side (https://developers.cloudflare.com/turnstile/get-started/server-side-validation/).
    `post` is the HTTP call (url, form fields) -> JSON, replaceable in tests."""
    URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    def __init__(self, secret: str, post: Callable[[str, dict[str, str]], dict[str, Any]] | None = None):
        self.secret, self.post = secret, post or self._post

    @staticmethod
    def _post(url: str, fields: dict[str, str]) -> dict[str, Any]:
        with urllib.request.urlopen(urllib.request.Request(url, urllib.parse.urlencode(fields).encode()), timeout=10) as r:
            return json.load(r)

    def verify(self, response: str | None, ip: str | None) -> bool:
        if not response:
            return False
        fields = {"secret": self.secret, "response": response, **({"remoteip": ip} if ip else {})}
        try:
            return self.post(self.URL, fields).get("success") is True
        except (OSError, ValueError) as e:  # Cloudflare unreachable: refuse rather than let bots through
            log.warning("turnstile verification failed: %s", e)
            return False


def turnstile_secret(path: Path | None = None, environ: Any = None) -> tuple[str, str]:
    """(secret, where it came from) for `serve`; ("", "") when no file is named, which leaves the check off.

    The file is the one named by --turnstile-secret-file or $RUNE_ADVISOR_API_TURNSTILE_SECRET_FILE, else the
    systemd credential `turnstile_secret` (a file of that name in $CREDENTIALS_DIRECTORY, from the unit's
    LoadCredential=) when there is one. One trailing newline is dropped. A file that is named but missing,
    unreadable or empty stops the start: running with the check silently off is the one thing not to do."""
    environ = os.environ if environ is None else environ
    if path is None and environ.get(TURNSTILE_FILE_ENV):
        path = Path(environ[TURNSTILE_FILE_ENV])
    source = f"the file {path}" if path is not None else ""
    if path is None and environ.get("CREDENTIALS_DIRECTORY"):
        credential = Path(environ["CREDENTIALS_DIRECTORY"]) / TURNSTILE_CREDENTIAL
        if credential.exists():
            path, source = credential, f"the credential '{TURNSTILE_CREDENTIAL}'"
    if path is None:
        return "", ""
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit(f"Turnstile secret: cannot read {source} ({type(e).__name__}); not starting without the bot check") from None
    value = value[:-1] if value.endswith("\n") else value
    if not value.strip():
        raise SystemExit(f"Turnstile secret: {source} is empty; not starting without the bot check")
    return value, source


def limit_key(ip: str) -> str:
    """The key an address counts under for the account-creation limit: an IPv4 address itself, an IPv6 address's
    /64 (one customer's allocation: counted per exact address, a client would have 2^64 of them to rotate through)."""
    try:
        addr = ipaddress.ip_address(ip.strip().split("%", 1)[0])
    except ValueError:
        return ip
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.version == 6:
        return str(ipaddress.ip_network((int(addr) >> 64 << 64, 64)))
    return str(addr)


def loggable_path(path: str) -> str:
    """A request path for the log with character names taken out (`/v1/snapshot/<player>`): the service's log goes
    to the system journal, which is kept longer than the web server's access log."""
    path = path.split("?", 1)[0]
    path = re.sub(r"(/v1/tokens/)(?!posted$)[^/]+", r"\1<id>", path)  # a token id (a route that no longer exists)
    return re.sub(r"(/v1/(?:snapshot|state|players|consent)/)[^/]+", r"\1<player>", path)


class RateLimiter:
    """Sliding-window counter per key, in memory only: nothing about who created accounts is written to disk."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, window: float, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, []) if now - t < window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def allowed(self, key: str, limit: int, window: float, now: float | None = None) -> bool:
        with self._lock:
            now = now or time.time()
            self._calls = getattr(self, "_calls", 0) + 1
            if self._calls % 5000 == 0:  # forget keys that never came back (nothing else would remove them)
                for stale in [k for k, hits in self._hits.items() if not hits or now - hits[-1] > 86400]:
                    del self._hits[stale]
            return len(self._recent(key, window, now)) < limit

    def record(self, key: str, window: float, now: float | None = None) -> None:
        with self._lock:
            now = now or time.time()
            self._recent(key, window, now)
            self._hits.setdefault(key, []).append(now)


# --- Load ---

BUSY_REQUESTS = 24      # requests in hand at once before new ones are told "busy" (the work itself is one core's)
MAX_CONNECTIONS = 256   # open connections before new ones are closed unanswered: the last resort, under TasksMax
BUSY_RETRY_SECONDS = 60  # what a busy answer asks for; the plugin retries any failed post after about a minute anyway
LOAD_WINDOW_SECONDS = 10.0
LOAD_HIGH, LOAD_LOW = 0.6, 0.3  # share of one core spent on requests: above, lengthen the interval; below, relax it
# Requests a token may make per minute, by kind, at the interval being handed out (a plugin posts once per
# interval; config uploads come in bursts of parts after a game update; a planner caches a snapshot for a minute).
TOKEN_LIMITS = {"state": lambda interval: 12 + 2 * 60 / interval, "configs": lambda interval: 300.0,
                "read": lambda interval: 60.0}


class Load:
    """What the service knows about its own load, in memory: requests in hand, open connections, and the CPU
    time requests took in the current window. From that it stretches the post interval it hands out.

    The service is one process, so one core. When requests use more than LOAD_HIGH of it over a window, the
    factor on `post_interval` grows by half (bounded by `auto_interval_max`); under LOAD_LOW it shrinks by a
    sixth, back to 1. Every reply carries the interval and plugins follow it within one post, so the load
    falls in proportion: at factor 1.5 a 0.6 load becomes 0.4, inside the band, and it stays there.

    This only changes *when* plugins post, never what: the interval was always the server's to set
    (docs/export-api.md, "the server never chooses what player data the plugin sends")."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0          # requests being handled or waiting for the store
        self.connections = 0
        self.factor = 1.0
        self.cpu = 0.0           # CPU seconds of requests since `since`
        self.shed = 0            # busy answers since `since`
        self.since = time.monotonic()
        self.totals = {"busy": 0, "dropped": 0, "limited": 0}

    def interval(self, settings: dict[str, float]) -> float:
        base = settings["post_interval"]
        if not settings["auto_interval"]:
            return base
        return max(base, min(base * self.factor, settings["auto_interval_max"], MAX_INTERVAL))

    def finished(self, cpu_seconds: float, settings: dict[str, float], now: float | None = None) -> None:
        """Account for one request and, once per window, adjust the factor."""
        now = time.monotonic() if now is None else now
        with self.lock:
            self.cpu += cpu_seconds
            elapsed = now - self.since
            if elapsed < LOAD_WINDOW_SECONDS:
                return
            share, shed = self.cpu / elapsed, self.shed
            self.cpu, self.shed, self.since = 0.0, 0, now
            before = self.factor
            ceiling = max(1.0, settings["auto_interval_max"] / settings["post_interval"])
            if share > LOAD_HIGH or shed:
                self.factor = min(ceiling, self.factor * 1.5)
            elif share < LOAD_LOW:
                self.factor = max(1.0, self.factor / 1.2)
            changed = self.factor != before and bool(settings["auto_interval"])
        if changed:
            log.info("load: requests took %d%% of a core, %d told busy; post interval now %.0f s",
                     round(share * 100), shed, self.interval(settings))


# --- Applying posts ---------------------------------------------------------------------------------------------

class Service:
    """The protocol logic, independent of HTTP: `apply` handles one post, `snapshot` builds the planner's view."""

    def __init__(self, store: Store, configs: GameConfigs, item_names: ItemNames | None = None,
                 captcha: Captcha | None = None):
        self.store, self.configs = store, configs
        self.captcha, self.limiter = captcha or Captcha(), RateLimiter()  # no captcha given: no check (tests, self-hosting)
        self.load, self.token_limiter = Load(), RateLimiter()
        self.item_names = item_names or ItemNames.load()
        self._layouts: dict[tuple, wire.WireLayout] = {}  # most recently used last; at most LAYOUT_CACHE
        # Global config sets by fingerprint -> (configs, time learned): the package dump (time 0) and the confirmed
        # generations, re-read when `configs confirm` runs (any process). Pending sets live in the store, per account.
        self._global: dict[int, tuple[GameConfigs, float]] = {configs.fingerprint: (configs, 0.0)}
        self._global_stamp: tuple | None = None
        self._pending_cache: dict[str, GameConfigs] = {}  # content hash -> set (immutable, so safe to keep)
        self._staging: dict[tuple[str, int], dict[str, Any]] = {}  # (owner, fingerprint) -> parts so far

    # Accounts: self-service, no login. `owner` everywhere else in this class is the account id.
    def _captcha(self) -> Captcha:
        return self.captcha

    def create_account(self, body: Any, ip: str) -> dict[str, Any]:
        """POST /v1/accounts: a new account and its key, shown once. Guarded by the per-IP limit (counted on
        success, in memory) and the captcha."""
        cfg = self.store.settings()
        limit, window = int(cfg["account_ip_limit"]), cfg["account_ip_window"]
        counted = limit_key(ip)
        if not self.limiter.allowed(counted, limit, window):
            raise ApiError(429, "too many accounts created from this address; try again later")
        response = body.get("captcha") if isinstance(body, dict) else None
        # The bot check is told the caller's own address (not the /64 it is counted under): a check solved
        # from one address and presented from another is a sign of abuse, and Cloudflare can only see that
        # when it gets the address the request came from.
        if not self._captcha().verify(response if isinstance(response, str) else None, ip):
            raise ApiError(403, "captcha check failed")
        account, key = self.store.create_account()
        self.limiter.record(counted, window)
        return {"account": account, "key": key, "note": "Keep this key: it is shown once and cannot be recovered."}

    def create_token(self, account: str, body: Any) -> dict[str, Any]:
        """POST /v1/tokens: {label, scopes: [submit|read], characters: [names] (read tokens only)}."""
        body = body if isinstance(body, dict) else {}
        label = body.get("label", "")
        scopes = body.get("scopes", ["submit"])
        characters = body.get("characters")
        if not isinstance(label, str) or len(label) > MAX_LABEL:
            raise ApiError(400, f"label must be text of at most {MAX_LABEL} characters")
        if not (isinstance(scopes, list) and scopes and all(x in SCOPES for x in scopes)):
            raise ApiError(400, f"scopes must be a non-empty list of {', '.join(SCOPES)}")
        if characters is not None:
            if "read" not in scopes:
                raise ApiError(400, "characters limit what a read token may read; this token has no read scope")
            if not (isinstance(characters, list) and characters and len(characters) <= MAX_LIMITED_CHARACTERS
                    and all(isinstance(c, str) and c.strip() for c in characters)):
                raise ApiError(400, "characters must be a non-empty list of names")
        with self.store.lock:
            if len(self.store.tokens_of(account)) >= int(self.store.settings()["max_tokens"]):
                raise ApiError(403, "token limit reached; revoke one first")
            token_id, token = self.store.create_token(account, label.strip(), set(scopes), characters)
        return {"id": token_id, "token": token, "label": label.strip(), "scopes": sorted(set(scopes)),
                "characters": characters, "created": _today(),
                "note": "Keep this token: it is shown once and cannot be recovered."}

    def check_character_room(self, owner: str) -> None:
        """A new character on a full account is refused; existing characters keep posting."""
        if self.store.count_states(owner) >= int(self.store.settings()["max_characters"]):
            raise ApiError(403, "character limit reached; delete one first")

    @property
    def baseline(self) -> int:
        return self.configs.fingerprint

    def _globals(self) -> dict[int, tuple[GameConfigs, float]]:
        """The global sets, picking up anything confirmed since the last call without a restart."""
        stamp = self.store.confirmed_stamp()
        if stamp != self._global_stamp:
            for fp, learned in self.store.confirmed().items():
                if fp not in self._global and (loaded := self.store.load_generation(fp)) is not None:
                    self._global[fp] = (loaded, learned)
            self._global_stamp = stamp
        return self._global

    def _pending_set(self, content: str) -> GameConfigs | None:
        if content not in self._pending_cache:
            if (loaded := self.store.load_pending(content)) is None:
                return None
            if len(self._pending_cache) >= 32:
                self._pending_cache.pop(next(iter(self._pending_cache)))
            self._pending_cache[content] = loaded
        return self._pending_cache[content]

    def lookup(self, fp: int, owner: str | None = None) -> tuple[GameConfigs, str] | None:
        """(configs, identity) for a fingerprint as `owner` sees it: global first, else the owner's own pending set.
        Owner None is the unscoped view for in-process callers (tests, tools); the HTTP layer always names the account."""
        with self.store.lock:
            if (found := self._globals().get(fp)) is not None:
                return found[0], f"g{fp:08x}"
            for row in self.store.pending_rows(fp, owner):
                if (loaded := self._pending_set(row["hash"])) is not None:
                    return loaded, row["hash"]
        return None

    def generation(self, fp: int | None, owner: str | None = None) -> GameConfigs | None:
        """The configs for a fingerprint; a post without one (an older plugin) gets the package dump."""
        if fp is None:
            return self.configs
        found = self.lookup(fp, owner)
        return found[0] if found else None

    def visible_generations(self, owner: str) -> tuple[list[int], list[int]]:
        """(global fingerprints, the owner's pending ones) for GET /v1/configs."""
        with self.store.lock:
            return sorted(self._globals()), sorted({p["fp"] for p in self.store.pending_of(owner)})

    def layout(self, fields: list[tuple[str, int]], skills: list[str], inventories: list[str], fp: int | None = None,
               owner: str | None = None, legacy: bool = False) -> wire.WireLayout:
        """The layout for these configs and these lists. `legacy`: a 0.4.x post's `named` list (32 bits each)."""
        found = (self.configs, f"g{self.baseline:08x}") if fp is None else self.lookup(fp, owner)
        if found is None:
            raise ApiError(428, f"configs {fp:#010x} unknown", extra=self._need_configs(owner))
        configs, ident = found
        key = (ident, legacy, tuple(fields), tuple(skills), tuple(inventories))
        layout = self._layouts.pop(key, None)
        if layout is None:
            layout = wire.WireLayout.build(configs, list(fields), list(skills), list(inventories), legacy=legacy)
            while len(self._layouts) >= LAYOUT_CACHE:
                del self._layouts[next(iter(self._layouts))]  # the least recently used
        self._layouts[key] = layout
        return layout

    def _record_layout(self, owner: str, rec: dict[str, Any]) -> wire.WireLayout:
        """The layout a stored record was posted under: a 0.4.x record carries its `named` list, a later one names
        its manifest (wire_manifests/, kept for every released version)."""
        lay = rec["layout"]
        if "named" in lay:
            return self.layout([(n, 32) for n in lay["named"]], lay["skills"], lay["inventories"], rec.get("configs"),
                               owner, legacy=True)
        fields = wire.load_manifest(lay["manifest"])
        if fields is None:
            raise ApiError(409, f"the stored state's manifest {lay['manifest']:#010x} is not on this server")
        return self.layout([(n, w) for n, w, _ in fields], lay["skills"], lay["inventories"], rec.get("configs"), owner)

    @staticmethod
    def _post_lists(post: dict[str, Any]) -> tuple[list[tuple[str, int]], list[str], list[str], bool, list[int]]:
        """(fields, skills, inventories, legacy, absent) a full post's layout is built from. Since plugin 0.5.0 the
        post names its manifest by hash and lists the fields its client lacks (`absent`, indexes into it); a 0.4.x
        post sends its `named` list, every name 32 bits. A manifest this server doesn't have means a plugin newer
        than the server: refused (the plugin retries in a minute; the server is deployed before a plugin release)."""
        def names(key: str, limit: int) -> list[str]:
            value = post.get(key)
            if not isinstance(value, list) or len(value) > limit or not all(
                    isinstance(x, str) and LIST_NAME.fullmatch(x) for x in value):
                raise ApiError(400, f"a full post's {key} list is malformed")
            if len(set(value)) != len(value):
                raise ApiError(400, f"a name appears twice in {key}")
            return value
        skills, inventories = names("skills", MAX_LIST_NAMES), names("inventories", MAX_LIST_NAMES)
        if "named" in post:
            return [(n, 32) for n in names("named", MAX_FIELDS)], skills, inventories, True, []
        h = post.get("manifest")
        if not isinstance(h, int):
            raise ApiError(400, "a full post needs its manifest hash")
        fields = wire.load_manifest(h)
        if fields is None:
            raise ApiError(400, f"manifest {h:#010x} unknown: the plugin is newer than this server")
        absent = post.get("absent", [])
        if not isinstance(absent, list) or not all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(fields)
                                                   for i in absent) or len(set(absent)) != len(absent):
            raise ApiError(400, "absent must be distinct indexes into the manifest")
        return [(n, w) for n, w, _ in fields], skills, inventories, False, sorted(absent)

    def _need_configs(self, owner: str | None = None) -> dict[str, Any]:
        """The 428 body: the hashes of the base the plugin should diff against. That is the newest global set (by
        time learned or confirmed), or the caller's own pending set when it is newer: never another account's."""
        with self.store.lock:
            fp, (configs, learned) = max(self._globals().items(), key=lambda kv: (kv[1][1], kv[0]))
            if owner is not None:
                for p in self.store.pending_of(owner):  # newest first
                    if p["uploaded"] > learned and (own := self._pending_set(p["hash"])) is not None:
                        fp, configs = p["fp"], own
                        break
        return {"need": "configs", "base": fp,
                "known": {kind: {str(i): h for i, h in table.items()} for kind, table in configs.hashes().items()}}

    def _expire_staging(self, now: float) -> None:
        for key in [k for k, v in self._staging.items() if now - v["t"] > STAGING_SECONDS]:
            del self._staging[key]

    def upload_configs(self, owner: str, part: dict[str, Any]) -> dict[str, Any]:
        """One part of a config difference; the last part composes and verifies the new generation, which stays
        pending (visible to this account only) until the operator confirms it."""
        if not isinstance(part, dict):
            raise ApiError(400, "the part is not a JSON object")
        fp, base = part.get("fingerprint"), part.get("base")
        n, of = part.get("part"), part.get("parts")
        if not isinstance(fp, int) or not isinstance(base, int):
            raise ApiError(400, "fingerprint and base are required")
        if not (isinstance(n, int) and isinstance(of, int) and 1 <= n <= of <= 1000):
            raise ApiError(400, "part/parts out of range")
        with self.store.lock:
            now = time.monotonic()
            self._expire_staging(now)
            base_found = self.lookup(base, owner)
            if base_found is None:
                raise ApiError(409, f"base {base:#010x} is not a set the server knows", extra=self._need_configs(owner))
            if self.lookup(fp, owner) is not None:  # a global set, or this account's own pending one
                self._staging.pop((owner, fp), None)
                return {"accepted": True, "fingerprint": fp, "known": True}
            if (owner, fp) not in self._staging:
                mine = sorted((k for k in self._staging if k[0] == owner), key=lambda k: self._staging[k]["t"])
                for k in mine[:max(0, len(mine) - MAX_STAGED_PER_ACCOUNT + 1)]:  # make room: the oldest goes
                    del self._staging[k]
            staged = self._staging.setdefault((owner, fp), {"base": base, "parts": of, "got": set(), "entries": 0, "t": now,
                                                            "added": {"quests": [], "achievements": []},
                                                            "removed": {"quests": [], "achievements": []}})
            if staged["base"] != base or staged["parts"] != of:
                self._staging.pop((owner, fp), None)
                raise ApiError(409, "the parts disagree about base or count; start again")
            if n in staged["got"]:
                return {"accepted": True, "fingerprint": fp, "part": n, "duplicate": True}
            staged["got"].add(n)
            staged["t"] = now
            for kind in ("quests", "achievements"):
                staged["added"][kind].extend(e for e in _as_list((part.get("added") or {}).get(kind)) if isinstance(e, dict))
                staged["removed"][kind].extend(i for i in _as_list((part.get("removed") or {}).get(kind)) if isinstance(i, int))
            staged["entries"] = sum(len(v) for d in (staged["added"], staged["removed"]) for v in d.values())
            if staged["entries"] > MAX_STAGED_ENTRIES:
                self._staging.pop((owner, fp), None)
                raise ApiError(413, f"more than {MAX_STAGED_ENTRIES} config entries in one upload")
            if len(staged["got"]) < of:
                return {"accepted": True, "fingerprint": fp, "part": n}
            self._staging.pop((owner, fp), None)
            base_configs = base_found[0]
            composed = base_configs.compose(staged["added"], staged["removed"])
            if composed.fingerprint != fp:
                raise ApiError(409, f"the composed set hashes to {composed.fingerprint:#010x}, not {fp:#010x}",
                               extra=self._need_configs(owner))
            content = self.store.add_pending(owner, composed, base_configs)
            self._pending_cache[content] = composed
            log.info("configs %08x (%s) uploaded by %s, pending: +%d/-%d quests, +%d/-%d achievements", fp, content[:12], owner,
                     len(staged["added"]["quests"]), len(staged["removed"]["quests"]),
                     len(staged["added"]["achievements"]), len(staged["removed"]["achievements"]))
            return {"accepted": True, "fingerprint": fp, "learned": True}

    @staticmethod
    def _unpack_record(layout: wire.WireLayout, rec: dict[str, Any]) -> wire.WireState:
        return wire.unpack_full(layout, base64.b64decode(rec["state"]))

    def _record(self, player: str, layout: wire.WireLayout, canon: bytes, h: int, header: dict,
                known: dict[str, dict[int, int]], posts: int, plugin: str | None = None,
                absent: list[int] | None = None) -> dict[str, Any]:
        """The stored record; `canon` is the state's canonical bytes (packed once per post, hashed and stored)."""
        return {
            "player": player, "schema": SCHEMA_VERSION, "plugin": header.get("plugin") or plugin,
            "configs": layout.configs_fingerprint,
            "layout": ({"hash": layout.hash, "named": layout.named, "skills": layout.skills, "inventories": layout.inventories}
                       if layout.legacy else {"hash": layout.hash, "manifest": layout.manifest_hash, "skills": layout.skills,
                                              "inventories": layout.inventories, "absent": absent}),
            "state": base64.b64encode(canon).decode("ascii"),
            "hash": h, "seq": header.get("seq"), "posts": posts, "updated": time.time(),
            "known_invs": {name: {str(i): c for i, c in items.items()} for name, items in known.items()},
        }

    def apply(self, owner: str, post: dict[str, Any]) -> dict[str, Any]:
        """Apply one post. Returns the JSON reply; raises ApiError(409, ...) when the state is out of step
        and ApiError(400, ...) for a malformed post."""
        if not isinstance(post, dict):
            raise ApiError(400, "the post is not a JSON object")
        player = post.get("player")
        kind = post.get("kind")
        if not isinstance(player, str) or not player.strip():
            raise ApiError(400, "no player name")
        if kind not in ("full", "delta"):
            raise ApiError(400, f"unknown kind {kind!r}")
        try:
            body = base64.b64decode(post.get("body") or "", validate=True)
        except (ValueError, TypeError):
            raise ApiError(400, "the body is not base64")
        for key in ("layout", "new"):
            if not isinstance(post.get(key), int):
                raise ApiError(400, f"missing {key} hash")
        with self.store.lock:
            rec = self.store.load_state(owner, player)
            posts = (rec.get("posts") or 0) + 1 if rec else 1
            if kind == "full":
                fp = post.get("configs")
                if fp is not None and not isinstance(fp, int):
                    raise ApiError(400, "configs must be the fingerprint")
                fields, skills, inventories, legacy, absent = self._post_lists(post)
                layout = self.layout(fields, skills, inventories, fp, owner, legacy=legacy)
                if layout.hash != post["layout"]:
                    raise ApiError(409, f"layout hash {layout.hash:#010x} differs from the plugin's {post['layout']:#010x}: "
                                        "the server's game configs don't match the client's")
                try:
                    state = wire.unpack_full(layout, body)
                except Exception as e:  # too short, bad widths
                    raise ApiError(400, f"can't unpack the full state: {type(e).__name__}: {e}")
                canon = wire.canonical(layout, state)  # re-packed from what was understood, not the body as sent
                h = wire.hash_canonical(layout, canon)
                if h != post["new"]:
                    raise ApiError(409, f"state hash {h:#010x} differs from the plugin's {post['new']:#010x}")
                known = {k: {int(i): c for i, c in v.items()} for k, v in (rec or {}).get("known_invs", {}).items()}
                known.update({k: dict(v) for k, v in state.invs.items()})
                if rec is None:
                    self.check_character_room(owner)
                self.store.save_state(owner, player, self._record(player, layout, canon, h, post, known, posts,
                                                                   absent=absent))
                return self._accepted(kind, h, post)
            # delta
            if rec is None:
                raise ApiError(409, "no state for this player yet")
            if rec["layout"]["hash"] != post["layout"]:
                raise ApiError(409, "layout changed")
            if post.get("base") != rec["hash"]:
                raise ApiError(409, f"base hash {post.get('base')} is not the server's state {rec['hash']}")
            layout = self._record_layout(owner, rec)
            state = self._unpack_record(layout, rec)
            try:
                wire.apply_delta(layout, state, body)
            except Exception as e:
                raise ApiError(400, f"can't apply the delta: {type(e).__name__}: {e}")
            canon = wire.canonical(layout, state)
            h = wire.hash_canonical(layout, canon)
            if h != post["new"]:
                raise ApiError(409, f"state after the delta hashes to {h:#010x}, the plugin says {post['new']:#010x}")
            known = {k: {int(i): c for i, c in v.items()} for k, v in rec.get("known_invs", {}).items()}
            known.update({k: dict(v) for k, v in state.invs.items()})
            self.store.save_state(owner, player, self._record(player, layout, canon, h, post, known, posts,
                                                               plugin=rec.get("plugin"), absent=rec["layout"].get("absent")))
            return self._accepted(kind, h, post)

    def interval(self) -> float:
        """The seconds between posts every reply carries: `post_interval`, stretched while the service is loaded."""
        return self.load.interval(self.store.settings())

    def check_token_rate(self, token_id: str, kind: str) -> None:
        """Per-token request limit (in memory, a minute's window): 429 for a client posting or reading far faster
        than the plugin and planner do. The plugin treats it like any failed post and retries in a minute."""
        limit = int(TOKEN_LIMITS[kind](self.interval()))
        key = f"{kind}:{token_id}"
        if not self.token_limiter.allowed(key, limit, 60.0):
            self.load.totals["limited"] += 1
            raise ApiError(429, "too many requests with this token; slow down", extra={"retry_after": 60})
        self.token_limiter.record(key, 60.0)

    def _accepted(self, kind: str, h: int, post: dict[str, Any]) -> dict[str, Any]:
        return {"accepted": True, "kind": kind, "hash": h, "seq": post.get("seq"), "interval": self.interval()}

    def raw_state(self, owner: str, player: str) -> dict[str, Any] | None:
        return self.store.load_state(owner, player)

    def snapshot(self, owner: str, player: str, today: date | None = None) -> dict[str, Any] | None:
        """The schema-2 snapshot (what the plugin writes to PersistentDB) rebuilt from the stored state."""
        rec = self.store.load_state(owner, player)
        if rec is None:
            return None
        layout = self._record_layout(owner, rec)
        state = self._unpack_record(layout, rec)
        known = {k: {int(i): c for i, c in v.items()} for k, v in rec.get("known_invs", {}).items()}
        self.item_names = self.item_names.fresh()
        snap = snapshot_from_state(self.generation(rec.get("configs"), owner) or self.configs, layout, state, known, self.item_names,
                                   player=rec.get("player", player), exported_at=rec.get("updated"), today=today)
        snap["configs"] = layout.configs_fingerprint
        snap["plugin"] = rec.get("plugin")
        for i in rec["layout"].get("absent") or []:  # fields the client lacked: sent as 0, not a value
            snap["vars"].pop(layout.fields[i][0], None)
        return snap


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= (1 << 31) else value


def snapshot_from_state(configs: GameConfigs, layout: wire.WireLayout, state: wire.WireState,
                        known_invs: dict[str, dict[int, int]], item_names: ItemNames, player: str,
                        exported_at: float | None = None, today: date | None = None) -> dict[str, Any]:
    fields, named, skills_xp, members = wire.values(layout, state)
    # Named vars travel unsigned; a whole 32-bit variable's value is signed on the client (the statue bag
    # mirrors read -1 until the bag is opened, and the planner tests for exactly that). A narrower field is a
    # VarBit, which is never negative.
    width = dict(layout.fields)
    named = {k: _signed32(v) if width.get(k, 32) == 32 else v for k, v in named.items()}
    # Levels as Player.GetLevelUncapped reports them: from the xp, capped at the skill's own maximum (99 for
    # Fishing, 120 for Archaeology), not virtual levels.
    skills = {}
    for name, xp in skills_xp.items():
        skill = SKILLTYPE_TO_NAME.get(name, name.capitalize())
        skills[name] = {"xp": xp, "level": min(level_at_xp(skill, xp), level_cap(skill))}
    levels = {name: s["level"] for name, s in skills.items()}
    d = derive(configs, fields, levels, today=today)
    notes: list[str] = []

    quests = []
    for q in configs.quests.values():
        if q.get("deprecated"):
            continue
        st = d.quests.get(q["id"])
        var = (q.get("progressVar") or {}).get("name") if isinstance(q.get("progressVar"), dict) else None
        quests.append({"id": q["id"], "started": None if st is None else st != "not started",
                       "finished": None if st is None else st == "finished", "canStart": None,
                       "progress": fields.get(var) if var else None})
    unknown_quests = sum(1 for q in quests if q["started"] is None)
    if unknown_quests:
        notes.append(f"{unknown_quests} quests have no progress var in the wire state (state unknown)")

    achievements = []
    today = today or date.today()
    for a in configs.achievements.values():
        complete = d.complete.get(a["id"])
        if complete:
            achievements.append({"id": a["id"], "progress": PROGRESS_COMPLETE})
            continue
        progress = PROGRESS_GRACE if in_grace(a, today) else PROGRESS_READY
        entry: dict[str, Any] = {"id": a["id"], "progress": progress}
        tests = _as_list(a.get("requirements"))
        results = d.tests.get(a["id"]) or []
        if tests and len(results) == len(tests):
            states = []
            for t, r in zip(tests, results):
                s = [1 if r else 0]
                if t.get("kind") == "varTotal":
                    s.append(sum(fields.get((v or {}).get("name"), 0) or 0 for v in _as_list(t.get("vars"))))
                states.append(s)
            entry["tests"] = states
        achievements.append(entry)

    by_config = item_names.by_config()
    item_log = {}
    for key, value in named.items():
        if key.startswith("ITEMLOG_ITEM_") and value > 0:
            item_log[by_config.get(key[len("ITEMLOG_ITEM_"):], key)] = value

    inventories = {}
    unnamed: set[int] = set()
    for inv_name in layout.inventories:
        items = known_invs.get(inv_name)
        if items is None:
            continue
        rows = []
        for item_id in sorted(items):
            name = item_names.name(item_id)
            if name is None:
                unnamed.add(item_id)
                name = f"Unknown item {item_id}"
            rows.append({"id": item_id, "name": name, "config": item_names.config(item_id) or "", "count": items[item_id],
                         "placeholder": False})
        inventories[inv_name] = {"exportedAt": exported_at, "items": rows}
    if unnamed:
        notes.append(f"{len(unnamed)} item ids have no name in data/item_names.json (shown as 'Unknown item <id>'): "
                     + ", ".join(str(i) for i in sorted(unnamed)[:10]))

    return {
        "schema": SCHEMA_VERSION, "player": player, "exportedAt": exported_at, "source": "export_api",
        "combatLevel": None, "membersWorld": members, "skills": skills, "vars": named, "itemLog": item_log,
        "quests": quests, "achievements": achievements, "inventories": inventories, "notes": notes,
    }


# --- HTTP ----------------------------------------------------------------------------------------------------

class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128  # the listen backlog; the default of 5 reset connections in a burst (load check, 2026-11-13)

    def process_request(self, request: Any, client_address: Any) -> None:
        """One thread per connection, up to MAX_CONNECTIONS; past that the connection is closed at once (nginx
        answers 502 and the plugin retries in a minute), so threads and memory stay bounded."""
        load = self.service.load
        with load.lock:
            if load.connections >= MAX_CONNECTIONS:
                load.totals["dropped"] += 1
                full = True
            else:
                load.connections += 1
                full = False
        if full:
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.service.load.lock:
                self.service.load.connections -= 1

    def __init__(self, address: tuple[str, int], service: Service, prefix: str = "/export_api",
                 cors_origin: str = CORS_ORIGIN, inherited: socket.socket | None = None):
        self.cors_origin = cors_origin.rstrip("/")
        self.inherited = inherited is not None
        if inherited is None:
            super().__init__(address, Handler)
        else:
            # A listening socket handed over by the service manager (socket activation): use it as it is. It is
            # already bound and listening, with the manager's backlog, and the manager keeps its own copy open,
            # so connections made while this process restarts wait there instead of being refused.
            super().__init__(address, Handler, bind_and_activate=False)
            self.socket.close()
            self.socket = inherited
            self.address_family = inherited.family
            self.server_address = inherited.getsockname()
            host, port = self.server_address[:2]
            self.server_name, self.server_port = str(host), port
        self.service = service
        self.prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""


class Handler(BaseHTTPRequestHandler):
    server: ApiServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # no tokens are ever in the path, so paths are safe to log
        log.info("%s %s", self.address_string(), fmt % args)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:  # the request line without character names
        status = code.value if hasattr(code, "value") else code
        self.log_message('"%s %s" %s', self.command, loggable_path(self.path), status)

    def _route(self) -> list[str]:
        if "?" in self.path:  # no route takes one; see the module docstring
            raise ApiError(400, "no query strings: parameters travel in headers or the body")
        path = self.path
        if self.server.prefix and path.startswith(self.server.prefix + "/"):
            path = path[len(self.server.prefix):]
        elif self.server.prefix and path == self.server.prefix:
            path = "/"
        route = [unquote(p) for p in path.strip("/").split("/") if p]
        # The character's name comes in a header so that it is in no URL (and so in no access log); it is
        # appended here as the route's third part. A name in the path is not a route.
        named = (self.command == "GET" and route[:2] in (["v1", "snapshot"], ["v1", "state"])) or (
            self.command == "DELETE" and route[:2] == ["v1", "players"]) or (
            self.command in ("GET", "POST") and route[:2] == ["v1", "consent"])
        if named and len(route) != 2:
            raise ApiError(404, "no such route")
        if named:
            player = unquote(self.headers.get(PLAYER_HEADER, "")).strip()
            if not player:
                raise ApiError(400, f"name the character in the {PLAYER_HEADER} header")
            route.append(player)
        return route

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if status in (429, 503) and isinstance(payload.get("retry_after"), (int, float)):
            self.send_header("Retry-After", str(int(payload["retry_after"])))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _cors_origin(self) -> str | None:
        """The request's Origin when it is the one allowed origin (the site), else None."""
        origin = self.headers.get("Origin", "")
        return origin if origin and origin == self.server.cors_origin else None

    def _cors_headers(self) -> None:
        """Let the site's pages read the reply. One exact origin, never `*`, no credentials: tokens travel in the
        Authorization header the page sets itself, so there is no ambient cookie another site could ride on."""
        if self.server.cors_origin:
            self.send_header("Vary", "Origin")
            if self._cors_origin():
                self.send_header("Access-Control-Allow-Origin", self.server.cors_origin)

    def do_OPTIONS(self) -> None:
        """A browser's preflight: 204 with the allowed methods and headers for the site's origin, 403 otherwise."""
        if not self._cors_origin():
            self._reply(403, {"accepted": False, "reason": "origin not allowed"})
            return
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE")
        self.send_header("Access-Control-Allow-Headers", f"Authorization, Content-Type, {PLAYER_HEADER}, {TOKEN_ID_HEADER}")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _bearer(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise ApiError(401, "no bearer token")
        return header[len("Bearer "):].strip()

    def _token(self, *scopes: str) -> TokenInfo:
        """The token's info if it is valid and holds one of `scopes` (any, when none are named)."""
        store = self.server.service.store
        info, status = store.lookup(self._bearer())
        if info is None:
            raise ApiError(401, f"{status} token", token=status)
        if scopes and not info.scopes & set(scopes):
            raise ApiError(403, f"this token has no {' or '.join(scopes)} scope")
        store.touch(info)
        return info

    def _account(self) -> str:
        """The account the bearer key belongs to (account keys manage tokens; they never post or read)."""
        account = self.server.service.store.account_by_key(self._bearer())
        if account is None:
            raise ApiError(401, "unknown account key", token="unknown")
        return account

    def _client_ip(self) -> str:
        """The caller's address, for the account-creation limit. Behind nginx it is the last X-Forwarded-For entry
        (the one the proxy appended; earlier ones are the client's to forge), used only when `trust_proxy` is set."""
        ip = self.client_address[0]
        if self.server.service.store.settings()["trust_proxy"]:
            forwarded = [p.strip() for p in self.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
            ip = forwarded[-1] if forwarded else ip
        return ip

    @staticmethod
    def _json(raw: bytes) -> Any:
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "the body is not JSON")

    def _token_id(self) -> str:
        """The id of the token an account-key request is about: in a header, like a character's name."""
        token_id = self.headers.get(TOKEN_ID_HEADER, "").strip()
        if not token_id:
            raise ApiError(400, f"name the token's id in the {TOKEN_ID_HEADER} header")
        return token_id

    def _consent_wanted(self, raw: bytes) -> bool:
        body = self._json(raw)
        if not isinstance(body, dict) or not isinstance(body.get("consent"), bool):
            raise ApiError(400, "send {\"consent\": true} or {\"consent\": false}")
        return body["consent"]

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(413, f"body over {MAX_BODY_BYTES} bytes")
        return self.rfile.read(length) if length else b""

    def _manage(self, method: str, route: list[str], raw: bytes) -> bool:
        """The account-key routes (accounts, tokens, deleting data). True when the request was one of them."""
        service = self.server.service
        store = service.store
        if route == ["v1", "accounts"] and method == "POST":
            self._reply(201, service.create_account(self._json(raw), self._client_ip()))
        elif route == ["v1", "account"] and method == "GET":
            quotas = {k: int(v) for k, v in store.settings().items() if k in ("max_characters", "max_tokens", "retention_days")}
            account = self._account()
            # The characters by name with the day each last posted, so the page can show what the account holds.
            held = [{"player": p["player"], "posts": p["posts"], **(store.consent_of(account, p["player"]) or {}),
                     "last_post": datetime.fromtimestamp(p["updated"], timezone.utc).date().isoformat() if p["updated"] else None}
                    for p in store.players(account)]
            self._reply(200, {**(store.account_info(account) or {}), **quotas, "players": held})
        elif route == ["v1", "account"] and method == "DELETE":
            self._reply(200, {"deleted": True, **store.delete_account(self._account())})
        elif route == ["v1", "tokens"] and method == "GET":
            self._reply(200, {"tokens": [{k: v for k, v in t.items() if k != "owner"} for t in store.tokens_of(self._account())]})
        elif route == ["v1", "tokens"] and method == "POST":
            self._reply(201, service.create_token(self._account(), self._json(raw)))
        elif route == ["v1", "tokens"] and method == "DELETE":
            account, token_id = self._account(), self._token_id()
            if not store.revoke_id(account, token_id):
                raise ApiError(404, "no such token")
            self._reply(200, {"revoked": token_id})
        elif route == ["v1", "tokens", "posted"] and method == "DELETE":
            account, token_id = self._account(), self._token_id()
            names = store.delete_posted(account, token_id)
            if names is None:
                raise ApiError(404, "no such token")
            self._reply(200, {"deleted": names, "revoked": token_id})
        elif len(route) == 3 and route[:2] == ["v1", "players"] and method == "DELETE":
            if not store.delete_state(self._account(), route[2]):
                raise ApiError(404, "no state for this player")
            self._reply(200, {"deleted": route[2]})
        else:
            return False
        return True

    def _handle(self, method: str) -> None:
        service = self.server.service
        load = service.load
        started = time.thread_time()
        with load.lock:
            load.active += 1
            busy = load.active > BUSY_REQUESTS
        try:
            self._handle_request(method, busy)
        finally:
            with load.lock:
                load.active -= 1
            load.finished(time.thread_time() - started, service.store.settings())

    def _handle_request(self, method: str, busy: bool) -> None:
        try:
            route = self._route()
            if route == ["v1", "health"] and method == "GET":
                ok = self.server.service.store.alive()  # a real read, so a broken database is not "healthy"
                self._reply(200 if ok else 503, {"ok": ok})
                return
            if len(route) < 2 or route[0] != "v1":
                raise ApiError(404, "no such route")
            raw = self._body()
            if busy:
                # Too much in hand already: say so now, cheaply (no token lookup, no JSON, no database), instead of
                # letting the request wait until its client gives up. The plugin retries a failed post in a minute.
                load = self.server.service.load
                with load.lock:
                    load.shed += 1
                    load.totals["busy"] += 1
                raise ApiError(503, "busy; try again in a minute", extra={"retry_after": BUSY_RETRY_SECONDS})
            if self._manage(method, route, raw):
                return
            if route[1] in ("state", "configs") and len(route) == 2 and method == "POST":
                info = self._token("submit")
                self.server.service.check_token_rate(info.id, route[1])
                try:
                    post = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ApiError(400, "the body is not JSON")
                if route[1] == "state":
                    reply = self.server.service.apply(info.account, post)
                    if reply.get("accepted") and isinstance(post, dict) and isinstance(post.get("player"), str):
                        self.server.service.store.note_post(info.id, info.account, post["player"])
                        # The character's consent to analysis: so a change made on the site reaches the plugin.
                        reply["consent"] = (self.server.service.store.consent_of(info.account, post["player"]) or {}).get("consent", False)
                    self._reply(200, reply)
                else:
                    self._reply(200, self.server.service.upload_configs(info.account, post))
                return
            if route[:2] == ["v1", "consent"] and len(route) == 3 and method in ("GET", "POST"):
                # A character's consent to analysis, asked for or changed: with the account key for any character
                # of the account, with a plugin's token for the characters it has posted for (what it proves).
                store = self.server.service.store
                account = store.account_by_key(self._bearer())
                if account is None:
                    info = self._token("submit")
                    self.server.service.check_token_rate(info.id, "read")
                    if route[2].lower() not in {p["player"].lower() for p in store.posted_by(info.id)}:
                        raise ApiError(404, "this token has not posted for that character")
                    account = info.account
                found = (store.set_consent(account, route[2], self._consent_wanted(raw)) if method == "POST"
                         else store.consent_of(account, route[2]))
                if found is None:
                    raise ApiError(404, "no state for this player")
                self._reply(200, found)
                return
            if route == ["v1", "posted"] and method in ("GET", "DELETE"):
                # What a plugin's token has posted, and deleting exactly that: for someone who lost the account
                # key but still has the token in their plugin. Followed by character id (`Store.posted_by`).
                info = self._token("submit")
                self.server.service.check_token_rate(info.id, "read")
                store = self.server.service.store
                if method == "GET":
                    row = next(t for t in store.tokens_of(info.account) if t["id"] == info.id)
                    self._reply(200, {"token": {k: row[k] for k in ("label", "prefix", "created", "last_used")},
                                      "posted": [{**p, **(store.consent_of(info.account, p["player"]) or {})} for p in row["posted"]]})
                else:
                    self._reply(200, {"deleted": store.delete_posted(info.account, info.id) or [], "revoked": True})
                return
            if method != "GET":
                raise ApiError(405, "method not allowed")
            if route[1] == "players" and len(route) == 2:
                info = self._token("submit", "read")  # submit: the plugin's check of a pasted token
                listing = self.server.service.store.players(info.account)
                if info.characters is not None:
                    listing = [p for p in listing if info.can_read(p["player"])]
                self._reply(200, {"players": listing})
            elif route[1] == "configs" and len(route) == 2:
                info = self._token()
                shared, own = self.server.service.visible_generations(info.account)
                self._reply(200, {"baseline": self.server.service.baseline, "generations": sorted({*shared, *own}),
                                  "pending": own})
            elif route[1] == "configs" and len(route) == 3:
                info = self._token()
                try:
                    gen = self.server.service.generation(int(route[2], 0), info.account)
                except ValueError:
                    gen = None
                if gen is None:
                    raise ApiError(404, "unknown fingerprint")
                self._reply(200, {"fingerprint": gen.fingerprint, "quests": list(gen.quests.values()),
                                  "achievements": list(gen.achievements.values())})
            elif route[1] in ("snapshot", "state") and len(route) == 3:
                info = self._token("read")
                self.server.service.check_token_rate(info.id, "read")
                if not info.can_read(route[2]):  # outside a limited token's characters: as if it were not there
                    raise ApiError(404, "no state for this player")
                if route[1] == "snapshot":
                    found = self.server.service.snapshot(info.account, route[2])
                else:
                    found = self.server.service.raw_state(info.account, route[2])
                if found is None:
                    raise ApiError(404, "no state for this player")
                self._reply(200, found)
            else:
                raise ApiError(404, "no such route")
        except ApiError as e:
            settings = self.server.service.store.settings()
            reply: dict[str, Any] = {"accepted": False, "reason": e.reason, "interval": self.server.service.interval(), **e.extra}
            if e.status == 401:
                # "revoked": the plugin deletes its copy; "unknown"/"no bearer token": it waits retry_after.
                reply["token"] = e.token or "unknown"
                reply["retry_after"] = settings["retry_after"]
            self._reply(e.status, reply)
        except Exception as e:  # never leave the plugin without an answer
            log.exception("unhandled error")
            self._reply(500, {"accepted": False, "reason": f"server error: {type(e).__name__}"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


SD_LISTEN_FDS_START = 3  # systemd's socket activation protocol: inherited sockets start at file descriptor 3


def inherited_socket(environ: Any = None) -> socket.socket | None:
    """The listening socket a service manager passed in (systemd's LISTEN_PID / LISTEN_FDS protocol, as
    sd_listen_fds does), or None when this process was started the ordinary way.

    LISTEN_PID must be this process (a child that merely inherited the variables must not take the socket),
    LISTEN_FDS at least 1; the first socket (fd 3) is used and must be a listening TCP socket. The variables
    are removed so that nothing started from here mistakes them for its own."""
    environ = os.environ if environ is None else environ
    pid, count = environ.get("LISTEN_PID"), environ.get("LISTEN_FDS")
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        environ.pop(name, None)
    if not pid or not count:
        return None
    try:
        if int(pid) != os.getpid() or int(count) < 1:
            return None
    except ValueError:
        return None
    if int(count) > 1:
        log.warning("socket activation passed %s sockets; using the first only", count)
    try:
        sock = socket.socket(fileno=SD_LISTEN_FDS_START)
    except OSError as e:
        raise SystemExit(f"socket activation: LISTEN_FDS is set but file descriptor 3 is not a socket ({e})") from None
    try:
        listening = sock.type == socket.SOCK_STREAM and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    except OSError:
        listening = False
    if not listening or sock.family not in (socket.AF_INET, socket.AF_INET6):
        sock.detach()
        raise SystemExit("socket activation: file descriptor 3 is not a listening TCP socket "
                         "(the .socket unit needs ListenStream=<address>:<port>)")
    sock.set_inheritable(False)
    sock.setblocking(True)
    return sock


def serve(data_dir: Path, listen: str, prefix: str, configs: GameConfigs | None = None,
          captcha: Captcha | None = None, cors_origin: str = CORS_ORIGIN,
          inherited: socket.socket | None = None) -> ApiServer:
    """The server, bound and listening but not yet accepting. With `inherited` (see `inherited_socket`) that
    socket is used and `listen` is ignored."""
    configs = configs or GameConfigs.read()
    if configs is None:
        raise SystemExit("no game configs in the package data: run scripts/import_game_configs.py first")
    host, _, port = listen.rpartition(":")
    # Bind and listen first: building the Service hashes every packaged game config (about half a second, two on
    # the server), and connections that arrive meanwhile wait in the listen backlog instead of being refused
    # (nginx answered 502 during that gap at every restart, 2026-11-13). Nothing is accepted until serve_forever.
    server = ApiServer((host or "127.0.0.1", int(port)), None, prefix, cors_origin, inherited)  # type: ignore[arg-type]
    try:
        server.service = Service(Store(data_dir), configs, captcha=captcha)
    except BaseException:
        server.server_close()
        raise
    return server


def accounts_copy(data_dir: Path, out: Path) -> dict[str, int] | None:
    """Write a copy of the database with everything that came from a plugin taken out, for the long-lived
    backup: accounts, tokens and config sets stay; every character state goes, and a read token limited to
    named characters (the names are the only other plugin-derived data) becomes a bare revoked hash, since a
    copy without the names could only make it unlimited. Restored from such a copy, the service has every
    account and plugin token; each plugin's next post is answered "send the full state" and the states come
    back by themselves. The source is opened read-only and may be the live data dir. None: no database."""
    db_path = data_dir / "export_api.sqlite3"
    if not db_path.is_file():
        return None
    live = db_path.with_name(db_path.name + "-wal").exists()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    src = sqlite3.connect(f"file:{quote(str(db_path.resolve()))}?mode=ro" + ("" if live else "&immutable=1"), uri=True)
    dest = sqlite3.connect(str(out))
    try:
        src.backup(dest)
        dest.execute("PRAGMA journal_mode=DELETE")  # one plain file, no -wal beside it
        today = _today()
        states = dest.execute("DELETE FROM states").rowcount
        if dest.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'token_posts'").fetchone():
            dest.execute("DELETE FROM token_posts")  # links to characters that are no longer in the copy
        limited = dest.execute(f"UPDATE tokens SET revoked = ?, created = ?, {BARE} WHERE revoked IS NULL AND characters IS NOT NULL",
                               (today, today)).rowcount
        dest.commit()
        dest.execute("VACUUM")  # the deleted rows' bytes must not stay in the file
        accounts = dest.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        tokens = dest.execute("SELECT COUNT(*) FROM tokens WHERE revoked IS NULL").fetchone()[0]
    finally:
        dest.close()
        src.close()
    return {"accounts": accounts, "tokens": tokens, "states_left_out": states, "limited_tokens_revoked": limited}


def check_data_dir(data_dir: Path) -> tuple[dict[str, int], list[str]] | None:
    """Verify a data dir without changing it: (counts, problems), or None when there is no database to check.

    Nothing is created or written: the database is opened read-only (as an immutable file when no `-wal` file
    lies beside it, i.e. a backup copy or a stopped service; otherwise read-only through the WAL, which needs
    the directory to be the live one). Checks: SQLite's integrity check, the tables, that every live token and
    every state belongs to an account, that every state record and settings.json parse, and that every config
    set the database refers to (pending and confirmed) and every directory under generations/ is on disk and
    hashes to its name."""
    db_path = data_dir / "export_api.sqlite3"
    if not db_path.is_file():
        return None
    live = db_path.with_name(db_path.name + "-wal").exists()
    counts: dict[str, int] = {}
    problems: list[str] = []
    try:
        db = sqlite3.connect(f"file:{quote(str(db_path.resolve()))}?mode=ro" + ("" if live else "&immutable=1"), uri=True)
        db.row_factory = sqlite3.Row
        rows = [r[0] for r in db.execute("PRAGMA integrity_check")]
        if rows != ["ok"]:
            return counts, [f"integrity_check: {line}" for line in rows[:20]]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = {"accounts", "tokens", "states", "config_contents", "config_refs", "config_confirmed"} - tables
        if missing:
            return counts, [f"missing table(s): {', '.join(sorted(missing))}"]
        one = lambda sql: db.execute(sql).fetchone()[0]
        counts = {"accounts": one("SELECT COUNT(*) FROM accounts"), "players": one("SELECT COUNT(*) FROM states"),
                  "tokens": one("SELECT COUNT(*) FROM tokens WHERE revoked IS NULL"),
                  "revoked_tokens": one("SELECT COUNT(*) FROM tokens WHERE revoked IS NOT NULL")}
        orphans = one("SELECT COUNT(*) FROM tokens WHERE revoked IS NULL AND account NOT IN (SELECT id FROM accounts)")
        if orphans:
            problems.append(f"{orphans} live token(s) without an account")
        orphans = one("SELECT COUNT(*) FROM states WHERE account NOT IN (SELECT id FROM accounts)")
        if orphans:
            problems.append(f"{orphans} character state(s) without an account")
        bad = 0
        for r in db.execute("SELECT record FROM states"):
            try:
                bad += not isinstance(json.loads(r["record"]), dict)
            except (TypeError, ValueError):
                bad += 1
        if bad:
            problems.append(f"{bad} character state(s) that do not parse")
        pending = {r["hash"]: r["fp"] for r in db.execute("SELECT hash, fp FROM config_contents")}
        unknown = one("SELECT COUNT(*) FROM config_refs WHERE hash NOT IN (SELECT hash FROM config_contents)")
        if unknown:
            problems.append(f"{unknown} pending config reference(s) to content the database does not list")
        confirmed = [r["fp"] for r in db.execute("SELECT fp FROM config_confirmed")]
        db.close()
    except sqlite3.Error as e:
        return counts, [f"database unreadable: {e}"]

    def load(folder: Path, what: str) -> GameConfigs | None:
        try:
            configs = GameConfigs.read(folder)
        except Exception as e:  # a truncated gzip, bad JSON, a wrong shape
            problems.append(f"{what}: unreadable ({type(e).__name__})")
            return None
        if configs is None:
            problems.append(f"{what}: files missing")
        return configs

    for content, fp in sorted(pending.items()):
        configs = load(data_dir / "pending_configs" / content, f"pending_configs/{content[:12]}")
        if configs is not None and (configs.content_hash() != content or configs.fingerprint != fp):
            problems.append(f"pending_configs/{content[:12]}: content does not hash to its name and fingerprint {fp:08x}")
    folder = data_dir / "generations"
    on_disk = sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []
    for name in on_disk:
        configs = load(folder / name, f"generations/{name}")
        if configs is not None and f"{configs.fingerprint:08x}" != name:
            problems.append(f"generations/{name}: content has fingerprint {configs.fingerprint:08x}")
    for fp in confirmed:
        if f"{fp:08x}" not in on_disk:
            problems.append(f"generations/{fp:08x}: confirmed in the database, missing on disk")
    counts["generations"], counts["pending_sets"] = len(on_disk), len(pending)
    settings = data_dir / "settings.json"
    if settings.exists():
        try:
            if not isinstance(json.loads(settings.read_text()), dict):
                problems.append("settings.json: not a JSON object")
        except (ValueError, OSError) as e:
            problems.append(f"settings.json: unreadable ({type(e).__name__})")
    return counts, problems


SWEEP_EVERY_SECONDS = 6 * 3600


def _sweep_forever(store: Store) -> None:
    """Expire inactive accounts at start and every few hours (the cutoff has day precision, so no need for more)."""
    while True:
        try:
            store.sweep()
        except Exception:
            log.exception("expiry sweep failed")
        time.sleep(SWEEP_EVERY_SECONDS)


def _account_report(store: Store, account: str, out: Callable[[str], None]) -> None:
    """What an account holds, for the person handling a request: its characters (name, last post, which
    tokens posted for them) and its tokens (first characters, label, scopes, what they post for or may read).
    Names and dates only, never a character's data, a token or a hash."""
    info = store.account_info(account) or {}
    out(f"account {account}: created {info.get('created')}, last used {info.get('last_active')}, "
        f"{info.get('characters')} character(s), {info.get('tokens')} token(s)")
    tokens = store.tokens_of(account)
    for p in store.find_account_players(account):
        by = [f"{t['prefix']}..." for t in tokens if any(x["player"] == p["player"] for x in t["posted"])]
        out(f"  character '{p['player']}', last post {p['last_post']}, posted by: {', '.join(by) or 'no token on record'}")
    for t in tokens:
        does = []
        if t["posted"]:
            does.append("posts for " + ", ".join(x["player"] for x in t["posted"]))
        if "read" in t["scopes"]:
            does.append("may read " + (", ".join(t["characters"]) if t["characters"] else "every character"))
        out(f"  token {t['prefix']}... '{t['label']}' [{', '.join(t['scopes'])}], made {t['created']}, "
            f"last used {t['last_used'] or 'never'}" + ("; " + "; ".join(does) if does else ""))


def _delete_by_secret(store: Store, args: Any, out: Callable[[str], None]) -> int:
    """`request delete --secret`: what the person's key or token proves decides the suggested deletion; the
    operator confirms or overrides it at a prompt (or with --choice).

    - the account key: the whole account;
    - a plugin's (submit) token: the whole account when that token posted for every character the account
      holds (it then proves control of all of it), else only the characters it posted for;
    - a read token: nothing by default (it was made to be handed to a planner, maybe someone else's); the
      choices are the account or the characters it may read.
    Characters are followed by their stored id: a name the person's character now has, or once had, gives no
    hold over an account that is not the token's own."""
    secret = sys.stdin.readline().strip() if args.secret == "-" else args.secret
    token_info = None
    account = store.account_by_key(secret)
    if account is not None:
        proof = "the account key"
    else:
        token_info, status = store.lookup(secret)
        if token_info is None:
            out(f"the secret is not a live key or token ({status})")
            return 1
        account = token_info.account
        proof = "a plugin's (submit) token" if "submit" in token_info.scopes else \
            "a read token" + (" limited to named characters" if token_info.characters is not None else "")
    out(f"the secret is {proof} of account {account}")
    _account_report(store, account, out)
    held = store.find_account_players(account)

    if token_info is None:
        characters, default, why = [p["player"] for p in held], "account", "the key controls the account"
    elif "submit" in token_info.scopes:
        posted = store.posted_by(token_info.id)
        characters = [p["player"] for p in posted]
        covers_all = {p["id"] for p in held} <= {p["id"] for p in posted}
        if covers_all:
            default, why = "account", "this token posted for every character the account holds"
        elif characters:
            default, why = "characters", "this token posted for only some of the account's characters"
        else:
            default, why = "none", "this token has not posted for any character on record"
    else:
        characters = [p["player"] for p in held if token_info.can_read(p["player"])]
        default, why = "none", "a read token shows what someone may read, not whose the data is"

    out("choices: account (everything, with all its tokens); characters ("
        + (", ".join(f"'{c}'" for c in characters) or "none to delete") + "); none")
    out(f"suggested: {default} ({why})")
    choice = args.choice
    if choice is None:
        try:
            typed = input(f"request: delete account / characters / none? [{default}] ").strip().lower()
        except EOFError:
            typed = ""
        choice = {"a": "account", "c": "characters", "n": "none", "": default}.get(typed, typed)
    if choice not in ("account", "characters", "none"):
        out(f"not a choice: {choice!r}; nothing deleted")
        return 2
    if choice == "none":
        out("nothing deleted")
        return 0
    if store.deletions_file is None:
        out("warning: no deletion log place is set (--deletions DIR, as the service has): a restore from a backup "
            "would bring this data back")
    if choice == "account":
        gone = store.delete_account(account)
        out(f"deleted account {account}: {gone['characters']} character(s), {gone['tokens']} token(s)")
        return 0
    for name in characters:
        store.delete_state(account, name)
    out(f"deleted {len(characters)} character(s) from account {account}: " + (", ".join(characters) or "none"))
    if token_info is not None and "submit" in token_info.scopes and characters:
        out("the token still works, so its plugin will post the character again: revoke it "
            "(`token revoke <its first 6 characters>`) if the person wants the posting to stop")
    return 0


def _data_request(store: Store, args: Any) -> int:
    """`request find | export | delete | delete-player | delete-account`: exit 0 done (or shown), 1 nothing
    matched, 2 ambiguous or refused. `delete` works from a key or token the person sent and asks what to
    delete; `delete-player` and `delete-account` are the operator's own decision by name or account id and
    need --yes (without it they print what they would do). An export goes to a file only: a character's data
    is never printed."""
    out = lambda text: print(f"request: {text}")
    if args.request_command == "delete":
        return _delete_by_secret(store, args, out)
    account, proof = args.account, None
    if args.secret:
        secret = sys.stdin.readline().strip() if args.secret == "-" else args.secret
        found, proof = store.account_of_secret(secret)
        if found is None:
            out(f"the secret is not a live key or token ({proof})")
            return 1
        if account and account != found:
            out(f"the secret belongs to account {found}, not {account}")
            return 2
        account = found
        out(f"the secret is a {proof} of account {account}")
    player = getattr(args, "player", None)
    matches = [m for m in store.find_player(player) if account in (None, m["account"])] if player else []
    if args.request_command == "find":
        if not player and not account:
            out("give --player, --account or --secret")
            return 2
        if player:
            for m in matches:
                out(f"account {m['account']} holds '{m['player']}', last post {m['last_post']}")
            if not matches:
                out("no character of that name" + (f" in account {account}" if account else ""))
                return 1
            for found_account in sorted({m["account"] for m in matches}):
                _account_report(store, found_account, out)
        elif store.account_info(account) is None:
            out(f"no account {account}")
            return 1
        else:
            _account_report(store, account, out)
        return 0
    if args.request_command == "export":
        return _export_request(store, args, account, proof, matches, out)
    if proof is not None and proof != "account key":
        out("refused: with a token as the proof use `request delete --secret`, which works out what the token "
            "covers; this command acts on a name or an account id by the operator's own decision")
        return 2
    if store.deletions_file is None:
        out("warning: no deletion log place is set (--deletions DIR, as the service has): a restore from a backup "
            "would bring this data back")
    if args.request_command == "delete-player":
        if not matches:
            out("no character of that name" + (f" in account {account}" if account else ""))
            return 1
        if len(matches) > 1:
            for m in matches:
                out(f"account {m['account']} holds '{m['player']}', last post {m['last_post']}")
            out("more than one account holds that name: say which with --account")
            return 2
        target = matches[0]
        if not args.yes:
            out(f"would delete '{target['player']}' from account {target['account']} (add --yes)")
            return 0
        store.delete_state(target["account"], target["player"])
        out(f"deleted '{target['player']}' from account {target['account']}")
        return 0
    if not account or store.account_info(account) is None:
        out("no such account" if account else "give --account or --secret")
        return 1 if account else 2
    info = store.account_info(account) or {}
    if not args.yes:
        out(f"would delete account {account} with {info['characters']} character(s) and {info['tokens']} token(s) (add --yes)")
        return 0
    gone = store.delete_account(account)
    out(f"deleted account {account}: {gone['characters']} character(s), {gone['tokens']} token(s)")
    return 0


def _export_request(store: Store, args: Any, account: str | None, proof: str | None,
                    matches: list[dict[str, Any]], out: Callable[[str], None]) -> int:
    """Everything stored for an account, or with --player for one character, as one JSON file: the account's
    dates, its live tokens as the account page shows them (never a token or hash), and per character the
    snapshot the planner reads. Nothing else is held about an account."""
    if proof == "limited read token":
        out("refused: a read token limited to named characters does not speak for the account "
            "(its holder can read those characters through the API)")
        return 2
    if args.player:
        if not matches:
            out("no character of that name" + (f" in account {account}" if account else ""))
            return 1
        if len(matches) > 1:
            for m in matches:
                out(f"account {m['account']} holds '{m['player']}', last post {m['last_post']}")
            out("more than one account holds that name: say which with --account or --secret")
            return 2
        account, names = matches[0]["account"], [matches[0]["player"]]
    elif not account:
        out("give --player, --account or --secret")
        return 2
    elif store.account_info(account) is None:
        out(f"no account {account}")
        return 1
    else:
        names = [p["player"] for p in store.players(account)]
    what = f"'{names[0]}' of account {account}" if args.player else f"account {account} with {len(names)} character(s)"
    if not args.yes:
        out(f"would write {what} to {args.out} (add --yes)")
        return 0
    configs = GameConfigs.read()
    if configs is None:
        out("no game configs in the package data")
        return 2
    service = Service(store, configs)
    export: dict[str, Any] = {"exported": _today(), "service": "Rune Advisor export API"}
    if not args.player:
        export["account"] = store.account_info(account)
        export["tokens"] = [{k: v for k, v in t.items() if k != "owner"} for t in store.tokens_of(account)]
    export["characters"] = []
    for name in names:
        try:
            export["characters"].append(service.snapshot(account, name))
        except Exception as e:  # a state that cannot be decoded here still belongs to the person: hand over the record
            export["characters"].append({"player": name, "undecoded": type(e).__name__, "record": store.load_state(account, name)})
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=1)
    os.chmod(args.out, 0o600)
    out(f"wrote {what} to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deletions", type=Path, default=None,
                    help="a directory outside the data dir for the deletion log (backed up with it), so deletions "
                         f"are applied again after a restore (default: ${DELETIONS_ENV}, else off)")
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("RUNE_ADVISOR_API_DATA", "./export_api_data")))
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="run the HTTP service")
    s.add_argument("--listen", default=os.environ.get("RUNE_ADVISOR_API_LISTEN", "127.0.0.1:8765"))
    s.add_argument("--cors-origin", default=CORS_ORIGIN,
                   help="the one web origin whose pages may call the API from a browser ('' = none)")
    s.add_argument("--prefix", default=os.environ.get("RUNE_ADVISOR_API_PREFIX", "/export_api"))
    s.add_argument("--turnstile-secret-file", type=Path, default=None,
                   help="a file holding the Cloudflare Turnstile secret; with it account creation requires the bot "
                        f"check (default: ${TURNSTILE_FILE_ENV}, else the systemd credential '{TURNSTILE_CREDENTIAL}')")
    s.add_argument("--configs", type=Path, help="a game_configs directory to serve with instead of the package dump "
                                                "(e.g. an older one, to exercise the 428 config sync)")
    t = sub.add_parser("token", help="manage API tokens")
    tsub = t.add_subparsers(dest="token_command", required=True)
    new = tsub.add_parser("new", help="create a token for an account (made if missing)")
    new.add_argument("owner")
    new.add_argument("--scopes", default=",".join(SCOPES), help="comma-separated: submit, read (default both)")
    new.add_argument("--label", default="")
    new.add_argument("--characters", help="comma-separated characters a read token is limited to")
    add = tsub.add_parser("add", help="register an existing token value (one minted on another deployment)")
    add.add_argument("owner")
    add.add_argument("token")
    tsub.add_parser("list", help="list tokens (hash prefixes only)")
    tsub.add_parser("revoke", help="revoke a token by value or its first 6 characters").add_argument("token")
    acc = sub.add_parser("account", help="manage accounts")
    asub = acc.add_subparsers(dest="account_command", required=True)
    asub.add_parser("list")
    asub.add_parser("key", help="issue (or replace) an account's key, for accounts made by `token new`").add_argument("account")
    asub.add_parser("delete", help="delete an account with its tokens and states").add_argument("account")
    rq = sub.add_parser("request", help="a data request made by hand (an email to privacy@): find or delete by "
                                        "character name, or by an account key or token the person sends")
    rsub = rq.add_subparsers(dest="request_command", required=True)
    for name, text in (("find", "which account(s) match; shows ids and days only, never a character's data"),
                       ("export", "write everything stored for the account (or one character) to a file, for a "
                                  "person's request for their data"),
                       ("delete-player", "delete one character's state"),
                       ("delete-account", "delete the account with its tokens and every character")):
        r = rsub.add_parser(name, help=text)
        if name != "delete-account":
            r.add_argument("--player", required=name == "delete-player", help="the character's name (case-insensitive)")
        if name == "export":
            r.add_argument("--out", type=Path, required=True, help="the JSON file to write (readable by its owner only)")
        r.add_argument("--account", help="the account id (from `request find`)")
        r.add_argument("--secret", help="an account key or token the person sent; '-' reads it from standard input, "
                                        "which keeps it out of the shell history")
        if name != "find":
            r.add_argument("--yes", action="store_true", help="do it; without this the command only says what it would do")
    rd = rsub.add_parser("delete", help="delete on the strength of a key or token the person sent: shows the account's "
                                        "characters and tokens, suggests what that secret covers, and asks")
    rd.add_argument("--secret", required=True, help="the account key or token; '-' reads it from standard input")
    rd.add_argument("--choice", choices=("account", "characters", "none"),
                    help="answer the question in advance instead of being asked")
    cf = sub.add_parser("configs", help="config sets clients uploaded after a game update (pending until confirmed)")
    csub = cf.add_subparsers(dest="configs_command", required=True)
    csub.add_parser("list", help="global and pending sets with how many accounts uploaded identical content")
    conf = csub.add_parser("confirm", help="make a pending set global (it takes effect in a running server)")
    conf.add_argument("fingerprint", help="hex, as `configs list` prints it")
    conf.add_argument("--hash", help="content hash (or a prefix) when different sets share the fingerprint")
    csub.add_parser("drop", help="forget the pending sets with a fingerprint").add_argument("fingerprint")
    sub.add_parser("sweep", help="delete accounts inactive for longer than retention_days now")
    dl = sub.add_parser("deletions", help="the deletion log")
    dsub = dl.add_subparsers(dest="deletions_command", required=True)
    dsub.add_parser("merge", help="during a restore, before starting the service: add the entries of other copies "
                                  "of the log (from every backup newer than the one restored), then apply them"
                    ).add_argument("files", nargs="+", type=Path)
    sub.add_parser("migrate", help="import tokens.json and players/ from a data dir written before SQLite")
    st = sub.add_parser("settings", help="load control, quotas, retention, account-creation guards")
    ssub = st.add_subparsers(dest="settings_command", required=True)
    ssub.add_parser("show")
    sset = ssub.add_parser("set")
    sset.add_argument("key", choices=sorted([*DEFAULT_SETTINGS, TURNSTILE_CREDENTIAL]))  # the last only to explain itself
    sset.add_argument("value", help="a number")
    ssub.add_parser("unset", help="take a setting out of the file, so it follows the default again"
                    ).add_argument("key", choices=sorted(DEFAULT_SETTINGS))
    ssub.add_parser("chosen", help="list the settings the file sets (everything else follows the defaults)")
    sub.add_parser("accounts-copy", help="write a copy of the database without character states (and without "
                                         "name-limited read tokens) for the long-lived backup; reads only"
                   ).add_argument("out", type=Path, help="the file to write, e.g. /backup/stage/export_api.sqlite3")
    sub.add_parser("check", help="verify a data dir (a restored backup, a copy) without writing to it: "
                                 "exit 0 ok, 1 problems found, 2 nothing to check")
    stats = sub.add_parser("stats", help="characters that posted recently")
    stats.add_argument("--minutes", type=float, default=10)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.deletions:
        os.environ[DELETIONS_ENV] = str(args.deletions)
    # Only `serve` takes over a socket; every command clears the variables so they are not passed on.
    passed = inherited_socket()
    if passed is not None and args.command != "serve":
        passed.detach()
        passed = None
    if args.command == "accounts-copy":  # before Store(): read-only on the data dir
        made = accounts_copy(args.data_dir, args.out)
        if made is None:
            print(f"accounts-copy: no export_api.sqlite3 in {args.data_dir}")
            return 2
        for key, value in made.items():
            print(f"{key} = {value}")
        print("accounts-copy: ok")
        return 0
    if args.command == "check":  # before Store(): opening the store would create and write the data dir
        result = check_data_dir(args.data_dir)
        if result is None:
            print(f"check: no export_api.sqlite3 in {args.data_dir}")
            return 2
        counts, problems = result
        for key, value in counts.items():
            print(f"{key} = {value}")
        for problem in problems:
            print(f"problem: {problem}")
        print(f"check: FAILED ({len(problems)} problem(s))" if problems else "check: ok")
        return 1 if problems else 0
    store = Store(args.data_dir)
    if args.command == "token":
        if args.token_command == "new":
            scopes = [x for x in args.scopes.split(",") if x]
            if not scopes or any(x not in SCOPES for x in scopes):
                print(f"scopes must be from: {', '.join(SCOPES)}", file=sys.stderr)
                return 2
            characters = [c.strip() for c in args.characters.split(",") if c.strip()] if args.characters else None
            print(store.new_token(args.owner, args.label, scopes, characters))
        elif args.token_command == "add":
            try:
                store.add_token(args.owner, args.token)
            except ValueError as e:
                print(e, file=sys.stderr)
                return 2
            print(f"registered {args.token[:6]}... for {args.owner}")
        elif args.token_command == "list":
            for e in store.list_tokens():
                print(f"{e['prefix']}...  {e['owner']}  [{','.join(e['scopes'])}]  {e['label']!r}  created {e['created']}"
                      f"  last used {e['last_used'] or 'never'}"
                      + (f"  limited to {', '.join(e['characters'])}" if e["characters"] else "")
                      + (f"  REVOKED {e['revoked']}" if e.get("revoked") else ""))
        else:
            print(f"revoked {store.revoke(args.token)} token(s)")
        return 0
    if args.command == "account":
        if args.account_command == "list":
            for a in store.accounts():
                print(f"{a['id']}  created {a['created']}  active {a['last_active']}  {a['characters']} character(s)  "
                      f"{a['tokens']} token(s)  {'key' if a['has_key'] else 'no key'}")
        elif args.account_command == "key":
            print(store.set_account_key(args.account))
        else:
            print(store.delete_account(args.account))
        return 0
    if args.command == "request":
        return _data_request(store, args)
    if args.command == "configs":
        if args.configs_command == "list":
            for r in store.config_rows():
                diff = (f"+{r['q_added']}/-{r['q_removed']} quests  +{r['a_added']}/-{r['a_removed']} achievements"
                        if r["q_added"] is not None else "(no diff recorded)")
                print(f"{r['status']:<7} {r['fp']:08x}  {(r['hash'] or '-')[:12]:<12}  {r['day']}  "
                      f"{'' if r['accounts'] is None else str(r['accounts']) + ' account(s)  '}{diff}"
                      + (f"  base {r['base']:08x}" if r["base"] is not None else ""))
            return 0
        try:
            fp = int(args.fingerprint, 16)
            if args.configs_command == "confirm":
                print(f"{fp:08x} is global now ({store.confirm_config(fp, args.hash)[:12]})")
            else:
                print(f"dropped {store.drop_config(fp)} reference(s) to {fp:08x}")
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        return 0
    if args.command == "sweep":
        print(f"expired {len(store.sweep())} account(s)")
        return 0
    if args.command == "deletions":
        missing = [str(f) for f in args.files if not f.is_file()]
        if missing or store.deletions_file is None:
            print("deletions merge: " + ("no such file: " + ", ".join(missing) if missing else "no log place (--deletions DIR)"))
            return 2
        added = store.merge_deletions(args.files)
        print(f"deletions merge: {added} new entr{'y' if added == 1 else 'ies'}, "
              f"{store.reapply_deletions()} deletion(s) applied again")
        return 0
    if args.command == "migrate":
        print(store.migrate_legacy())
        return 0
    if args.command == "settings":
        if args.settings_command == "set":
            try:
                if args.key == TURNSTILE_CREDENTIAL:
                    store.set_setting(args.key, 0)  # refused, with where the secret goes instead
                store.set_setting(args.key, float(args.value))
            except ValueError as e:
                print(e, file=sys.stderr)
                return 2
        if args.settings_command == "unset":
            store.unset_setting(args.key)
        if args.settings_command == "chosen":
            for key in sorted(store.chosen_settings()):
                print(key)
            return 0
        for key, value in store.settings_lines().items():
            print(f"{key} = {value}")
        return 0
    if args.command == "stats":
        for key, value in store.stats(args.minutes).items():
            print(f"{key} = {value}")
        return 0
    configs = GameConfigs.read(args.configs) if args.configs else None
    if args.configs and configs is None:
        print(f"no quests/achievements dump in {args.configs}", file=sys.stderr)
        return 2
    secret, secret_source = turnstile_secret(args.turnstile_secret_file)  # stops the start if named but unusable
    store.note_turnstile(secret_source)
    store.close()  # serve() opens its own
    server = serve(args.data_dir, args.listen, args.prefix, configs=configs, cors_origin=args.cors_origin,
                   inherited=passed, captcha=TurnstileCaptcha(secret) if secret else None)
    log.info("account creation: %s", f"Turnstile check on (secret from {secret_source})" if secret else "no bot check (no Turnstile secret file)")
    threading.Thread(target=_sweep_forever, args=(server.service.store,), daemon=True).start()
    where = "%s:%s" % server.server_address[:2]
    log.info("export API listening on %s%s, routes under %s, data in %s", where,
             " (socket passed in by the service manager; --listen ignored)" if server.inherited else "",
             server.prefix or "/", args.data_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
