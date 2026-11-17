#!/usr/bin/env python3
"""Publish a signed data package to dl.runeadvisor.com (docs/data-package.md, "Cutting a release").

    uv run python scripts/publish_data_package.py dist/data/rune-advisor-data-1-2026.11.14.tar.xz
    uv run python scripts/publish_data_package.py <package> --dry-run        # show what would be sent, send nothing
    uv run python scripts/publish_data_package.py --prune-only               # only remove superseded packages

The package comes from `scripts/build_data_package.py`; its `.sig` must lie beside it. Run this where the SSH
key for the upload account is (the user's own machine: the key is not in this repo, a sandbox or a server).

What it does, in this order, so that nobody is ever offered a file that is not there:

1. Checks the package locally: the file name, that the signature verifies against the app's public keys
   (`datapkg.PUBLIC_KEYS`; refused when there are none, unless `--public-key` gives one), that the archive
   unpacks under the app's own rules and its manifest carries the version and format in the name.
2. Reads what is on the server (an sftp listing and the current `index.json`). A file of the same name is
   never overwritten: a published package is immutable, a new build takes a new version.
3. Uploads the package and its signature (as `<name>.part`, renamed when complete: half-uploaded files are
   not served under their real name).
4. Uploads the new index as `index.json.tmp` and renames it over `index.json`. The index keeps the other
   formats' entries, so older apps keep finding their own newest package.
5. Fetches the index, the package and the signature back over HTTPS and compares them with the local files.
6. Removes superseded packages: per format the indexed one and the one before it stay, older ones go.

The upload is `sftp` in batch mode (OpenSSH's own client, so the SSH agent, `~/.ssh/config` and, later, SSH
certificates all work without this script knowing): host dl.runeadvisor.com, port 22, user rune-dl, directory
/data. `--host/--port/--user/--remote-dir/--base-url` point it elsewhere (the tests use a local sshd).

Exit 0 published and verified, 1 refused or failed (the message says at which step and what is on the server),
2 usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rune_advisor import datapkg  # noqa: E402

NAME = re.compile(r"^rune-advisor-data-([0-9]+)-([0-9]+(?:\.[0-9]+){0,3})\.tar\.xz$")  # what the server serves
KEEP_PER_FORMAT = 2  # the indexed package and the one before it (an app's rollback target)


class Refused(Exception):
    pass


class Remote:
    """The upload directory, through `sftp -b` (one batch per call; any failing command fails the batch)."""

    def __init__(self, args: argparse.Namespace):
        self.target = f"{args.user}@{args.host}"
        self.base = ["sftp", "-q", "-P", str(args.port), "-o", "BatchMode=yes", *args.ssh_option]
        self.dir = args.remote_dir.rstrip("/") or "/"
        self.dry = args.dry_run

    def run(self, commands: list[str], read_only: bool = False) -> str:
        batch = "\n".join([f"cd {self.dir}", *commands]) + "\n"
        if self.dry and not read_only:
            print("would run over sftp:\n  " + "\n  ".join(commands))
            return ""
        done = subprocess.run([*self.base, "-b", "-", self.target], input=batch, capture_output=True, text=True)
        if done.returncode != 0:
            raise Refused(f"sftp failed ({done.returncode}): {(done.stderr or done.stdout).strip()[-400:]}")
        return done.stdout

    def listing(self) -> list[str]:
        out = self.run(["ls -1"], read_only=True)
        return sorted(line.strip() for line in out.splitlines() if line.strip() and not line.startswith("sftp>"))

    def index(self, scratch: Path) -> dict:
        if "index.json" not in self.listing():
            return {"packages": {}}
        local = scratch / "index.remote.json"
        self.run([f"get index.json {local}"], read_only=True)
        try:
            index = json.loads(local.read_text())
            if not isinstance(index.get("packages"), dict):
                raise ValueError("no packages table")
        except ValueError as e:
            raise Refused(f"the server's index.json is unusable ({e}); fix it by hand before publishing") from None
        return index


def check_local(package: Path, extra_keys: list[str]) -> tuple[int, str, Path]:
    """(format, version, signature file) of a package that the app would accept."""
    match = NAME.match(package.name)
    if not match:
        raise Refused(f"{package.name} is not named rune-advisor-data-<format>-<version>.tar.xz")
    fmt, version = int(match.group(1)), match.group(2)
    sig = package.with_name(package.name + ".sig")
    if not package.is_file() or not sig.is_file():
        raise Refused(f"need both {package} and {sig.name} beside it")
    keys = [*datapkg.PUBLIC_KEYS, *extra_keys]
    if not keys:
        raise Refused("the app has no public key yet (datapkg.PUBLIC_KEYS is empty), so no installed app could "
                      "verify this package: add the release key to PUBLIC_KEYS first, or pass --public-key to "
                      "publish for testing")
    data = package.read_bytes()
    try:
        datapkg.verify(data, sig.read_text().strip(), keys)
        with tempfile.TemporaryDirectory() as scratch:
            manifest = datapkg.unpack(data, Path(scratch) / "unpacked")
    except datapkg.PackageError as e:
        raise Refused(f"the package does not verify: {e}") from None
    if str(manifest.get("version")) != version or int(manifest.get("format", -1)) != fmt:
        raise Refused(f"the manifest says format {manifest.get('format')} version {manifest.get('version')}, "
                      f"the file name says {fmt} and {version}")
    return fmt, version, sig


def superseded(names: list[str], index: dict) -> list[str]:
    """Package files (with their signatures) to delete: per format, all but the newest KEEP_PER_FORMAT versions
    up to the indexed one. Versions newer than the index (an upload in progress) are never touched."""
    by_format: dict[int, list[str]] = {}
    for name in names:
        m = NAME.match(name)
        if m:
            by_format.setdefault(int(m.group(1)), []).append(m.group(2))
    doomed: list[str] = []
    for fmt, versions in by_format.items():
        entry = index.get("packages", {}).get(str(fmt))
        if not entry:
            continue  # nothing indexed for this format: not ours to judge
        indexed = datapkg.version_key(str(entry["version"]))
        old = sorted((v for v in versions if datapkg.version_key(v) <= indexed), key=datapkg.version_key)
        for version in old[:-KEEP_PER_FORMAT]:
            file = f"rune-advisor-data-{fmt}-{version}.tar.xz"
            doomed += [file] + ([file + ".sig"] if file + ".sig" in names else [])
    return doomed


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "rune-advisor-publish", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", nargs="?", type=Path, help="the .tar.xz from build_data_package.py (its .sig beside it)")
    ap.add_argument("--dry-run", action="store_true", help="check everything, list the server, change nothing")
    ap.add_argument("--prune-only", action="store_true", help="only delete superseded packages")
    ap.add_argument("--no-prune", action="store_true", help="publish without deleting superseded packages")
    ap.add_argument("--no-verify", action="store_true", help="skip fetching the files back over HTTPS")
    ap.add_argument("--public-key", action="append", default=[], help="an extra base64 public key to verify with")
    ap.add_argument("--host", default="dl.runeadvisor.com")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--user", default="rune-dl")
    ap.add_argument("--remote-dir", default="/data")
    ap.add_argument("--base-url", default="https://dl.runeadvisor.com", help="where the files are served (<base>/data/...)")
    ap.add_argument("--ssh-option", action="append", default=[], help="passed to sftp, e.g. --ssh-option=-oIdentityFile=PATH")
    args = ap.parse_args()
    if not args.prune_only and args.package is None:
        ap.error("give the package file (or --prune-only)")
    remote = Remote(args)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            if not args.prune_only:
                fmt, version, sig = check_local(args.package, args.public_key)
                print(f"package ok: format {fmt}, version {version}, {args.package.stat().st_size / 1e6:.1f} MB")
            names = remote.listing()
            index = remote.index(scratch)
            print(f"on the server: {len(names)} file(s); index: "
                  + (", ".join(f"format {k} -> {v.get('version')}" for k, v in sorted(index["packages"].items())) or "empty"))
            if not args.prune_only:
                name = args.package.name
                if name in names or name + ".sig" in names:
                    raise Refused(f"{name} is already on the server: a published package is never replaced "
                                  "(build a new version)")
                current = index["packages"].get(str(fmt))
                if current and datapkg.version_key(str(current["version"])) >= datapkg.version_key(version):
                    raise Refused(f"the index already offers format {fmt} version {current['version']}, which is not "
                                  f"older than {version}: apps would refuse this as a downgrade")
                # 3. the package and its signature, complete before they carry their real names
                remote.run([f"put {args.package} {name}.part", f"put {sig} {name}.sig.part",
                            f"rename {name}.part {name}", f"rename {name}.sig.part {name}.sig"])
                # 4. the index, last
                index["packages"][str(fmt)] = {"version": version, "file": name}
                new_index = scratch / "index.json"
                new_index.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
                remote.run([f"put {new_index} index.json.tmp", "rename index.json.tmp index.json"])
                print(f"uploaded {name}, {name}.sig and the index" if not args.dry_run else "dry run: nothing uploaded")
                names = sorted({*names, name, name + ".sig", "index.json"})
                # 5. what the world now sees
                if not args.no_verify and not args.dry_run:
                    base = args.base_url.rstrip("/") + "/data/"
                    served = json.loads(fetch(base + "index.json"))
                    if served.get("packages", {}).get(str(fmt)) != index["packages"][str(fmt)]:
                        raise Refused(f"{base}index.json does not show the new entry (uploaded, but not served: "
                                      "check the server; nothing was pruned)")
                    for local in (args.package, sig):
                        if hashlib.sha256(fetch(base + local.name)).digest() != hashlib.sha256(local.read_bytes()).digest():
                            raise Refused(f"{base}{local.name} differs from the local file (nothing was pruned)")
                    print(f"verified over HTTPS: {base}index.json, {name}, {name}.sig")
            # 6. tidy up
            if not args.no_prune:
                doomed = superseded(names, index)
                if doomed:
                    remote.run([f"rm {file}" for file in doomed])
                print(("would remove: " if args.dry_run else "removed: ") + (", ".join(doomed) or "nothing"))
    except Refused as e:
        print(f"publish: {e}", file=sys.stderr)
        return 1
    print("publish: dry run complete" if args.dry_run else "publish: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
