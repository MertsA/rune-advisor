"""Paths and settings. Override via environment variables or ./rune_advisor.toml."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "RuneAdvisor/0.1 (RuneScape planning tool; https://runeadvisor.com; support@runeadvisor.com)"

# The clients' plugin PersistentDB for a Steam install in Steam's default library. The Steam build keeps its
# cache/user folder inside the game folder (steamapps/common/RuneScape/RuneScape), and the plugin beta world
# (where dev plugins run) keeps its own beside it. A library elsewhere: `plugin_db` / `beta_plugin_db` in
# rune_advisor.toml, or RUNE_ADVISOR_PLUGIN_DB. The Windows location is the installer's default, not verified.
_STEAM = (Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Steam" if sys.platform == "win32"
          else Path.home() / ".local" / "share" / "Steam")
_STEAM_RS = _STEAM / "steamapps" / "common" / "RuneScape"
DEFAULT_PLUGIN_DB = _STEAM_RS / "RuneScape" / "plugins" / "PluginData.db"
BETA_PLUGIN_DB = _STEAM_RS / "RuneScape-BETA" / "plugins" / "PluginData.db"

CACHE_DIR = Path(os.environ.get("RUNE_ADVISOR_CACHE", Path.home() / ".cache" / "rune-advisor"))
# Kept state (archived snapshots, seen outfit pieces, the tile store, the API snapshot cache). It lives in the
# checkout (state/, gitignored) so planner runs inside the agentbox sandbox and on the host share it; it was
# ~/.local/share/rune-advisor until 2026-11-04 and would move back there if this were ever packaged.
STATE_DIR = Path(os.environ.get("RUNE_ADVISOR_STATE", Path(__file__).resolve().parents[2] / "state"))
ARCHIVE_DIR = STATE_DIR / "archive"


def _default_data_home() -> Path:
    """Where installed data packages live: %LOCALAPPDATA% on Windows, the XDG data dir elsewhere."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "rune-advisor" / "data"


# Installed data packages (rune_advisor.datapkg), apart from the kept planner state above.
DATA_HOME = Path(os.environ.get("RUNE_ADVISOR_DATA_HOME") or _default_data_home())


@dataclass
class Settings:
    plugin_db: Path = DEFAULT_PLUGIN_DB
    # PersistentDB PluginIDs of our plugins: the client uses the manifest "name". The exporter (snapshots,
    # inventories) is the one meant for publication; the dev plugin (sessions, config and name dumps) only
    # ever runs as a dev plugin on the beta.
    export_plugin_id: str = "Rune Advisor Export"
    dev_plugin_id: str = "Rune Advisor Dev"
    # Per-skill xp/hr overrides, e.g. {"Hunter": 900_000}.
    xp_rates: dict[str, float] = field(default_factory=dict)
    # Relative weights when merging tracks into the overall ranking.
    track_weights: dict[str, float] = field(default_factory=dict)
    # [skill_weights] Construction = 0.3: how much a skill's xp is worth to the free_xp track relative to others
    # (the user's call on usefulness: Construction unlocks furniture, Herblore potions every plan drinks).
    skill_weights: dict[str, float] = field(default_factory=dict)
    coins_per_hour: float | None = None
    # Hours a missing collection-log unique is worth to the player (netted off a boss log's cost). 0 (the
    # default, the user's call): uniques are listed, not valued.
    unique_value_hours: float = 0.0
    prefer_exquisite_urns: bool = False  # use exquisite urns whenever they can be made, cost aside  # money-making rate for coins as time (default gathering.COINS_PER_HOUR)
    # Extra/stocked xp resources ([[resources]] tables), see data/xp_resources.toml.
    resources: list[dict] = field(default_factory=list)
    # Training methods the wiki guides don't have yet ([[methods]] tables), see CostModel.default.
    methods: list[dict] = field(default_factory=list)
    # Farming patch assumptions per patch type ([farming."Herb patch"] patches/yield/run_minutes).
    farming: dict[str, dict] = field(default_factory=dict)
    # Ironman status for snapshot-only planning (otherwise detected from the ironman hiscores).
    ironman: bool = False
    # Plan for a free-to-play account when False (rune_advisor.toml `members = false`, or --f2p).
    # None = decide from the plugin snapshot's world type (free world -> F2P), else members.
    members: bool | None = None
    # Group Ironman: everyone in the group (rune_advisor.toml `group = ["PlayerOne", "PlayerTwo"]`).
    group: list[str] = field(default_factory=list)
    # Per-player settings ([players.<name>] tables), see PlayerSettings.
    players: dict[str, "PlayerSettings"] = field(default_factory=dict)
    # The export API ([api] url = "https://api.runeadvisor.com", token = "..."): where the
    # published exporter posts to, and where the planner fetches snapshots from when the local plugin DB
    # has none (rune_advisor.api_client). `token` is a read token (or, on older deployments, the one pasted into
    # the plugin's panel); `tokens = [...]` lists several, e.g. a group mate's one-character token, and a
    # character's snapshot is looked up across them.
    api_url: str | None = None
    api_tokens: list[str] = field(default_factory=list)

    @property
    def api_token(self) -> str | None:
        """The first API token, or None: enough to know the API is configured."""
        return self.api_tokens[0] if self.api_tokens else None
    # Archived snapshots (state/archive): where inventories a fresh export hasn't loaded are looked up
    # (player.fill_stale_inventories). Tests point it at a scratch folder.
    archive_dir: Path = ARCHIVE_DIR
    # Data package updates ([data] url = "https://host/base", check_updates = true; env RUNE_ADVISOR_DATA_URL).
    # With no url nothing ever calls out; check_updates = false turns checks off even with one.
    data_url: str | None = None
    check_updates: bool = True

    def player(self, name: str) -> "PlayerSettings":
        return self.players.get(name.lower(), PlayerSettings())

    def in_group(self, name: str) -> bool:
        return name.lower() in {m.lower() for m in self.group}


