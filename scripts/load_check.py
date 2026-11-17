#!/usr/bin/env python3
"""Load check for the export API: N simulated plugins posting at the server's interval.

    uv run python scripts/load_check.py                              # spawns a throwaway server, stages 50,200,500
    python scripts/load_check.py --plugins 100 --duration 120        # one stage
    python scripts/load_check.py --url http://127.0.0.1:8766         # an instance somebody else started (see below)

What it does. Each simulated plugin is one account with a submit and a read token and one character the size
of a real one (about 25 KB as a full post: every quest/achievement var slot from the packaged game configs, 60
named vars, 29 skills, a bank of 1,500 items). It posts the full state once, then a delta (one skill, one named
var, one bank item) every `--interval` seconds, each on a new connection, as nginx forwards them; every
`--read-every` seconds its planner reads the snapshot. Starts are spread over one interval. These are real
wire-protocol messages: the server unpacks, applies and hashes them exactly as it does a plugin's.

By default the script starts its own server (`python -m rune_advisor.export_api serve`) on a free loopback port
with a temporary data dir, raises that instance's account-creation limit, measures the process (peak memory,
CPU time, threads) from /proc, and removes everything afterwards. It never touches another data dir.

With `--url` it uses a running instance instead. That instance must be a throwaway one: the script makes
`--plugins` accounts in it, so first run `export_api --data-dir <scratch> settings set account_ip_limit 100000`
there. Anything but a loopback address is refused: production through nginx would only measure nginx's rate
limit and would leave test accounts behind. Give `--pid` to have the server process measured.

Like the plugin, a simulated plugin follows the interval the server hands out in every reply, and after a
refused post ("busy" 503, rate limit 429, no answer) holds off for a minute. `shed_requests` counts the 503/429
answers: under overload they are the server working as designed, not failures. `--no-obey` makes the clients
ignore both, to see the server defend itself.

Output: `key = value` lines per stage, then `load_check: ok` or `load_check: FAILED (...)`. Exit 0 when no
request of any stage failed (no answer, or an answer other than the expected one, 503/429 aside) and 95% of
delta posts took under `--p95-limit` seconds (1.0), 1 otherwise, 2 when the run could not start.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import random
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor import wire  # noqa: E402
from rune_advisor.sources.game_configs import GameConfigs  # noqa: E402

SKILLS = ["ATTACK", "DEFENCE", "STRENGTH", "HITPOINTS", "RANGED", "PRAYER", "MAGIC", "COOKING", "WOODCUTTING",
          "FLETCHING", "FISHING", "FIREMAKING", "CRAFTING", "SMITHING", "MINING", "HERBLORE", "AGILITY", "THIEVING",
          "SLAYER", "FARMING", "RUNECRAFTING", "HUNTER", "CONSTRUCTION", "SUMMONING", "DUNGEONEERING", "DIVINATION",
          "INVENTION", "ARCHAEOLOGY", "NECROMANCY"]
NAMED = ["QP"] + [f"LOADCHECK_VAR_{i}" for i in range(59)]  # the server takes the names from the post
INVENTORIES = ["BANK", "INV", "WORN", "MONEY_POUCH", "METAL_BANK", "CURRENCY_POUCH"]
LOOPBACK = ("127.0.0.1", "localhost", "::1")
RETRY_HOLD_SECONDS = 60.0  # what the plugin waits after a failed post (RETRY_HOLD_TICKS in lib/wire.lua)
SHED = (429, 503)  # "slow down" answers: counted apart from failures


class Api:
    """A tiny JSON client: one connection per request, the way nginx forwards to the service."""

    def __init__(self, url: str):
        parts = urlsplit(url)
        self.host, self.port, self.prefix = parts.hostname or "127.0.0.1", parts.port or 80, parts.path.rstrip("/")

    def call(self, method: str, path: str, body: dict | None = None, auth: str | None = None,
             player: str | None = None) -> tuple[int, dict, float]:
        headers = {"Content-Type": "application/json"}
        if player:
            headers["X-Player"] = player.replace(" ", "%20")
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        start = time.perf_counter()
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
            conn.request(method, self.prefix + path, body=None if body is None else json.dumps(body), headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            return resp.status, data, time.perf_counter() - start
        except (OSError, http.client.HTTPException) as e:
            return 0, {"reason": f"{type(e).__name__}: {e}"}, time.perf_counter() - start


class Results:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.times: dict[str, list[float]] = {}
        self.statuses: dict[str, dict[int, int]] = {}
        self.unexpected: list[str] = []
        self.interval_seen: float | None = None
        self.interval_max = 0.0

    def add(self, kind: str, status: int, seconds: float, expected: int, reply: dict) -> bool:
        with self.lock:
            self.times.setdefault(kind, []).append(seconds)
            by = self.statuses.setdefault(kind, {})
            by[status] = by.get(status, 0) + 1
            if isinstance(reply.get("interval"), (int, float)):
                self.interval_seen = float(reply["interval"])
                self.interval_max = max(self.interval_max, self.interval_seen)
            if status != expected and len(self.unexpected) < 5:
                self.unexpected.append(f"{kind}: {status} {str(reply.get('reason', ''))[:100]}")
        return status == expected


def encode_delta(layout: wire.WireLayout, slots: list[tuple[int, int]], items: list[tuple[int, int, int]]) -> bytes:
    """The plugin's delta body (what `wire.apply_delta` reads): changed slots, then changed items."""
    w = wire._Writer()
    w.write(len(slots), 16)
    for index, value in slots:
        w.write(index, 16)
        w.write(value, layout.slots[index].width)
    w.write(len(items), 16)
    for inv, item_id, count in items:
        w.write(inv, 8)
        w.write(item_id, 32)
        w.write(count, 32)
    return w.bytes()


