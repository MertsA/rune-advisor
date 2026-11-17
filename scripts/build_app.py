#!/usr/bin/env python3
"""Build the installable planner app from the current commit.

    uv run --extra ui --group package scripts/build_app.py [--out dist] [--allow-dirty]

Freezes the planner with PyInstaller (packaging/rune-advisor.spec: Python, the dependencies, the data tables
and the window's files in one folder), then wraps the folder for the platform it runs on:

- Linux: `RuneAdvisor-<version>-x86_64.AppImage`, one executable file. No arguments opens the planner window;
  with arguments it is the CLI (`./RuneAdvisor-...AppImage guide YourName`).
- Windows: the frozen folder only, for now; the installer step comes with the Windows CI job.

PyInstaller does not cross-build: each platform's app is built on that platform. A build is portable to systems
with the same or a newer glibc than the build machine's, so release builds run in CI on an old base image; a
build made on a current Arch is for testing.

The build records the commit it was made from (`BUILD` in the bundle) and refuses a tree with uncommitted
changes unless told otherwise, so a released file can be traced to a commit. Timestamps inside the AppImage
are the commit's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "rune-advisor.spec"
APP_ID = "com.runeadvisor.RuneAdvisor"

# The AppImage runtime: the small static executable at the front of every AppImage that mounts the image
# behind it and starts AppRun. Pinned by hash; from https://github.com/AppImage/type2-runtime (MIT).
RUNTIME_URL = "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA256 = "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

DESKTOP = f"""[Desktop Entry]
Type=Application
Name=Rune Advisor
Comment=RuneScape planning helper: what to do next, from your own character's data
Exec=rune-advisor
Icon={APP_ID}
Categories=Game;
Terminal=false
StartupWMClass=rune-advisor
"""

METAINFO = f"""<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>{APP_ID}</id>
  <name>Rune Advisor</name>
  <summary>RuneScape planning helper</summary>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>GPL-3.0-or-later</project_license>
  <description>
    <p>Reads your character's data and recommends what to do next: quests, achievements, training methods
    and gear, with the time each takes for your character.</p>
  </description>
  <launchable type="desktop-id">{APP_ID}.desktop</launchable>
  <url type="homepage">https://runeadvisor.com</url>
  <provides><binary>rune-advisor</binary></provides>
  <content_rating type="oars-1.1"/>
</component>
"""


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout.strip()


def version() -> str:
    """The app version, read from the source without importing the package."""
    for line in (ROOT / "src" / "rune_advisor" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    raise SystemExit("no __version__ in src/rune_advisor/__init__.py")


def commit_info(allow_dirty: bool) -> dict:
    # --ignore-submodules=none: a data submodule at another commit than the recorded one is a change too.
    dirty = bool(git("status", "--porcelain", "--ignore-submodules=none"))
    if dirty and not allow_dirty:
        raise SystemExit("uncommitted changes: commit them, or pass --allow-dirty for a test build")
    return {"version": version(), "commit": git("rev-parse", "HEAD") + ("-dirty" if dirty else ""),
            "data_commit": git("-C", "src/rune_advisor/data", "rev-parse", "HEAD"),
            "commit_time": int(git("log", "-1", "--format=%ct"))}


def freeze(work: Path) -> Path:
    """Run PyInstaller; return the one-folder bundle."""
    if not (ROOT / "src" / "rune_advisor" / "data" / "mechanics.json").exists():
        raise SystemExit("src/rune_advisor/data is empty: git submodule update --init")
    dist = work / "frozen"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--log-level", "WARN",
                    "--distpath", str(dist), "--workpath", str(work / "pyinstaller"), str(SPEC)],
                   check=True, cwd=ROOT)
    return dist / "rune-advisor"


def runtime(cache: Path) -> bytes:
    path = cache / f"appimage-runtime-{RUNTIME_SHA256[:12]}"
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(RUNTIME_URL, timeout=60) as r:
            path.write_bytes(r.read())
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != RUNTIME_SHA256:
        path.unlink()
        raise SystemExit("the downloaded AppImage runtime does not match the pinned hash")
    return data


def appimage(bundle: Path, info: dict, work: Path, out: Path) -> Path:
    """An AppImage is the runtime followed by a squashfs image of the AppDir; no appimagetool needed."""
    if not shutil.which("mksquashfs"):
        raise SystemExit("mksquashfs not found: install squashfs-tools")
    appdir = work / "AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)
    shutil.copytree(bundle, appdir, symlinks=True)
    # The runtime starts AppRun. PyInstaller's launcher finds its folder through /proc/self/exe, so a link does.
    (appdir / "AppRun").symlink_to("rune-advisor")
    (appdir / f"{APP_ID}.desktop").write_text(DESKTOP)
    shutil.copy(ROOT / "site" / "assets" / "icon.svg", appdir / f"{APP_ID}.svg")
    (appdir / ".DirIcon").symlink_to(f"{APP_ID}.svg")
    meta = appdir / "usr" / "share" / "metainfo"
    meta.mkdir(parents=True)
    (meta / f"{APP_ID}.appdata.xml").write_text(METAINFO)
    (appdir / "BUILD").write_text(json.dumps(info, indent=1) + "\n")

    image = work / "app.squashfs"
    image.unlink(missing_ok=True)
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(info["commit_time"])}
    subprocess.run(["mksquashfs", str(appdir), str(image), "-root-owned", "-noappend", "-no-progress", "-quiet",
                    "-comp", "zstd", "-Xcompression-level", "19", "-b", "1M"], check=True, env=env)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"RuneAdvisor-{info['version']}-x86_64.AppImage"
    with target.open("wb") as f:
        f.write(runtime(work / "cache"))
        with image.open("rb") as src:
            shutil.copyfileobj(src, f)
    target.chmod(0o755)
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "dist", help="where the result goes (default: dist/)")
    ap.add_argument("--work", type=Path, default=ROOT / "dist" / "build", help="scratch folder (default: dist/build)")
    ap.add_argument("--allow-dirty", action="store_true", help="build with uncommitted changes (marked in BUILD)")
    args = ap.parse_args()

    info = commit_info(args.allow_dirty)
    bundle = freeze(args.work)
    if sys.platform.startswith("linux"):
        result = appimage(bundle, info, args.work, args.out)
    else:
        (bundle / "BUILD").write_text(json.dumps(info, indent=1) + "\n")
        result = bundle
    size = (sum(p.stat().st_size for p in result.rglob("*") if p.is_file()) if result.is_dir()
            else result.stat().st_size)
    print(f"built {result} ({size / 1e6:.0f} MB) from {info['commit'][:12]}, data {info['data_commit'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