@dataclass
class PlayerSettings:
    # Overrides Settings.members for this player (e.g. a free-to-play groupmate).
    members: bool | None = None
    # A plugin snapshot JSON file to use when the plugin DB has none for this player (an archived
    # export, or one a groupmate sent over).
    snapshot: Path | None = None


def _path(value: str, config: Path) -> Path:
    """A path from rune_advisor.toml: ~ expanded, relative ones taken from the file's folder (state/..., .plugin_db/...)."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else config.parent / p


def load_settings(path: Path | None = None, beta: bool = False) -> Settings:
    path = path or Path(os.environ.get("RUNE_ADVISOR_CONFIG", "rune_advisor.toml"))
    s = Settings()
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    if raw:
        if "plugin_db" in raw:
            s.plugin_db = _path(raw["plugin_db"], path)
        s.export_plugin_id = raw.get("export_plugin_id", s.export_plugin_id)
        s.dev_plugin_id = raw.get("dev_plugin_id", s.dev_plugin_id)
        s.xp_rates = raw.get("xp_rates", {})
        s.track_weights = raw.get("track_weights", {})
        s.skill_weights = {k: float(v) for k, v in raw.get("skill_weights", {}).items()}
        s.coins_per_hour = raw.get("coins_per_hour")
        s.unique_value_hours = float(raw.get("unique_value_hours", 0.0))
        s.prefer_exquisite_urns = bool(raw.get("prefer_exquisite_urns", False))
        s.resources = raw.get("resources", [])
        s.methods = raw.get("methods", [])
        s.farming = raw.get("farming", {})
        s.ironman = bool(raw.get("ironman", False))
        if "members" in raw:
            s.members = bool(raw["members"])
        s.group = list(raw.get("group", []))
        api = raw.get("api", {})
        s.api_url = api.get("url") or os.environ.get("RUNE_ADVISOR_API_URL")
        listed = [t for t in [api.get("token"), *api.get("tokens", [])] if isinstance(t, str) and t]
        env = os.environ.get("RUNE_ADVISOR_API_TOKEN")
        s.api_tokens = list(dict.fromkeys(listed or ([env] if env else [])))
        data = raw.get("data", {})
        s.data_url = data.get("url")
        s.check_updates = bool(data.get("check_updates", True))
        for name, p in raw.get("players", {}).items():
            s.players[name.lower()] = PlayerSettings(
                members=bool(p["members"]) if "members" in p else None,
                snapshot=_path(p["snapshot"], path) if "snapshot" in p else None,
            )
    s.data_url = os.environ.get("RUNE_ADVISOR_DATA_URL") or s.data_url
    if env_db := os.environ.get("RUNE_ADVISOR_PLUGIN_DB"):
        s.plugin_db = Path(env_db)
    if beta:
        s.plugin_db = _path(raw["beta_plugin_db"], path) if "beta_plugin_db" in raw else BETA_PLUGIN_DB
    return s
