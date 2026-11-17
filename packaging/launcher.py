"""Entry point of the frozen app (AppImage, Windows build): one executable for the window and the CLI.

With no arguments it opens the planner window; with arguments it is `radv` (so `RuneAdvisor.AppImage guide
YourName` works from a terminal).
"""

import os
import sys


def main() -> int:
    if len(sys.argv) > 1:
        from rune_advisor.cli import app

        app(prog_name="radv")
        return 0
    if sys.platform.startswith("linux"):
        os.environ.setdefault("PYWEBVIEW_GUI", "qt")  # the only backend in the bundle; skips pywebview's GTK probe
    from rune_advisor.app import main as window

    return window([])


if __name__ == "__main__":
    sys.exit(main())
