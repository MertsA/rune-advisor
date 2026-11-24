"""Builds the web planner (a proof of concept): the public site plus /planner/, where the planner runs in the
browser under Pyodide (CPython compiled to WebAssembly).

    uv run scripts/build_web.py [--out dist/web] [--api http://127.0.0.1:8766] [--quest-list-from NAME]
                                [--assets-url https://dl.runeadvisor.com/planner-assets/]

What lands in <out>, in two parts. The small files the pages run are served by our own servers; the big ones
can come from a CDN (Cloudflare in front of dl.runeadvisor.com), because the worker checks every file it fetches
itself against a hash in the manifest, which comes from our servers (fetch's `integrity`). Pyodide's own files
are loaded by Pyodide's loader and are not checked (the user's call, 2026-11-24).

  the site (site/), with the account page told where the planner is and, with --api, both pages told which
    API to talk to;
  planner/                packaging/web/ (page, page script, worker), the desktop app's app.js and style.css,
                          and Pyodide's small loader (pyodide.js: a worker may only import scripts from the
                          page's own site)
  planner/manifest.json   what the worker loads, with each file's hash and the day the wiki tables were fetched
  planner-assets/         the big files, under names that never change meaning (cache them forever):
    pyodide/<version>/    the pinned Pyodide runtime, from its npm package (checked against PYODIDE_SHA256)
    files/<sha256>/<name> this checkout's wheel (the planner and its data), the pure-Python packages it imports
                          (Pyodide's own builds of some, checked against Pyodide's lock file, the rest from PyPI,
                          checked against PYPI_WHEELS), and wiki-cache.zip: the wiki tables the planner reads
                          (achievements, their requirements, quests, GE prices), fetched now, since a page cannot
                          fetch them. With --quest-list-from, also the list of quests that exist today, read from
                          that player's Adventurer's Log (the same for every player; only the titles are kept).

--assets-url names where planner-assets/ is served (default: /planner-assets/ on the same site). For the live
site, upload planner-assets/ there and serve the rest of <out> from runeadvisor.com without it.

Downloads are kept in <out>/../web-cache. Serve the result with scripts/serve_web.py.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PYODIDE = "314.0.8"  # Python 3.14
PYODIDE_SHA256 = "9a52704e0ae5b07aaacb8e4fed9f1658e8024694a078695ea45ac79dee4cefcb"  # the npm tarball
PYODIDE_FILES = ["pyodide.js", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"]
# Pure-Python packages taken from the Pyodide distribution (its httpx talks through the browser's fetch). The
# command line's libraries (typer, rich, click) are not shipped: the web planner builds its Context in
# rune_advisor.planning, which does not import them (tests/test_app.py checks).
PYODIDE_PACKAGES = ["httpx", "typing-extensions"]
# Wheels from PyPI the planner imports and Pyodide lacks (none today), checked against their SHA-256.
PYPI_WHEELS: dict[str, str] = {}
USER_AGENT = "rune-advisor-build"


def fetch(url: str, dest: Path, sha256: str) -> Path:
    """`url` at `dest` (kept between builds), refused unless it hashes to `sha256`."""
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as r:
            dest.write_bytes(r.read())
    got = hashlib.sha256(dest.read_bytes()).hexdigest()
    if got != sha256:
        dest.unlink()
        sys.exit(f"{url}: sha256 {got}, expected {sha256}")
    return dest


def pypi_url(filename: str) -> str:
    """The download URL PyPI lists for a wheel file."""
    name, version = filename.split("-")[:2]
    with urllib.request.urlopen(urllib.request.Request(f"https://pypi.org/pypi/{name.replace('_', '-')}/{version}/json",
                                                       headers={"User-Agent": USER_AGENT}), timeout=60) as r:
        files = json.load(r)["urls"]
    return next(f["url"] for f in files if f["filename"] == filename)


def hashed(src: Path, assets: Path, name: str | None = None) -> dict:
    """Copy `src` to assets/files/<sha256>/<name>: {path, integrity} for the manifest."""
    data = src.read_bytes()
    digest = hashlib.sha256(data)
    path = f"files/{digest.hexdigest()}/{name or src.name}"
    (assets / path).parent.mkdir(parents=True, exist_ok=True)
    (assets / path).write_bytes(data)
    return {"path": path, "integrity": "sha256-" + base64.b64encode(digest.digest()).decode("ascii")}


def runtime(cache: Path, planner: Path, assets: Path) -> list[Path]:
    """Pyodide's loader into planner/, the rest of it into assets/pyodide/<version>/; returns the wheels to ship."""
    tgz = fetch(f"https://registry.npmjs.org/pyodide/-/pyodide-{PYODIDE}.tgz", cache / f"pyodide-{PYODIDE}.tgz", PYODIDE_SHA256)
    core = assets / "pyodide" / PYODIDE
    core.mkdir(parents=True)
    with tarfile.open(tgz) as tar:
        for name in PYODIDE_FILES:
            (planner if name == "pyodide.js" else core).joinpath(name).write_bytes(tar.extractfile(f"package/{name}").read())
    lock = json.loads((core / "pyodide-lock.json").read_text())["packages"]
    wheels = []
    for package in PYODIDE_PACKAGES:
        entry = lock[package]
        if entry["depends"]:
            sys.exit(f"{package} has dependencies in this Pyodide ({entry['depends']}): list them in PYODIDE_PACKAGES")
        wheels.append(fetch(f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE}/full/{entry['file_name']}",
                            cache / entry["file_name"], entry["sha256"]))
    for filename, sha in PYPI_WHEELS.items():
        wheels.append(fetch("" if (cache / filename).exists() else pypi_url(filename), cache / filename, sha))
    return wheels


