"""Data files, shipped apart from the app: one accessor, a signed package format and its update logic.

Why: the planner's tables (19 MB under rune_advisor/data) change more often than its code, so they travel as a
signed `rune-advisor-data-<format>-<version>.tar.xz` that users install without a rebuilt, re-signed app. Every
reader asks `path(name)` / `load_json(name)`; the answer comes from the active package, else from the data
bundled in the wheel, so behaviour with no package installed is exactly the bundled data's.

Layout under `config.DATA_HOME`: `packages/<version>/` (unpacked, with manifest.json) and `state.json`
({"current", "previous"}), switched atomically with os.replace. Trust: a package is accepted only when its
detached Ed25519 signature (over the whole archive) verifies against `PUBLIC_KEYS` *before* anything is
unpacked; unpacking then takes regular files only, with no traversal, and checks every size and sha256
against the manifest. Only packages of this app's `DATA_FORMAT` are taken.

Switching: the active package is resolved once per process (`reset()` re-resolves). `apply`/`rollback`
change what the *next* start uses, never what this process already read, so module caches (`@cache`) can
never mix bundled and packaged data. See docs/data-package.md.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from . import config

log = logging.getLogger(__name__)

# Bumped only when the planner's logic needs a different data shape; an app takes only packages of its own.
DATA_FORMAT = 1
# Trusted release keys, base64 of the raw 32-byte Ed25519 public key. Empty until the first release key is
# generated (`scripts/build_data_package.py --keygen`); with none, no package verifies.
PUBLIC_KEYS: list[str] = []

BUNDLED = Path(__file__).resolve().parent / "data"
MANIFEST = "manifest.json"
NOTICE = "NOTICE"
# The version of the data bundled with the app: the package version the tree was last released as
# (`scripts/build_data_package.py` writes it). An installed package older than this is not used, so an app
# upgrade is never held back by a stale package.
BUNDLED_VERSION_FILE = "VERSION"
# Never packaged: the data repository's own files (the data is a git submodule: `.git` there is a file).
# Quest and achievement configs read from the game client (`game_configs/`) are packaged since 2026-11-16:
# they are Jagex's, the package's notice says so, and no licence of ours covers them.
UNPACKAGED = (BUNDLED_VERSION_FILE, ".git", "LICENSE", "NOTICE.md", "README.md")
MAX_MEMBER_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 600 * 1024 * 1024
MAX_MEMBERS = 500
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
_VERSION = re.compile(r"^\d+(\.\d+){0,3}$")


class PackageError(Exception):
    """A package that must not be installed (bad signature, format, hash, member...)."""


# ---- version / names ---------------------------------------------------------------------------------------

def version_key(version: str) -> tuple[int, ...]:
    if not _VERSION.match(version):
        raise PackageError(f"bad version {version!r} (want digits and dots, e.g. 2026.11.12.1)")
    return tuple(int(p) for p in version.split("."))


def package_name(version: str, fmt: int = DATA_FORMAT) -> str:
    return f"rune-advisor-data-{fmt}-{version}.tar.xz"


# ---- bundled/package resolution ------------------------------------------------------------------------------

@dataclass
class Active:
    root: Path
    version: str | None  # None: the bundled data
    source: str  # "package", "previous" (fell back) or "bundled"
    notes: list[str]


_active: Active | None = None


def _state_file() -> Path:
    return config.DATA_HOME / "state.json"


def read_state() -> dict[str, str | None]:
    try:
        raw = json.loads(_state_file().read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    return {"current": raw.get("current"), "previous": raw.get("previous")}


def _write_state(state: dict[str, str | None]) -> None:
    config.DATA_HOME.mkdir(parents=True, exist_ok=True)
    tmp = _state_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, _state_file())  # atomic: readers see the old or the new pointer, never half


def package_dir(version: str) -> Path:
    return config.DATA_HOME / "packages" / version


def _loadable(version: str | None) -> tuple[Path | None, str]:
    """(folder, why not): an installed package whose manifest is of our format and whose files are all there
    at the recorded size. Hashes were checked at install; this catches a damaged or half-deleted folder."""
    if not version or not _VERSION.match(version):
        return None, "no package"
    root = package_dir(version)
    try:
        manifest = json.loads((root / MANIFEST).read_text())
        if manifest.get("format") != DATA_FORMAT or manifest.get("version") != version:
            return None, f"{version}: wrong format or version in manifest"
        for name, meta in manifest["files"].items():
            if (root / name).stat().st_size != meta["size"]:
                return None, f"{version}: {name} has the wrong size"
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return None, f"{version}: {e}"
    return root, ""


def bundled_version() -> str:
    """The bundled data's version, "0" when the tree was never released as a package."""
    try:
        text = (BUNDLED / BUNDLED_VERSION_FILE).read_text().strip()
    except OSError:
        return "0"
    return text if _VERSION.match(text) else "0"


