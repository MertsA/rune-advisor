"""Serves the web planner built by scripts/build_web.py on this machine, with an export API of its own.

    uv run scripts/build_web.py --api http://127.0.0.1:8766
    uv run scripts/serve_web.py [--copy-character NAME ...]

Two loopback servers:
  the site      http://127.0.0.1:8765   <out> as static files, with the headers the real site needs for the
                                         planner (a Content-Security-Policy that allows WebAssembly and the API)
  the API       http://127.0.0.1:8766   `rune_advisor.export_api serve` on a throwaway data folder
                                         (<out>/../web-dev/api), allowing the site's origin

The API starts with one account, "dev". Its account key is written to <out>/../web-dev/account-key.txt: paste it
into the account page, which hands the planner a read token. --copy-character copies a character's stored state
from the API in rune_advisor.toml ([api] url and a read token) into that account, so the planner has something
real to plan for; nothing is sent back.
"""

from __future__ import annotations

import argparse
import functools
import json
import http.server
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ACCOUNT = "dev"


def csp(api: str, assets: str = "") -> str:
    """The real site's policy (default-src 'self', connect-src the API) plus what Pyodide needs: compiling
    WebAssembly, and its files from the assets host when that is not this site (Pyodide imports its runtime
    module from there and fetches the rest). The worker itself is same-origin."""
    extra = f" {assets}" if assets else ""
    return (f"default-src 'self'; script-src 'self' 'wasm-unsafe-eval'{extra}; style-src 'self'; "
            f"connect-src 'self' {api}{extra}; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def assets_origin(manifest: Path) -> str:
    """The origin of the build's planner assets when they are not on this site ('' when they are)."""
    url = json.loads(manifest.read_text()).get("assets", "")
    return "/".join(url.split("/")[:3]) if "://" in url else ""


class Site(http.server.SimpleHTTPRequestHandler):
    api = ""
    assets = ""
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".wasm": "application/wasm", ".mjs": "text/javascript", ".js": "text/javascript"}

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", csp(self.api, self.assets))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # What the live site should do too: the hashed and versioned assets never change; the rest is revalidated.
        immutable = self.path.startswith("/planner-assets/")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if immutable else "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args) -> None:
        pass


def api_cli(data: Path, *args: str) -> str:
    return subprocess.run([sys.executable, "-m", "rune_advisor.export_api", "--data-dir", str(data), *args],
                          cwd=ROOT, check=True, capture_output=True, text=True, env=_env()).stdout.strip()


def _env() -> dict:
    import os
    return {**os.environ, "PYTHONPATH": str(ROOT / "src")}


def copy_character(data: Path, name: str) -> str:
    """One character's stored state, read from the configured API and saved under the local account. A config
    set the local package lacks comes along, so the local API can rebuild the snapshot."""
    from rune_advisor import api_client, export_api
    from rune_advisor.config import load_settings
    from rune_advisor.sources import game_configs
    from rune_advisor.sources.game_configs import GameConfigs

    settings = load_settings()
    if not (settings.api_url and settings.api_token):
        return f"{name}: no [api] url and token in rune_advisor.toml"
    for token in settings.api_tokens:
        listing = api_client.players(settings, token)
        actual = next((p["player"] for p in listing if (p.get("player") or "").lower() == name.lower()), None)
        if actual is None:
            continue
        record = api_client._get(settings, "v1/state", token, player=actual)
        if not isinstance(record, dict) or "state" not in record:
            return f"{name}: the API returned no stored state"
        store = export_api.Store(data)
        fp = record.get("configs")
        local = game_configs.load()
        if isinstance(fp, int) and (local is None or local.fingerprint != fp) and store.load_generation(fp) is None:
            body = api_client._get(settings, f"v1/configs/0x{fp:08x}", token)
            configs = GameConfigs.from_dumps({game_configs.DUMP_KEYS["quests"]: body.get("quests"),
                                              game_configs.DUMP_KEYS["achievements"]: body.get("achievements")})
            configs.write(store.generation_dir(fp))
        store.save_state(ACCOUNT, actual, record)
        return f"{actual}: copied (exported {time.strftime('%Y-%m-%d %H:%M', time.localtime(record.get('updated') or 0))})"
    return f"{name}: not under the configured API tokens"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "web")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--api-port", type=int, default=8766)
    ap.add_argument("--copy-character", action="append", default=[], metavar="NAME")
    args = ap.parse_args()
    out = args.out.resolve()
    if not (out / "planner" / "manifest.json").exists():
        sys.exit(f"{out} has no planner: run scripts/build_web.py first")
    dev = out.parent / "web-dev"
    data = dev / "api"
    data.mkdir(parents=True, exist_ok=True)
    origin, api = f"http://127.0.0.1:{args.port}", f"http://127.0.0.1:{args.api_port}"
    if f'content="{api}"' not in (out / "planner" / "index.html").read_text():
        sys.exit(f"the build does not point at {api}: rebuild with scripts/build_web.py --api {api}")

    key_file = dev / "account-key.txt"
    if not key_file.exists():
        api_cli(data, "token", "new", ACCOUNT, "--scopes", "submit", "--label", "plugin")  # makes the account
        key_file.touch(mode=0o600)
        key_file.write_text(api_cli(data, "account", "key", ACCOUNT).split()[-1] + "\n")
    for name in args.copy_character:
        print(copy_character(data, name))

    server = subprocess.Popen([sys.executable, "-m", "rune_advisor.export_api", "--data-dir", str(data), "serve",
                               "--listen", f"127.0.0.1:{args.api_port}", "--cors-origin", origin, "--prefix", "/"],
                              cwd=ROOT, env=_env())
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{api}/v1/health", timeout=1).read()
                break
            except OSError:
                if server.poll() is not None:
                    sys.exit("the API did not start")
                time.sleep(0.2)
        Site.api = api
        Site.assets = assets_origin(out / "planner" / "manifest.json")
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), functools.partial(Site, directory=str(out)))
        print(f"site     {origin}/planner/")
        print(f"API      {api}")
        print(f"account  key in {key_file}", flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