def make_state(layout: wire.WireLayout, rng: random.Random, bank_items: int) -> wire.WireState:
    slots = [rng.getrandbits(s.width) if s.kind == "base" else rng.randrange(0, 200_000_000) if s.kind == "skill"
             else rng.randrange(0, 1 << 20) if s.kind == "named" else 1 for s in layout.slots]
    ids = rng.sample(range(1, 60_000), bank_items + 28 + 14)
    invs = {"BANK": {i: rng.randrange(1, 100_000) for i in ids[:bank_items]},
            "INV": {i: rng.randrange(1, 1000) for i in ids[bank_items:bank_items + 28]},
            "WORN": {i: 1 for i in ids[bank_items + 28:]},
            "MONEY_POUCH": {995: rng.randrange(1, 2_000_000_000)}}  # METAL_BANK, CURRENCY_POUCH: not loaded (unknown)
    return wire.WireState(slots, invs)


def plugin(api: Api, layout: wire.WireLayout, index: int, args: argparse.Namespace, results: Results,
           start_at: float, stop_at: float, fingerprint: int) -> None:
    rng = random.Random(index)
    player = f"Load {index:06d}"
    time.sleep(max(0.0, start_at - time.time()))
    # Setup: an account, a submit token for the "plugin" and a read token for the "planner".
    status, made, took = api.call("POST", "/v1/accounts", {})
    if not results.add("account", status, took, 201, made):
        return
    tokens = {}
    for scope in ("submit", "read"):
        status, reply, took = api.call("POST", "/v1/tokens", {"label": scope, "scopes": [scope]}, auth=made["key"])
        if not results.add("token", status, took, 201, reply):
            return
        tokens[scope] = reply["token"]

    state = make_state(layout, rng, args.bank_items)
    skill_slots = [i for i, s in enumerate(layout.slots) if s.kind == "skill"]
    named_slots = [i for i, s in enumerate(layout.slots) if s.kind == "named"]
    bank = sorted(state.invs["BANK"])
    seq = 0

    def full() -> bool:
        nonlocal seq
        seq += 1
        h = wire.state_hash(layout, state)
        post = {"player": player, "kind": "full", "layout": layout.hash, "base": None, "new": h, "seq": seq,
                "body": base64.b64encode(wire.canonical(layout, state)).decode(), "named": layout.named,
                "skills": layout.skills, "inventories": layout.inventories, "configs": fingerprint, "plugin": "load-check"}
        status, reply, took = api.call("POST", "/v1/state", post, auth=tokens["submit"])
        return results.add("full", status, took, 200, reply)

    if not full():
        return
    next_post = time.time() + args.interval
    next_read = time.time() + rng.uniform(0, args.read_every)
    while True:
        now = time.time()
        wake = min(next_post, next_read)
        if wake >= stop_at:
            return
        time.sleep(max(0.0, wake - now))
        if next_read <= next_post:
            status, reply, took = api.call("GET", "/v1/snapshot", auth=tokens["read"], player=player)
            results.add("snapshot", status, took, 200, reply)
            next_read += args.read_every
            continue
        # A delta: a skill gains xp, a named var moves, a bank stack changes.
        before = wire.state_hash(layout, state)
        s, n, item = rng.choice(skill_slots), rng.choice(named_slots), rng.choice(bank)
        state.slots[s] = min(200_000_000, state.slots[s] + rng.randrange(1, 5000))
        state.slots[n] = rng.randrange(0, 1 << 20)
        state.invs["BANK"][item] = rng.randrange(1, 100_000)
        body = encode_delta(layout, [(s, state.slots[s]), (n, state.slots[n])], [(0, item, state.invs["BANK"][item])])
        seq += 1
        post = {"player": player, "kind": "delta", "layout": layout.hash, "base": before,
                "new": wire.state_hash(layout, state), "seq": seq, "body": base64.b64encode(body).decode()}
        status, reply, took = api.call("POST", "/v1/state", post, auth=tokens["submit"])
        results.add("delta", status, took, 200, reply)
        pace = args.interval
        if args.obey and isinstance(reply.get("interval"), (int, float)):
            pace = max(args.interval, float(reply["interval"]))  # a plugin follows the interval every reply carries
        if status == 200:
            next_post += pace
        elif status == 409:  # out of step: the plugin sends its full state
            if not full():
                return
            next_post += pace
        else:
            # Busy, rate-limited or no answer: the plugin holds off about a minute, then (in this simulation; the
            # real one retries the same delta) sends its full state.
            hold = RETRY_HOLD_SECONDS if args.obey else pace
            if time.time() + hold >= stop_at:
                return
            time.sleep(hold)
            if not full():
                return
            next_post = time.time() + pace