def resolve() -> Active:
    """The package to read from: current, else previous, else the bundled data. RUNE_ADVISOR_BUNDLED_DATA=1
    forces the bundled data (the build scripts use it so they read the tree they are regenerating)."""
    if os.environ.get("RUNE_ADVISOR_BUNDLED_DATA"):
        return Active(BUNDLED, None, "bundled", [])
    state, notes = read_state(), []
    for slot, source in (("current", "package"), ("previous", "previous")):
        root, why = _loadable(state[slot])
        if root is not None and version_key(state[slot]) <= version_key(bundled_version()):
            notes.append(f"{slot} package {state[slot]} is not newer than the bundled data ({bundled_version()})")
            continue
        if root is not None:
            return Active(root, state[slot], source, notes)
        if state[slot]:
            notes.append(f"{slot} package unusable ({why})")
    return Active(BUNDLED, None, "bundled", notes)


def active() -> Active:
    global _active
    if _active is None:
        _active = resolve()
        for n in _active.notes:
            log.warning("data package: %s", n)
    return _active


def reset() -> None:
    """Re-resolve on next use. Module caches are not cleared: switching mid-process is not supported, so
    this is for tests and for tools that start from a clean interpreter state."""
    global _active
    _active = None


def _under(root: Path, name: str) -> Path:
    if not name or name.startswith("/") or "\\" in name or ".." in Path(name).parts:
        raise ValueError(f"bad data file name {name!r}")
    return root / name


def path(name: str) -> Path:
    """Where data file (or folder) `name` is: the active package's copy if it has one, else the bundled one
    (which may not exist: callers that tolerate a missing file test `.is_file()` as before)."""
    a = active()
    if a.root is not BUNDLED:
        p = _under(a.root, name)
        if p.exists():
            return p
    return _under(BUNDLED, name)


def exists(name: str) -> bool:
    return path(name).exists()


def read_text(name: str) -> str:
    return path(name).read_text(encoding="utf-8")


def load_json(name: str) -> Any:
    """Parsed JSON of a data file. A packaged copy that fails to parse falls back to the bundled one."""
    p = path(name)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if BUNDLED not in p.parents:
            log.warning("data package: %s unreadable, using the bundled copy", name)
            return json.loads(_under(BUNDLED, name).read_text(encoding="utf-8"))
        raise


def load_toml(name: str) -> dict:
    p = path(name)
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if BUNDLED not in p.parents:
            log.warning("data package: %s unreadable, using the bundled copy", name)
            return tomllib.loads(_under(BUNDLED, name).read_text(encoding="utf-8"))
        raise


def version() -> str:
    """The active package's version, or "bundled"."""
    return active().version or "bundled"


# ---- signing -----------------------------------------------------------------------------------------------------

def generate_keypair() -> tuple[str, str]:
    """(private, public) as base64 of the raw 32-byte keys."""
    key = Ed25519PrivateKey.generate()
    raw_priv = key.private_bytes_raw()
    raw_pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw_priv).decode(), base64.b64encode(raw_pub).decode()


def sign(data: bytes, private_b64: str) -> str:
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))
    return base64.b64encode(key.sign(data)).decode()


def verify(data: bytes, signature_b64: str, keys: list[str] | None = None) -> None:
    """Raise PackageError unless `signature_b64` is a valid signature of `data` by one of the trusted keys."""
    keys = PUBLIC_KEYS if keys is None else keys
    try:
        sig = base64.b64decode(signature_b64.strip(), validate=True)
    except ValueError as e:
        raise PackageError(f"signature is not base64: {e}") from None
    for k in keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(k)).verify(sig, data)
            return
        except InvalidSignature:
            continue
    raise PackageError("signature does not verify against a trusted key" if keys else "no trusted keys in this app")


