#!/usr/bin/env python3
"""Build the installable planner app from the current commit.

    uv run --extra ui --group package scripts/build_app.py [--out dist] [--allow-dirty]

Freezes the planner with PyInstaller (packaging/rune-advisor.spec: Python, the dependencies, the data tables
and the window's files in one folder), then wraps the folder for the platform it runs on:

- Linux: `RuneAdvisor-<version>-x86_64.AppImage`, one executable file. No arguments opens the planner window;
  with arguments it is the CLI (`./RuneAdvisor-...AppImage guide YourName`).
- Windows: `RuneAdvisor-<version>-windows-x64-setup.exe`, a per-user installer made with Inno Setup
  (packaging/windows/rune-advisor.iss; needs `iscc` on the PATH or in its default folder).
- macOS: `RuneAdvisor-<version>-macos-<arch>.dmg` holding `Rune Advisor.app`.

Before wrapping, the frozen program is run once (`data status`, with an empty home folder) so a bundle that
cannot start fails the build.

PyInstaller does not cross-build: each platform's app is built on that platform. A build is portable to systems
with the same or a newer glibc than the build machine's, so release builds run in CI on an old base image; a
build made on a current Arch is for testing.

The build records the commit it was made from (`BUILD` beside the bundled LICENSE) and refuses a tree with uncommitted
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
import tempfile
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


def freeze(work: Path, info: dict) -> Path:
    """Run PyInstaller; return the one-folder bundle (on macOS: the .app)."""
    if not (ROOT / "src" / "rune_advisor" / "data" / "mechanics.json").exists():
        raise SystemExit("src/rune_advisor/data is empty: git submodule update --init")
    dist = work / "frozen"
    # The spec adds this file to the bundle. It goes in at freeze time because a macOS app is sealed by its
    # signature when PyInstaller finishes: adding a file afterwards would break it.
    work.mkdir(parents=True, exist_ok=True)
    build_file = work / "BUILD"
    build_file.write_text(json.dumps(info, indent=1) + "\n")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--log-level", "WARN",
                    "--distpath", str(dist), "--workpath", str(work / "pyinstaller"), str(SPEC)],
                   check=True, cwd=ROOT, env={**os.environ, "RUNE_ADVISOR_BUILD_FILE": str(build_file)})
    bundle = dist / ("Rune Advisor.app" if sys.platform == "darwin" else "rune-advisor")
    if sys.platform.startswith("linux"):
        # PyInstaller links Qt's libraries into the bundle's top folder; the ones the spec trimmed leave links
        # to nothing behind.
        for link in bundle.rglob("*"):
            if link.is_symlink() and not link.exists():
                link.unlink()
    return bundle


def executable(bundle: Path) -> Path:
    if sys.platform == "darwin":
        return bundle / "Contents" / "MacOS" / "rune-advisor"
    return bundle / ("rune-advisor.exe" if sys.platform == "win32" else "rune-advisor")


def smoke(bundle: Path) -> None:
    """Start the frozen program as a new user would have it: no settings, no state, another working directory."""
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HOME": home, "USERPROFILE": home, "APPDATA": str(Path(home) / "Roaming"),
               "LOCALAPPDATA": str(Path(home) / "Local")}
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "RUNE_ADVISOR_CONFIG",
                     "RUNE_ADVISOR_STATE", "RUNE_ADVISOR_DATA_HOME", "RUNE_ADVISOR_CACHE"):
            env.pop(name, None)
        done = subprocess.run([str(executable(bundle)), "data", "status"], cwd=home, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=300)
    if done.returncode != 0 or "bundled" not in done.stdout:
        raise SystemExit(f"the frozen program failed its start check (exit {done.returncode}):\n"
                         f"{done.stdout}\n{done.stderr}")


def windows_installer(bundle: Path, info: dict, out: Path) -> Path:
    iscc = shutil.which("iscc") or shutil.which("ISCC")
    if not iscc:
        default = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe"
        if not default.exists():
            raise SystemExit("Inno Setup's iscc not found: install Inno Setup 6")
        iscc = str(default)
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([iscc, "/Qp", f"/DAppVersion={info['version']}", f"/DBundle={bundle}", f"/DOutDir={out.resolve()}",
                    str(ROOT / "packaging" / "windows" / "rune-advisor.iss")], check=True)
    return out / f"RuneAdvisor-{info['version']}-windows-x64-setup.exe"


def mac_disk_image(bundle: Path, info: dict, work: Path, out: Path) -> Path:
    """A disk image with the app and a link to /Applications to drag it onto."""
    import platform

    stage = work / "dmg"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copytree(bundle, stage / bundle.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    out.mkdir(parents=True, exist_ok=True)
    arch = "arm64" if platform.machine() == "arm64" else "x86_64"
    target = out / f"RuneAdvisor-{info['version']}-macos-{arch}.dmg"
    subprocess.run(["hdiutil", "create", "-volname", "Rune Advisor", "-srcfolder", str(stage), "-ov",
                    "-format", "UDZO", str(target)], check=True)
    return target


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
    bundle = freeze(args.work, info)
    smoke(bundle)
    if sys.platform.startswith("linux"):
        result = appimage(bundle, info, args.work, args.out)
    else:
        result = (windows_installer(bundle, info, args.out) if sys.platform == "win32"
                  else mac_disk_image(bundle, info, args.work, args.out))
    print(f"built {result} ({result.stat().st_size / 1e6:.0f} MB) from {info['commit'][:12]}, data {info['data_commit'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