def proc_usage(pid: int) -> dict[str, float] | None:
    """Peak and current memory (MB), CPU seconds and thread count of a process, from /proc (Linux)."""
    try:
        status = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        return {"peak_mb": int(status["VmHWM"].split()[0]) / 1024, "rss_mb": int(status["VmRSS"].split()[0]) / 1024,
                "cpu_s": (int(fields[11]) + int(fields[12])) / ticks, "threads": float(status["Threads"].strip())}
    except (OSError, KeyError, ValueError, IndexError):
        return None


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def run_stage(api: Api, layout: wire.WireLayout, plugins: int, offset: int, args: argparse.Namespace, pid: int | None,
              fingerprint: int) -> bool:
    results = Results()
    begin = time.time()
    stop_at = begin + args.interval + args.duration  # one interval of staggered starts, then the measured run
    before = proc_usage(pid) if pid else None
    peak_threads = 0.0
    threads = [threading.Thread(target=plugin, daemon=True, args=(
        api, layout, offset + i, args, results, begin + args.interval * i / plugins, stop_at, fingerprint)) for i in range(plugins)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(1.0)
        usage = proc_usage(pid) if pid else None
        if usage:
            peak_threads = max(peak_threads, usage["threads"])
    elapsed = time.time() - begin
    after = proc_usage(pid) if pid else None

    print(f"stage_plugins = {plugins}")
    print(f"interval_s = {args.interval:g}")
    print(f"server_interval_s = {results.interval_seen if results.interval_seen is not None else 'unknown'}")
    print(f"elapsed_s = {elapsed:.1f}")
    total = sum(len(v) for v in results.times.values())
    print(f"requests = {total}")
    print(f"requests_per_s = {total / elapsed:.1f}")
    ok = True
    for kind in ("account", "token", "full", "delta", "snapshot"):
        times = results.times.get(kind, [])
        if not times:
            print(f"{kind}_count = 0")
            continue
        by = ", ".join(f"{status or 'no answer'}: {n}" for status, n in sorted(results.statuses[kind].items()))
        print(f"{kind}_count = {len(times)} ({by})")
        print(f"{kind}_ms = p50 {statistics.median(times) * 1000:.0f}, p95 {percentile(times, .95) * 1000:.0f}, "
              f"p99 {percentile(times, .99) * 1000:.0f}, max {max(times) * 1000:.0f}")
    expected = {"account": 201, "token": 201, "full": 200, "delta": 200, "snapshot": 200}
    shed = sum(n for by in results.statuses.values() for status, n in by.items() if status in SHED)
    failed = sum(n for kind, by in results.statuses.items() for status, n in by.items()
                 if status != expected[kind] and status not in SHED and not (kind == "delta" and status == 409))
    print(f"shed_requests = {shed}")
    print(f"failed_requests = {failed}")
    print(f"server_interval_max_s = {results.interval_max:g}")
    for line in results.unexpected:
        print(f"unexpected: {line}")
    deltas = results.times.get("delta", [])
    slow = bool(deltas) and percentile(deltas, .95) > args.p95_limit
    want = plugins * max(1, int(args.duration / args.interval) - 1)
    print(f"delta_posts_expected_at_least = {want}")
    if before and after:
        print(f"server_peak_memory_mb = {after['peak_mb']:.0f}")
        print(f"server_memory_mb = {after['rss_mb']:.0f}")
        print(f"server_cpu_s = {after['cpu_s'] - before['cpu_s']:.1f}")
        print(f"server_cpu_percent_of_one_core = {100 * (after['cpu_s'] - before['cpu_s']) / elapsed:.0f}")
        print(f"server_peak_threads = {peak_threads:.0f}")
    stretched = results.interval_max > args.interval  # the server slowed the plugins down: fewer posts is the point
    behind = len(deltas) < want and not stretched and not shed
    ok = failed == 0 and not slow and not behind
    print(f"stage = {'ok' if ok else 'FAILED'}" + ("" if ok else
          f" ({failed} failed request(s)" + (", p95 over the limit" if slow else "")
          + (", fewer delta posts than the interval allows: the server or this machine fell behind" if behind else "") + ")"))
    print()
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="a running throwaway instance on loopback; default: start one")
    ap.add_argument("--pid", type=int, help="with --url: the server's process id, to measure memory and CPU")
    ap.add_argument("--plugins", default="50,200,500", help="simulated plugins per stage, comma-separated")
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between a plugin's posts (the server default)")
    ap.add_argument("--duration", type=float, default=60.0, help="seconds each stage runs after its plugins have started")
    ap.add_argument("--read-every", type=float, default=60.0, help="seconds between a plugin's planner reads")
    ap.add_argument("--bank-items", type=int, default=1500)
    ap.add_argument("--no-obey", dest="obey", action="store_false",
                    help="ignore the interval the server hands out and the minute's hold after a refused post "
                         "(a misbehaving client; default: behave like the plugin)")
    ap.add_argument("--p95-limit", type=float, default=1.0, help="seconds; a stage fails when delta posts' p95 is above it")
    args = ap.parse_args()
    try:
        stages = [int(x) for x in args.plugins.split(",") if x.strip()]
    except ValueError:
        ap.error("--plugins takes numbers")
    configs = GameConfigs.read()
    if configs is None:
        print("load_check: no packaged game configs to build a layout from")
        return 2
    layout = wire.WireLayout.build(configs, NAMED, SKILLS, INVENTORIES)
    size = len(base64.b64encode(wire.canonical(layout, make_state(layout, random.Random(0), args.bank_items))))
    print(f"full_post_body_bytes = {size}")
    print(f"layout_slots = {len(layout.slots)}")

    server, scratch, pid = None, None, args.pid
    if args.url:
        if (urlsplit(args.url).hostname or "") not in LOOPBACK:
            print("load_check: --url must be a loopback address (a throwaway instance, never production)")
            return 2
        url = args.url
    else:
        scratch = Path(tempfile.mkdtemp(prefix="rune-advisor-load-"))
        (scratch / "settings.json").write_text(json.dumps({"account_ip_limit": 1_000_000, "max_tokens": 20}))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = subprocess.Popen([sys.executable, "-m", "rune_advisor.export_api", "--data-dir", str(scratch), "serve",
                                   "--listen", f"127.0.0.1:{port}", "--prefix", ""],
                                  env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url, pid = f"http://127.0.0.1:{port}", server.pid
    api = Api(url)
    try:
        for _ in range(100):
            if api.call("GET", "/v1/health")[0] == 200:
                break
            time.sleep(0.1)
        else:
            print(f"load_check: no healthy server at {url}")
            return 2
        print(f"server = {url}" + (" (started by this script)" if server else ""))
        print()
        ok, offset = True, 0
        for plugins in stages:
            ok = run_stage(api, layout, plugins, offset, args, pid, configs.fingerprint) and ok
            offset += plugins
        if scratch:
            db = scratch / "export_api.sqlite3"
            print(f"database_mb = {db.stat().st_size / 1e6:.1f} ({offset} characters)")
        print("load_check: ok" if ok else "load_check: FAILED (see the stages above)")
        return 0 if ok else 1
    finally:
        if server:
            server.terminate()
            try:
                server.wait(10)
            except subprocess.TimeoutExpired:
                server.kill()
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
