# PyInstaller spec for the frozen planner app: a one-folder bundle (dist/rune-advisor/) that
# scripts/build_app.py wraps into an AppImage on Linux and an installer on Windows.
# Run through the script, not directly: it sets the working directory and checks the result.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
windows = sys.platform == "win32"

# The planner's tables (the data submodule) and the window's static files travel as package data.
# The submodule's own repository files stay out.
datas = collect_data_files("rune_advisor", excludes=["**/__pycache__", "data/.git", "data/README.md"])
datas += [(str(ROOT / "LICENSE"), "."), (str(ROOT / "NOTICE.md"), ".")]

# pywebview picks its backend at run time: Qt (PyQt6 + QtWebEngine) on Linux, WebView2 on Windows.
hidden = ["webview.platforms.edgechromium"] if windows else ["webview.platforms.qt", "qtpy", "PyQt6.QtWebEngineWidgets",
                                                              "PyQt6.QtWebChannel", "PyQt6.QtNetwork"]
# Backends and toolkits that are never used; leaving them out keeps stray system libraries out of the bundle.
excluded = ["tkinter", "gi", "PySide6", "PyQt5", "PySide2", "pytest", "webview.platforms.gtk", "webview.platforms.cef",
            "webview.platforms.android", "webview.platforms.cocoa"]
excluded += ["PyQt6", "qtpy"] if windows else ["clr", "webview.platforms.winforms", "webview.platforms.edgechromium"]

a = Analysis([str(ROOT / "packaging" / "launcher.py")], pathex=[str(ROOT / "src")], datas=datas,
             hiddenimports=hidden, excludes=excluded, noarchive=False)
pyz = PYZ(a.pure)
# console=True: the same executable is the CLI. On Windows a second, windowed executable opens the planner
# without a console flashing up.
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="rune-advisor", console=True, strip=not windows, upx=False)
exes = [exe]
if windows:
    exes.append(EXE(pyz, a.scripts, exclude_binaries=True, name="Rune Advisor", console=False, upx=False))
COLLECT(*exes, a.binaries, a.datas, strip=not windows, upx=False, name="rune-advisor")
