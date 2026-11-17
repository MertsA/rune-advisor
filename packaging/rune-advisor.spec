# PyInstaller spec for the frozen planner app: a one-folder bundle (dist/build/frozen/rune-advisor/) that
# scripts/build_app.py wraps into an AppImage on Linux, an installer on Windows and a disk image on macOS.
# Run through the script, not directly: it sets the working directory and checks the result.
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
windows, mac = sys.platform == "win32", sys.platform == "darwin"
linux = not windows and not mac
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src" / "rune_advisor" / "__init__.py").read_text()).group(1)

# The planner's tables (the data submodule) and the window's static files travel as package data.
# The submodule's own repository files stay out.
datas = collect_data_files("rune_advisor", excludes=["**/__pycache__", "data/.git", "data/README.md"])
datas += [(str(ROOT / "LICENSE"), "."), (str(ROOT / "NOTICE.md"), ".")]
if os.environ.get("RUNE_ADVISOR_BUILD_FILE"):  # version and commits, written by scripts/build_app.py
    datas += [(os.environ["RUNE_ADVISOR_BUILD_FILE"], ".")]

# pywebview picks its backend at run time: Qt (PyQt6 + QtWebEngine) on Linux, WebView2 on Windows, the
# system's WebKit on macOS. Only Linux carries a browser engine of its own.
if linux:
    hidden = ["webview.platforms.qt", "qtpy", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebChannel", "PyQt6.QtNetwork"]
elif windows:
    hidden = ["webview.platforms.edgechromium"]
else:
    hidden = ["webview.platforms.cocoa"]
# Backends and toolkits that are never used; leaving them out keeps stray system libraries out of the bundle.
excluded = ["tkinter", "gi", "PySide6", "PyQt5", "PySide2", "pytest", "webview.platforms.gtk", "webview.platforms.cef",
            "webview.platforms.android"]
if not linux:
    excluded += ["PyQt6", "qtpy", "webview.platforms.qt"]
if not windows:
    excluded += ["clr", "webview.platforms.winforms", "webview.platforms.edgechromium"]
if not mac:
    excluded += ["webview.platforms.cocoa"]

a = Analysis([str(ROOT / "packaging" / "launcher.py")], pathex=[str(ROOT / "src")], datas=datas,
             hiddenimports=hidden, excludes=excluded, noarchive=False)


# ---- Linux: trim Qt ---------------------------------------------------------------------------------------
# The PyQt6 wheels carry all of Qt. The window is one QtWebEngine widget showing our own pages, so these go:
QT = "PyQt6/Qt6/"
UNUSED = [
    QT + "qml/",                                   # QML modules (Quick Controls, Quick 3D, Multimedia...): no QML here
    QT + "resources/qtwebengine_devtools_resources.pak",  # the browser's developer tools
    QT + "plugins/platformthemes/libqgtk3.so",     # GTK theme plugin: would drag the build machine's GTK along
    QT + "plugins/platforms/libqeglfs.so", QT + "plugins/platforms/libqlinuxfb.so",   # embedded / framebuffer
    QT + "plugins/platforms/libqminimalegl.so", QT + "plugins/platforms/libqvkkhrdisplay.so",
    QT + "plugins/platforms/libqvnc.so", QT + "plugins/egldeviceintegrations/",
]
# Qt's translations of its own dialogs and of the browser's built-in pages; the app's pages are English.
# The browser engine needs one locale file to start.
TRANSLATIONS, KEPT_LOCALE = QT + "translations/", QT + "translations/qtwebengine_locales/en-US.pak"


# Libraries that must come from the user's system, never from the build machine. The graphics drivers are
# the system's own and load into this process; they need the system's C++ runtime, Wayland, X11 and DRM
# libraries, and an older bundled copy found first makes them fail to load (seen on Arch with a bundle built
# on Ubuntu 22.04: Mesa wanted a newer libstdc++, no EGL, the browser engine aborted). Fonts are configured
# by the system's files, which an older bundled fontconfig cannot read. Every desktop has these, and the build
# machine is older than any supported system, so the system's copy is always new enough for the rest of
# the bundle.
HOST_LIBS = re.compile(r"^lib(stdc\+\+|gcc_s|fontconfig|freetype|harfbuzz|expat|z|drm|gbm|glapi|GL|EGL|GLX|GLdispatch|"
                       r"OpenGL|wayland-[a-z]+|X11|X11-xcb|xcb|xcb-dri[23]|xcb-glx|xcb-present|xcb-sync|xcb-xfixes|"
                       r"xcb-randr|xcb-shm|xshmfence|Xau|Xdmcp|Xext|zstd|"
                       r"asound|glib-2\.0|gobject-2\.0|gio-2\.0|gmodule-2\.0|gthread-2\.0|dbus-1|systemd|udev)\.so(\.|$)")


def unused(dest: str) -> bool:
    dest = PurePosixPath(dest).as_posix()
    if "/" not in dest and HOST_LIBS.match(dest):
        return True
    if dest.startswith(TRANSLATIONS):
        return dest != KEPT_LOCALE
    return any(dest == u or (u.endswith("/") and dest.startswith(u)) for u in UNUSED)


def needed(path: str) -> set[str]:
    """The shared libraries an ELF file names as dependencies."""
    out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True).stdout
    return set(re.findall(r"\(NEEDED\).*\[(.+?)\]", out))


def trim(binaries):
    """Drop the unused files, then every library that only they needed.

    A library goes only when a dropped file led to it and nothing kept does, so libraries loaded by name at
    run time (which no file lists as a dependency) are never touched.
    """
    dropped = [b for b in binaries if unused(b[0])]
    kept = [b for b in binaries if not unused(b[0])]
    by_name = {PurePosixPath(b[0]).name: b for b in kept}

    def reach(start):
        seen, todo = set(), [b[1] for b in start]
        while todo:
            for name in needed(todo.pop()):
                if name in by_name and name not in seen:
                    seen.add(name)
                    todo.append(by_name[name][1])
        return seen

    orphan_candidates = reach(dropped)
    live = reach([b for b in kept if PurePosixPath(b[0]).name not in orphan_candidates])
    orphans = orphan_candidates - live
    print(f"trim: {len(dropped)} unused files, {len(orphans)} libraries only they needed: {sorted(orphans)}")
    return [b for b in kept if PurePosixPath(b[0]).name not in orphans]


if linux:
    a.binaries = trim(a.binaries)
    a.datas = [d for d in a.datas if not unused(d[0])]

pyz = PYZ(a.pure)
if mac:
    # One windowed executable inside an app bundle; from a terminal it is still the CLI:
    # "Rune Advisor.app/Contents/MacOS/rune-advisor" guide YourName
    exe = EXE(pyz, a.scripts, exclude_binaries=True, name="rune-advisor", console=False, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="rune-advisor")
    BUNDLE(coll, name="Rune Advisor.app", bundle_identifier="com.runeadvisor.RuneAdvisor", version=VERSION,
           info_plist={"CFBundleDisplayName": "Rune Advisor", "NSHighResolutionCapable": True,
                       "LSMinimumSystemVersion": "12.0"})
else:
    # console=True: the same executable is the CLI. On Windows a second, windowed executable opens the planner
    # without a console flashing up.
    exes = [EXE(pyz, a.scripts, exclude_binaries=True, name="rune-advisor", console=True, strip=linux, upx=False)]
    if windows:
        exes.append(EXE(pyz, a.scripts, exclude_binaries=True, name="Rune Advisor", console=False, upx=False))
    COLLECT(*exes, a.binaries, a.datas, strip=linux, upx=False, name="rune-advisor")
