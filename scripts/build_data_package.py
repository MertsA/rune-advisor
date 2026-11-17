"""Build and sign a data package from src/rune_advisor/data (see docs/data-package.md).

Usage:
  python scripts/build_data_package.py --keygen [--out DIR]       # new Ed25519 pair; prints the public key
  python scripts/build_data_package.py --key PATH|--key-env VAR [--version 2026.11.12] [--out dist/data]
                                      [--skip-tests]

Writes rune-advisor-data-<format>-<version>.tar.xz and its .sig. The private key is a file (or environment
variable) holding base64 of the raw 32-byte key; it never belongs in the repo. Refuses to build when the
test suite fails (--skip-tests for the package tests themselves). The public half goes in
rune_advisor.datapkg.PUBLIC_KEYS.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rune_advisor import datapkg  # noqa: E402

NOTICE = """Rune Advisor data package

Most tables in this package are derived from the RuneScape Wiki (https://runescape.wiki), whose text is
licensed under CC BY-NC-SA 3.0 (https://creativecommons.org/licenses/by-nc-sa/3.0/). They were extracted,
reshaped and in places hand-corrected by the Rune Advisor project; this package is shared under the same
licence: attribution to the RuneScape Wiki and its editors, non-commercial use only, share alike.

Hand-written tables are the project's own work and carry the same licence as the wiki-derived ones.

game_configs/ holds quest and achievement definitions as the RuneScape client provides them through Jagex's
plugin API. They belong to Jagex Ltd; this project claims no rights in them and grants none, and the licence
above does not cover them.

Created using intellectual property belonging to Jagex Limited under the terms of Jagex's Fan Content Policy.
This content is not endorsed by or affiliated with Jagex. RuneScape is a trademark of Jagex Ltd.
"""


def keygen(out: Path) -> None:
    priv, pub = datapkg.generate_keypair()
    out.mkdir(parents=True, exist_ok=True)
    key_file = out / "rune-advisor-data.key"
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(priv + "\n")
    (out / "rune-advisor-data.pub").write_text(pub + "\n")
    print(f"private key: {key_file} (keep it out of the repo)")
    print(f"public key (add to datapkg.PUBLIC_KEYS): {pub}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keygen", action="store_true", help="generate a key pair into --out and exit")
    ap.add_argument("--key", type=Path, help="file holding the base64 private key")
    ap.add_argument("--key-env", help="environment variable holding the base64 private key")
    ap.add_argument("--version", default=date.today().strftime("%Y.%m.%d"))
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "data")
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args()
    if args.keygen:
        keygen(args.out)
        return 0
    if args.key:
        private = args.key.read_text().strip()
    elif args.key_env and os.environ.get(args.key_env):
        private = os.environ[args.key_env].strip()
    else:
        ap.error("give --key PATH or --key-env VAR (or --keygen)")
    if not args.skip_tests:
        if subprocess.run([sys.executable, "-m", "pytest", "-q", "-x"], cwd=ROOT).returncode != 0:
            print("tests fail: not building a package", file=sys.stderr)
            return 1
    archive, sig = datapkg.build(args.out, args.version, private, NOTICE)
    # The tree now is this release: an app built from it must not prefer an older installed package.
    (datapkg.BUNDLED / datapkg.BUNDLED_VERSION_FILE).write_text(args.version + "\n")
    print(f"{archive} ({archive.stat().st_size / 1e6:.1f} MB), {sig}")
    print(f"wrote {datapkg.BUNDLED_VERSION_FILE} = {args.version} to the data tree: commit it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