def planner_wheel(out: Path) -> Path:
    """This checkout's wheel, data included."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    subprocess.run(["uv", "build", "--wheel", "-o", str(out)], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    (wheel,) = [p for p in out.iterdir() if p.suffix == ".whl"]
    return wheel


def wiki_cache(cache: Path, dest: Path, quest_player: str | None) -> dict:
    """The planner's wiki fetches, made here and zipped as its HTTP cache (http/<key>.json, quest_titles.json)."""
    os.environ["RUNE_ADVISOR_CACHE"] = str(cache / "planner-cache")  # read when rune_advisor.config is imported
    sys.path.insert(0, str(ROOT / "src"))
    from rune_advisor.http import CachedClient
    from rune_advisor.prices import Prices
    from rune_advisor.sources import adventurers_log, wiki

    client = CachedClient()
    counts = {
        "achievements": len(wiki.fetch_all_achievements(client)),
        "requirements": len(wiki.fetch_achievement_requirements(client, ironman=False))
        + len(wiki.fetch_achievement_requirements(client, ironman=True)),
        "members_only": len(wiki.members_only_achievements(client)),
        "quests": len(wiki.fetch_all_quests(client)),
    }
    Prices(client)
    titles = None
    if quest_player:
        titles = sorted(adventurers_log.parse_quests(adventurers_log.fetch_quests(client, quest_player)))
        counts["quest_titles"] = len(titles)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(client.cache_dir.glob("*.json")):
            if quest_player and quest_player.lower() in f.read_text()[:2000].lower():
                continue  # a player's own answer (their quest list): only the titles go out
            z.write(f, f"http/{f.name}")
        if titles:
            z.writestr("quest_titles.json", json.dumps(titles))
    return counts


def site(out: Path, api: str | None) -> None:
    shutil.copytree(ROOT / "site", out)
    planner = out / "planner"
    shutil.copytree(ROOT / "packaging" / "web", planner)
    for name in ("app.js", "style.css"):
        shutil.copy(ROOT / "src" / "rune_advisor" / "app" / "static" / name, planner / name)
    account = out / "account" / "index.html"
    text = account.read_text()
    marker = '<meta name="api-base"'
    assert marker in text
    account.write_text(text.replace(marker, '<meta name="planner" content="/planner/">\n  ' + marker))
    if api:
        for page in (account, planner / "index.html"):
            text = page.read_text()
            old = '<meta name="api-base" content="https://api.runeadvisor.com">'
            assert old in text, page
            page.write_text(text.replace(old, f'<meta name="api-base" content="{api.rstrip("/")}">'))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "web")
    ap.add_argument("--api", help="the API the pages talk to (default: the one the site names)")
    ap.add_argument("--quest-list-from", metavar="NAME", help="a player whose Adventurer's Log supplies today's quest titles")
    ap.add_argument("--assets-url", default="/planner-assets/",
                    help="where planner-assets/ is served (default: the same site); a CDN for the live site")
    args = ap.parse_args()
    out, cache = args.out.resolve(), args.out.resolve().parent / "web-cache"
    if out.exists():
        shutil.rmtree(out)
    site(out, args.api)
    planner, assets = out / "planner", out / "planner-assets"
    wheels = runtime(cache, planner, assets)
    wheels.append(planner_wheel(cache / "planner-wheel"))
    counts = wiki_cache(cache, cache / "wiki-cache.zip", args.quest_list_from)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    (planner / "manifest.json").write_text(json.dumps({
        "version": commit, "pyodide": f"pyodide/{PYODIDE}/", "assets": args.assets_url,
        "wheels": [hashed(w, assets) for w in wheels], "cache": hashed(cache / "wiki-cache.zip", assets),
        "tables_fetched": datetime.date.today().isoformat()}, indent=1))
    size = lambda p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
    print(f"built {out}")
    print(f"  wiki tables: {counts}")
    print(f"  planner (our servers): {size(planner) / 1e6:.1f} MB")
    print(f"  planner-assets ({args.assets_url}): {size(assets) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