# ---- building ------------------------------------------------------------------------------------------------------

def data_files(root: Path = BUNDLED, exclude: tuple[str, ...] = UNPACKAGED) -> list[str]:
    """Posix names of the data files under `root` (no python, no caches, nothing in `exclude`), sorted."""
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and "__pycache__" not in rel.parts and p.suffix != ".py" \
                and not any(rel.as_posix() == e or rel.as_posix().startswith(e) for e in exclude if e):
            out.append(rel.as_posix())
    return out


def build(out_dir: Path, version: str, private_b64: str, notice: str, root: Path = BUNDLED,
          fmt: int = DATA_FORMAT) -> tuple[Path, Path]:
    """Write `rune-advisor-data-<fmt>-<version>.tar.xz` and its `.sig` into `out_dir`."""
    version_key(version)
    files = {}
    for name in data_files(root):
        blob = (root / name).read_bytes()
        files[name] = {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}
    manifest = {"format": fmt, "version": version, "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": files}
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / package_name(version, fmt)
    with tarfile.open(archive, "w:xz", preset=6) as tar:
        def add(name: str, blob: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(blob), 0o644, 0
            tar.addfile(info, io.BytesIO(blob))
        add(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True).encode())
        add(NOTICE, notice.encode())
        for name in files:
            add(name, (root / name).read_bytes())
    sig = archive.with_name(archive.name + ".sig")
    sig.write_text(sign(archive.read_bytes(), private_b64) + "\n")
    return archive, sig


# ---- installing ----------------------------------------------------------------------------------------------------

def _safe_member_name(name: str) -> str:
    parts = Path(name).parts
    if not name or name.startswith("/") or "\\" in name or "\0" in name or not parts \
            or any(p in ("..", ".", "") for p in parts) or Path(name).is_absolute():
        raise PackageError(f"unsafe member name {name!r}")
    return "/".join(parts)


def unpack(archive_bytes: bytes, dest: Path) -> dict:
    """Extract an already-verified archive into the new folder `dest` and return its manifest. Regular files
    only (no links, devices, directories entries), safe names, size caps, and every file must match the
    manifest's size and sha256, with nothing extra in the archive."""
    blobs: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:xz") as tar:
            for i, m in enumerate(tar):
                if i >= MAX_MEMBERS:
                    raise PackageError("too many members")
                if not m.isreg():
                    raise PackageError(f"member {m.name!r} is not a regular file")
                name = _safe_member_name(m.name)
                if m.size > MAX_MEMBER_BYTES or (total := total + m.size) > MAX_TOTAL_BYTES:
                    raise PackageError(f"member {name!r} is too large")
                if name in blobs:
                    raise PackageError(f"duplicate member {name!r}")
                f = tar.extractfile(m)
                blob = f.read(m.size + 1) if f else b""
                if len(blob) != m.size:
                    raise PackageError(f"member {name!r} has the wrong length")
                blobs[name] = blob
    except (tarfile.TarError, OSError, EOFError) as e:
        raise PackageError(f"unreadable archive: {e}") from None
    try:
        manifest = json.loads(blobs[MANIFEST])
        files = manifest["files"]
        assert isinstance(files, dict)
    except (KeyError, ValueError, AssertionError):
        raise PackageError("manifest.json missing or malformed") from None
    if manifest.get("format") != DATA_FORMAT:
        raise PackageError(f"package format {manifest.get('format')!r}, this app takes {DATA_FORMAT}")
    version_key(str(manifest.get("version")))
    extra = set(blobs) - set(files) - {MANIFEST, NOTICE}
    if extra:
        raise PackageError(f"files not in the manifest: {sorted(extra)[:3]}")
    for name, meta in files.items():
        _safe_member_name(name)
        blob = blobs.get(name)
        if blob is None:
            raise PackageError(f"manifest lists {name!r} but the archive lacks it")
        if len(blob) != meta.get("size") or hashlib.sha256(blob).hexdigest() != meta.get("sha256"):
            raise PackageError(f"{name!r} does not match the manifest")
    for name, blob in blobs.items():
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return manifest


