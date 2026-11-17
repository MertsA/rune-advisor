"""Builds the web planner (a proof of concept): the public site plus /planner/, where the planner runs in the
browser under Pyodide (CPython compiled to WebAssembly).

    uv run scripts/build_web.py [--out dist/web] [--api http://127.0.0.1:8766] [--quest-list-from NAME]

What lands in <out>:
  the site (site/), with the account page told where the planner is and, with --api, both pages told which
    API to talk to;
  planner/               packaging/web/ (page, page script, worker) + the desktop app's app.js and style.css
  planner/pyodide/       the pinned Pyodide runtime, from its npm package (checked against PYODIDE_SHA256)
  planner/wheels/        this checkout's wheel (the planner and its data) and the pure-Python packages it
                         imports: Pyodide's own builds of some (checked against Pyodide's lock file), the rest
                         from PyPI (checked against PYPI_WHEELS)
  planner/wiki-cache.zip the wiki tables the planner reads (achievements, their requirements, quests, GE
                         prices), fetched now: a page cannot fetch them, so the worker unpacks them into the
                         planner's HTTP cache. With --quest-list-from, also the list of quests that exist today,
                         read from that player's Adventurer's Log (the list is the same for every player; only
                         the titles are kept).
  planner/manifest.json  what the worker loads

Downloads are kept in <out>/../web-cache. Serve the result with scripts/serve_web.py.
"""

from __future__ import annotations

import argparse
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
# Pure-Python packages taken from the Pyodide distribution (its httpx talks through the browser's fetch).
PYODIDE_PACKAGES = ["httpx", "click", "rich", "typing-extensions"]
# The rest of what `rune_advisor.cli` imports (the web planner builds its Context there), from PyPI.
PYPI_WHEELS = {
    "typer-0.29.1-py3-none-any.whl": "00dbc549c13428bec1be55fe1f4d0fb8c6637a310e4f178ef14abe9534ee7b58",
    "shellingham-1.5.4-py2.py3-none-any.whl": "7ecfff8f2fd72616f7481040475a65b2bf8af90a56c89140852d1120324e8686",
    "annotated_doc-0.0.6-py3-none-any.whl": "361ca0950cc7082f53fa69e9328409f40a62c61e57e0a47e1825a08078a58a53",
    "markdown_it_py-4.3.0-py3-none-any.whl": "7aa1511a46c39138de78d9eba92ce47e63124d8003fc98b3a351909e04239efa",
    "mdurl-0.1.2-py3-none-any.whl": "84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8",
}
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


def runtime(cache: Path, out: Path) -> list[str]:
    """Pyodide into out/pyodide and its wheels into out/wheels; returns the wheel file names."""
    tgz = fetch(f"https://registry.npmjs.org/pyodide/-/pyodide-{PYODIDE}.tgz", cache / f"pyodide-{PYODIDE}.tgz", PYODIDE_SHA256)
    (out / "pyodide").mkdir(parents=True)
    with tarfile.open(tgz) as tar:
        for name in PYODIDE_FILES:
            (out / "pyodide" / name).write_bytes(tar.extractfile(f"package/{name}").read())
    lock = json.loads((out / "pyodide" / "pyodide-lock.json").read_text())["packages"]
    wheels = []
    for package in PYODIDE_PACKAGES:
        entry = lock[package]
        if entry["depends"]:
            sys.exit(f"{package} has dependencies in this Pyodide ({entry['depends']}): list them in PYODIDE_PACKAGES")
        fetched = fetch(f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE}/full/{entry['file_name']}",
                        cache / entry["file_name"], entry["sha256"])
        shutil.copy(fetched, out / "wheels" / fetched.name)
        wheels.append(fetched.name)
    for filename, sha in PYPI_WHEELS.items():
        fetched = fetch("" if (cache / filename).exists() else pypi_url(filename), cache / filename, sha)
        shutil.copy(fetched, out / "wheels" / filename)
        wheels.append(filename)
    return wheels


def planner_wheel(out: Path) -> str:
    """This checkout's wheel, data included."""
    before = set(out.iterdir())
    subprocess.run(["uv", "build", "--wheel", "-o", str(out)], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    (wheel,) = [p for p in set(out.iterdir()) - before if p.suffix == ".whl"]
    return wheel.name


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
    args = ap.parse_args()
    out, cache = args.out.resolve(), args.out.resolve().parent / "web-cache"
    if out.exists():
        shutil.rmtree(out)
    site(out, args.api)
    planner = out / "planner"
    (planner / "wheels").mkdir()
    wheels = runtime(cache, planner)
    wheels.append(planner_wheel(planner / "wheels"))
    counts = wiki_cache(cache, planner / "wiki-cache.zip", args.quest_list_from)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    (planner / "manifest.json").write_text(json.dumps(
        {"version": commit, "pyodide": PYODIDE, "wheels": wheels, "cache": "wiki-cache.zip"}, indent=1))
    size = lambda p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
    print(f"built {out}")
    print(f"  wiki tables: {counts}")
    for part in ("pyodide", "wheels", "wiki-cache.zip"):
        print(f"  planner/{part}: {size(planner / part) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
