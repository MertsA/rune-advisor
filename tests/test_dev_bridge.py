"""The dev bridge (plugins/rune_advisor_dev/lib/bridge.lua and private/scripts/dev_bridge.py): both live in the private
overlay, so these skip in a public checkout."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "private" / "scripts" / "dev_bridge.py"
needs_bridge = pytest.mark.skipif(
    not (ROOT / "plugins" / "rune_advisor_dev" / "lib" / "bridge.lua").exists() or not SERVER.exists(),
    reason="the dev plugin and private/scripts are not in this checkout")


@needs_bridge
def test_plugin_side_against_the_mock():
    lua = shutil.which("lua5.4") or shutil.which("lua")
    if not lua:
        pytest.skip("no Lua interpreter")
    out = subprocess.run([lua, "plugins/rune_advisor_dev/tests/run_bridge.lua"], cwd=ROOT, capture_output=True,
                         text=True)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr


def _server():
    spec = importlib.util.spec_from_file_location("dev_bridge", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bridge = mod.Bridge(hold=0.2, log_dir=None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.make_handler(bridge))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, bridge


def _post(port: int, path: str, body: dict) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


@needs_bridge
def test_server_relays_commands_results_and_events():
    httpd, bridge = _server()
    port = httpd.server_address[1]
    try:
        assert _post(port, "/streams", {"var": True, "bogus": True}) == {"var": True, "inventory": False,
                                                                         "xp": False, "chat": False, "chatmeta": False}
        # The agent asks; a fake plugin polls, gets the command and answers on its next poll.
        answer = {}
        asker = threading.Thread(target=lambda: answer.update(
            _post(port, "/cmd", {"cmd": "var", "args": {"names": ["QP"]}, "timeout": 5})))
        asker.start()
        cmd = None
        for _ in range(50):
            reply = _post(port, "/v1/poll", {"player": "PlayerOne", "events": [], "results": []})
            assert reply["streams"]["var"] is True
            if reply["commands"]:
                cmd = reply["commands"][0]
                break
        assert cmd["cmd"] == "var" and cmd["args"] == {"names": ["QP"]}
        _post(port, "/v1/poll", {"player": "PlayerOne", "results": [
            {"id": cmd["id"], "ok": True, "result": {"values": {"QP": 473}}}],
            "events": [{"kind": "var", "name": "QP", "old": 472, "new": 473}]})
        asker.join(5)
        assert answer["ok"] and answer["result"] == {"values": {"QP": 473}}
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/events?after=0&kind=var", timeout=5) as r:
            events = json.loads(r.read())
        assert len(events) == 1 and events[0]["seq"] == 1 and events[0]["player"] == "PlayerOne"
        status = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5).read())
        assert status["connected"] and status["player"] == "PlayerOne" and status["events"] == 1
        # Nobody polling: the agent hears so instead of hanging.
        bridge.last_poll = None
        lost = _post(port, "/cmd", {"cmd": "ping", "timeout": 0.3})
        assert not lost["ok"] and "isn't polling" in lost["error"]
    finally:
        httpd.shutdown()
