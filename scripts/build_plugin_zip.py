"""Build the zip of the exporter plugin that is uploaded to the Plugin Hub (Mod.io).

    uv run python scripts/build_plugin_zip.py

Writes dist/rune-advisor-export-<version>.zip from the files of the current commit (never from uncommitted
edits), so an upload can always be traced to a commit: `main.lua` and `plugin.json` at the top level, as Jagex
requires, and `lib/`. The tests and the README (the listing text) stay out. File times are fixed, so the same
commit always gives the same bytes; the script prints the commit and the zip's SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "plugins/rune_advisor_export"
INCLUDED = ("main.lua", "plugin.json", "lib/")   # paths under PLUGIN; anything else is left out
FORBIDDEN = set('\\?"<>|:*')                     # Mod.io: not allowed in file names ("/" separates folders)
FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def main() -> int:
    if git("status", "--porcelain", "--", PLUGIN).strip():
        print(f"uncommitted changes under {PLUGIN}: commit them first, the zip is built from the commit", file=sys.stderr)
        return 1
    commit = git("rev-parse", "HEAD").decode().strip()
    names = sorted(n[len(PLUGIN) + 1:] for n in git("ls-tree", "-r", "--name-only", "HEAD", PLUGIN).decode().split("\n") if n)
    names = [n for n in names if any(n == i or (i.endswith("/") and n.startswith(i)) for i in INCLUDED)]
    bad = [n for n in names if FORBIDDEN & set(n)]
    if bad or "main.lua" not in names or "plugin.json" not in names:
        print(f"cannot build: bad names {bad} or main.lua / plugin.json missing", file=sys.stderr)
        return 1
    version = json.loads(git("show", f"HEAD:{PLUGIN}/plugin.json"))["version"]
    out = ROOT / "dist" / f"rune-advisor-export-{version}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in names:
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, git("show", f"HEAD:{PLUGIN}/{name}"))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"{out.relative_to(ROOT)}: version {version}, {len(names)} files, {out.stat().st_size} bytes")
    print(f"commit {commit}\nsha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