def install(archive: Path, signature: str, allow_older: bool = False, keys: list[str] | None = None) -> str:
    """Verify, unpack beside the previous package and switch to it; returns the version. Raises PackageError
    with nothing changed on any failure. The signature is checked before the archive is opened."""
    data = archive.read_bytes()
    verify(data, signature, keys)
    packages = config.DATA_HOME / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    staging = packages / f".staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        manifest = unpack(data, staging)
        ver = str(manifest["version"])
        state = read_state()
        if state["current"] and not allow_older and version_key(ver) < version_key(state["current"]):
            raise PackageError(f"{ver} is older than the installed {state['current']} (--force to install anyway)")
        final = package_dir(ver)
        if state["current"] == ver:
            return ver  # already active
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    old_previous = state["previous"]
    _write_state({"current": ver, "previous": state["current"]})
    if old_previous and old_previous not in (ver, state["current"]):
        shutil.rmtree(package_dir(old_previous), ignore_errors=True)  # keep one previous only
    return ver


def install_file(path_: Path, allow_older: bool = False) -> str:
    """Install a local package; its signature is `<file>.sig` beside it. Still required."""
    sig = path_.with_name(path_.name + ".sig")
    if not sig.is_file():
        raise PackageError(f"no signature file {sig.name}")
    return install(path_, sig.read_text(), allow_older)


def rollback() -> str:
    """Switch back to the previous package (or, with none, to the bundled data); returns the version now
    pointed at. Takes effect on next start."""
    state = read_state()
    if not state["current"]:
        raise PackageError("no data package is installed")
    prev, _ = _loadable(state["previous"])
    _write_state({"current": state["previous"] if prev else None, "previous": None})
    return state["previous"] if prev else "bundled"


# ---- updates ---------------------------------------------------------------------------------------------------

def _get(url: str, limit: int) -> bytes:
    import httpx
    from .config import USER_AGENT
    with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > limit:
                raise PackageError(f"{url} is larger than {limit} bytes")
        return bytes(buf)


@dataclass
class Offer:
    version: str
    url: str
    signature_url: str
    newer: bool


def _bad_url_part(s: str) -> bool:
    return not s or "/" in s or "\\" in s or s.startswith(".")


def check(base_url: str | None, fetch=_get) -> Offer | None:
    """The newest package of our format at `<base_url>/data/index.json`, or None when no URL is set. Raises on
    network or index errors. `newer` says whether it beats what is installed."""
    if not base_url:
        return None
    base = base_url.rstrip("/") + "/data/"
    try:
        index = json.loads(fetch(base + "index.json", 1_000_000))
        entry = index["packages"][str(DATA_FORMAT)]
        ver, file = str(entry["version"]), str(entry["file"])
        version_key(ver)
    except (ValueError, KeyError, TypeError) as e:
        raise PackageError(f"index.json unusable: {e}") from None
    if _bad_url_part(file) or file != package_name(ver):
        raise PackageError(f"index names an unexpected file {file!r}")
    current = read_state()["current"]
    newer = current is None or version_key(ver) > version_key(current)
    return Offer(ver, base + file, base + file + ".sig", newer)


def update(base_url: str | None, fetch=_get) -> str | None:
    """Download and install the newest package if it is newer than the installed one; returns the new version."""
    offer = check(base_url, fetch)
    if offer is None or not offer.newer:
        return None
    blob = fetch(offer.url, MAX_DOWNLOAD_BYTES)
    sig = fetch(offer.signature_url, 10_000).decode("ascii", "replace")
    tmp = config.DATA_HOME / "downloads"
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / package_name(offer.version)
    f.write_bytes(blob)
    try:
        ver = install(f, sig)
    finally:
        f.unlink(missing_ok=True)
    if ver != offer.version:
        raise PackageError(f"index promised {offer.version}, package is {ver}")
    return ver


def check_on_launch(settings: "config.Settings", fetch=_get) -> str | None:
    """For the app's start: install a newer package when `check_updates` is on and a URL is set. Never
    raises (a failed check leaves the installed data as it is); the new data is used from the next start."""
    if not settings.check_updates or not settings.data_url:
        return None
    try:
        return update(settings.data_url, fetch)
    except Exception as e:  # network down, bad index, bad package: keep what we have
        log.warning("data package update failed: %s", e)
        return None
