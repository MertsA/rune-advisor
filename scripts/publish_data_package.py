#!/usr/bin/env python3
"""Publish a signed data package to dl.runeadvisor.com (docs/data-package.md, "Cutting a release").

    uv run python scripts/publish_data_package.py dist/data/rune-advisor-data-1-2026.11.14.tar.xz
    uv run python scripts/publish_data_package.py <package> --dry-run        # make the commit, push nothing
    uv run python scripts/publish_data_package.py --prune-only               # only remove superseded packages

The package comes from `scripts/build_data_package.py`; its `.sig` must lie beside it. Run this where the SSH
key for the upload account is (the user's own machine: the key is not in this repo, a sandbox or a server).

What `dl.runeadvisor.com` serves is the tree of branch `main` in a git repository (`rune-dl@upload.runeadvisor.com:
dl.git`). A push to it is the upload: the server's hook exports the pushed commit and switches to it at once, so
the new index and the package it names appear together. This script owns `data/` in that tree and touches
nothing else (`planner-assets/` and `_headers` are pushed by the server's operators).

What it does, in this order:

1. Checks the package locally: the file name, that the signature verifies against the app's public keys
   (`datapkg.PUBLIC_KEYS`; refused when there are none, unless `--public-key` gives one), that the archive
   unpacks under the app's own rules and its manifest carries the version and format in the name.
2. Clones the repository (an empty one, before the first publish, means no index and no packages) and reads
   `data/index.json`. A published file is never overwritten: a package of the same name is refused, and so is a
   version not newer than the indexed one (apps would refuse it as a downgrade).
3. Makes one commit under `data/`: the package and its signature, the new `index.json` (keeping the other
   formats' entries, so older apps keep finding their own newest package), and the removal of superseded
   packages (per format the indexed one and the one before it stay, older ones go).
4. Pushes it to `main` as a plain fast-forward. When `main` moved meanwhile (another push of assets), it fetches,
   makes the commit again on the new tip and pushes again; never a force push.
5. Fetches the index, the package and the signature back over HTTPS and compares them with the local files,
   retrying for `--verify-seconds` (a CDN in front can take a minute to show a new deployment).

The remote is reached by git's own ssh (so the SSH agent, `~/.ssh/config` and `GIT_SSH_COMMAND` work as usual;
without `GIT_SSH_COMMAND` the script runs `ssh -o BatchMode=yes`). `--remote` points it elsewhere (the tests use a
local bare repository), `--base-url` at another web server.

Exit 0 published and verified, 1 refused or failed (the message says at which step and what is on the server),
2 usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rune_advisor import datapkg  # noqa: E402

REMOTE = "rune-dl@upload.runeadvisor.com:dl.git"  # always gluon itself, whatever dl. points at
BASE_URL = "https://dl.runeadvisor.com"
BRANCH = "main"  # the only ref the server accepts
DATA = "data"  # this script's part of the tree
NAME = re.compile(r"^rune-advisor-data-([0-9]+)-([0-9]+(?:\.[0-9]+){0,3})\.tar\.xz$")  # what the server serves
KEEP_PER_FORMAT = 2  # the indexed package and the one before it (an app's rollback target)
PUSH_ATTEMPTS = 5


class Refused(Exception):
    pass


class Moved(Exception):
    """The push was refused because `main` is no longer where the commit was made."""


def git_env() -> dict[str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


class Repo:
    """A scratch clone of the download tree."""

    def __init__(self, remote: str, path: Path):
        self.remote, self.path = remote, path

    def git(self, *args: str, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
        done = subprocess.run(["git", *args], cwd=cwd or self.path, env=git_env(), capture_output=True, text=True)
        if check and done.returncode != 0:
            raise Refused(f"git {args[0]} failed ({done.returncode}): {(done.stderr or done.stdout).strip()[-400:]}")
        return done

    def clone(self) -> None:
        self.git("clone", "--quiet", "--no-checkout", self.remote, str(self.path), cwd=self.path.parent)
        if not self.git("config", "user.email", check=False).stdout.strip():
            self.git("config", "user.name", "Rune Advisor publish")
            self.git("config", "user.email", "support@runeadvisor.com")

    def fetch(self) -> None:
        self.git("fetch", "--quiet", "origin")

    def tip(self) -> str | None:
        """The remote's `main`, None while the repository has no commits."""
        done = self.git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{BRANCH}^{{commit}}", check=False)
        return done.stdout.strip() or None

    def reset(self) -> str | None:
        """Work tree and index at the remote's `main` (empty before the first publish); returns that commit."""
        tip = self.tip()
        if tip:
            self.git("checkout", "--quiet", "--force", "-B", BRANCH, tip)
            self.git("clean", "--quiet", "-dfx")
        else:
            self.git("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
            self.git("rm", "--quiet", "-rf", "--cached", "--ignore-unmatch", ".")
            for child in self.path.iterdir():
                if child.name != ".git":
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
        return tip

    def data_names(self) -> list[str]:
        folder = self.path / DATA
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def index(self) -> dict:
        file = self.path / DATA / "index.json"
        if not file.exists():
            return {"packages": {}}
        try:
            index = json.loads(file.read_text())
            if not isinstance(index.get("packages"), dict):
                raise ValueError("no packages table")
        except ValueError as e:
            raise Refused(f"the published data/index.json is unusable ({e}); fix it by hand before publishing") from None
        return index

    def changed_paths(self) -> list[str]:
        self.git("add", "--all", DATA)
        out = self.git("diff", "--cached", "--name-only", "--no-renames", "-z", check=False).stdout
        return [p for p in out.split("\0") if p]

    def push(self) -> str:
        """Pushes `main`; returns the server's messages. Raises Moved when the push is not a fast-forward."""
        done = self.git("push", "--porcelain", "origin", f"HEAD:refs/heads/{BRANCH}", check=False)
        remote_lines = "\n".join(line for line in done.stderr.splitlines() if line.startswith("remote:"))
        if done.returncode == 0:
            return remote_lines
        text = done.stdout + done.stderr
        if "[rejected]" in text and ("non-fast-forward" in text or "fetch first" in text):
            raise Moved(text.strip()[-300:])
        raise Refused(f"git push failed ({done.returncode}): {text.strip()[-400:]}")


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
    up to the indexed one. Versions newer than the index are never touched."""
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


def describe(index: dict) -> str:
    return ", ".join(f"format {k} -> {v.get('version')}" for k, v in sorted(index["packages"].items())) or "empty"


def prepare(repo: Repo, args: argparse.Namespace, local: tuple[int, str, Path] | None) -> tuple[str | None, list[str]]:
    """Builds the commit on the remote's current `main`. Returns (its message, files removed); no message when
    there is nothing to commit."""
    tip = repo.reset()
    names, index = repo.data_names(), repo.index()
    print(f"published ({tip[:12] if tip else 'no commits yet'}): {sum(1 for n in names if NAME.match(n))} "
          f"package(s); index: {describe(index)}")
    folder = repo.path / DATA
    message = None
    if local is not None:
        fmt, version, sig = local
        name = args.package.name
        for file in (args.package, sig):
            if (folder / file.name).exists():
                same = (folder / file.name).read_bytes() == file.read_bytes()
                raise Refused(f"data/{file.name} is already published{'' if same else ' with different content'}: "
                              "a published file is never replaced (build a new version)")
        current = index["packages"].get(str(fmt))
        if current and datapkg.version_key(str(current["version"])) >= datapkg.version_key(version):
            raise Refused(f"the index already offers format {fmt} version {current['version']}, which is not "
                          f"older than {version}: apps would refuse this as a downgrade")
        folder.mkdir(exist_ok=True)
        shutil.copyfile(args.package, folder / name)
        shutil.copyfile(sig, folder / sig.name)
        index["packages"][str(fmt)] = {"version": version, "file": name}
        (folder / "index.json").write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
        names = sorted({*names, name, sig.name, "index.json"})
        message = f"Publish {name}"
    doomed = [] if args.no_prune else superseded(names, index)
    for file in doomed:
        (folder / file).unlink()
    if doomed:
        message = (message + "; remove " if message else "Remove ") + ", ".join(doomed)
    changed = repo.changed_paths()
    stray = [p for p in changed if not p.startswith(DATA + "/")]
    if stray:
        raise Refused(f"the commit would touch files outside {DATA}/ ({', '.join(stray[:5])}): refusing")
    if not changed:
        return None, doomed
    repo.git("commit", "--quiet", "-m", message)
    return message, doomed


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "rune-advisor-publish", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def verify_served(base_url: str, entry: dict, files: list[Path], fmt: int, seconds: float) -> None:
    """The index entry and the files as the world sees them, retried until `seconds` have passed."""
    base = base_url.rstrip("/") + f"/{DATA}/"
    deadline = time.monotonic() + seconds
    while True:
        try:
            served = json.loads(fetch(base + "index.json"))
            if served.get("packages", {}).get(str(fmt)) != entry:
                raise Refused(f"{base}index.json does not show the new entry")
            for local in files:
                if hashlib.sha256(fetch(base + local.name)).digest() != hashlib.sha256(local.read_bytes()).digest():
                    raise Refused(f"{base}{local.name} differs from the local file")
            return
        except (Refused, OSError, ValueError) as e:
            if time.monotonic() >= deadline:
                raise Refused(f"pushed, but not served after {seconds:.0f} s: {e} (check the server)") from None
            time.sleep(min(10.0, max(0.5, seconds / 20)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", nargs="?", type=Path, help="the .tar.xz from build_data_package.py (its .sig beside it)")
    ap.add_argument("--dry-run", action="store_true", help="check everything and make the commit, push nothing")
    ap.add_argument("--prune-only", action="store_true", help="only remove superseded packages")
    ap.add_argument("--no-prune", action="store_true", help="publish without removing superseded packages")
    ap.add_argument("--no-verify", action="store_true", help="skip fetching the files back over HTTPS")
    ap.add_argument("--verify-seconds", type=float, default=180, help="how long to retry the HTTPS check (default 180)")
    ap.add_argument("--public-key", action="append", default=[], help="an extra base64 public key to verify with")
    ap.add_argument("--remote", default=REMOTE, help=f"the download tree's repository (default {REMOTE})")
    ap.add_argument("--base-url", default=BASE_URL, help="where the tree is served (<base>/data/...)")
    args = ap.parse_args(argv)
    if not args.prune_only and args.package is None:
        ap.error("give the package file (or --prune-only)")
    try:
        local = None if args.prune_only else check_local(args.package, args.public_key)
        if local:
            print(f"package ok: format {local[0]}, version {local[1]}, {args.package.stat().st_size / 1e6:.1f} MB")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(args.remote, Path(tmp) / "dl")
            repo.clone()
            for attempt in range(PUSH_ATTEMPTS):
                message, doomed = prepare(repo, args, local)
                if message is None:
                    print("nothing to change")
                    break
                if args.dry_run:
                    stat = repo.git("show", "--stat", "--format=%s", "HEAD").stdout.strip()
                    print("would push this commit to main:\n  " + "\n  ".join(stat.splitlines()))
                    break
                try:
                    said = repo.push()
                except Moved as e:
                    if attempt == PUSH_ATTEMPTS - 1:
                        raise Refused(f"main kept moving; gave up after {PUSH_ATTEMPTS} attempts: {e}") from None
                    print("main moved on the server meanwhile: fetching and making the commit again")
                    repo.fetch()
                    continue
                print(f"pushed: {message}" + (f"\n{said}" if said else ""))
                break
            if local and message and not args.dry_run and not args.no_verify:
                fmt, version, sig = local
                verify_served(args.base_url, {"version": version, "file": args.package.name}, [args.package, sig],
                              fmt, args.verify_seconds)
                print(f"verified over HTTPS: {args.base_url.rstrip('/')}/{DATA}/ index.json, "
                      f"{args.package.name}, {sig.name}")
            if doomed:
                print(("would remove: " if args.dry_run else "removed: ") + ", ".join(doomed))
    except Refused as e:
        print(f"publish: {e}", file=sys.stderr)
        return 1
    print("publish: dry run complete" if args.dry_run else "publish: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
