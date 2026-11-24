"""The desktop app's JSON API: auth, Host/Origin checks, jobs, view-model shapes, static files."""

import http.client
import json
import threading
import time

import pytest

from rune_advisor import views
from rune_advisor.app.jobs import JobManager
from rune_advisor.app.planner import Planner, normalise
from rune_advisor.app.server import STATIC, AppServer
from rune_advisor.cost import CostModel
from rune_advisor.recommend import Context
from rune_advisor.sources import wiki
from test_core import load, player  # noqa: F401  (the fixture)


class FakePlanner:
    """Stands in for Planner: a result per view, a gate to hold a job mid-run."""

    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.calls = 0
        self.forgot = 0

    def forget(self):
        self.forgot += 1

    def compute(self, view, params, progress):
        self.calls += 1
        progress(0, 2, "step one")
        self.gate.wait(5)
        progress(1, 2, "step two")
        if params["player"] == "Boom":
            raise RuntimeError("no data for Boom")
        return {"view": view, "player": params["player"], "n": self.calls}


@pytest.fixture
def srv(tmp_path):
    planner = FakePlanner()
    s = AppServer(planner=planner, secret="s3cret", prefs_path=tmp_path / "ui.json")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    s.fake = planner
    yield s
    s.shutdown()
    s.server_close()


def call(srv, method, path, body=None, headers=None, auth=True):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    h = {"Host": f"127.0.0.1:{srv.port}", **(headers or {})}
    if auth:
        h.setdefault("X-RA-Secret", "s3cret")
    data = None
    if body is not None:
        data = json.dumps(body)
        h.setdefault("Content-Type", "application/json")
    conn.request(method, path, data, h)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw), r
    except ValueError:
        return r.status, raw, r


def wait_job(srv, job_id, states=("done", "error", "cancelled")):
    for _ in range(100):
        status, j, _ = call(srv, "GET", f"/api/jobs/{job_id}")
        assert status == 200
        if j["state"] in states:
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_secret_required_everywhere(srv):
    for path in ("/api/meta", "/", "/app.js", "/api/jobs/x"):
        assert call(srv, "GET", path, auth=False)[0] == 403
    assert call(srv, "GET", "/api/meta", headers={"X-RA-Secret": "wrong"})[0] == 403
    assert call(srv, "POST", "/api/jobs", {"view": "profile", "player": "PlayerOne"}, auth=False)[0] == 403
    assert call(srv, "GET", "/api/meta")[0] == 200


def test_cookie_exchange_sets_cookie_and_hides_secret(srv):
    status, _, r = call(srv, "GET", "/?s=s3cret", auth=False)
    assert status == 302 and r.getheader("Location") == "/"
    cookie = r.getheader("Set-Cookie")
    assert "ra_secret=s3cret" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    status, body, _ = call(srv, "GET", "/", headers={"Cookie": "ra_secret=s3cret"}, auth=False)
    assert status == 200 and b"Rune Advisor" in body
    assert call(srv, "GET", "/?s=nope", auth=False)[0] == 403


def test_host_and_origin_checked(srv):
    assert call(srv, "GET", "/api/meta", headers={"Host": "evil.example"})[0] == 403
    assert call(srv, "GET", "/api/meta", headers={"Host": f"rebind.example:{srv.port}"})[0] == 403
    assert call(srv, "GET", "/?s=s3cret", headers={"Host": "evil.example"}, auth=False)[0] == 403
    assert call(srv, "GET", "/api/meta", headers={"Origin": "http://evil.example"})[0] == 403
    assert call(srv, "POST", "/api/jobs", {"view": "profile", "player": "PlayerOne"},
                headers={"Origin": "http://evil.example"})[0] == 403
    assert call(srv, "GET", "/api/meta", headers={"Origin": srv.origin})[0] == 200


def test_post_needs_json(srv):
    status, _, _ = call(srv, "POST", "/api/jobs", None, headers={"Content-Type": "text/plain"})
    assert status == 415


def test_job_lifecycle_cache_and_refresh(srv):
    status, job, _ = call(srv, "POST", "/api/jobs", {"view": "profile", "player": "PlayerOne"})
    assert status == 202 and job["state"] != "error"
    done = wait_job(srv, job["id"])
    assert done["state"] == "done" and done["result"]["view"] == "profile" and not done["cached"]
    assert done["progress"]["message"] == "step two"
    # Same request: from the cache, no second compute.
    _, again, _ = call(srv, "POST", "/api/jobs", {"view": "profile", "player": "PlayerOne"})
    assert wait_job(srv, again["id"])["cached"] and srv.fake.calls == 1
    # Options are part of the key; refresh recomputes and drops the planner's contexts.
    call(srv, "POST", "/api/jobs", {"view": "guide", "player": "PlayerOne", "options": {"mood": "short"}})
    _, fresh, _ = call(srv, "POST", "/api/jobs", {"view": "profile", "player": "PlayerOne", "refresh": True})
    assert wait_job(srv, fresh["id"])["result"]["n"] > 1 and srv.fake.forgot == 1


def test_job_error_is_readable(srv):
    _, job, _ = call(srv, "POST", "/api/jobs", {"view": "profile", "player": "Boom"})
    j = wait_job(srv, job["id"])
    assert j["state"] == "error" and j["error"] == "no data for Boom" and "result" not in j


def test_cancelled_job_result_is_discarded(srv):
    srv.fake.gate.clear()
    _, job, _ = call(srv, "POST", "/api/jobs", {"view": "profile", "player": "PlayerOne"})
    wait_job(srv, job["id"], states=("running",))
    status, c, _ = call(srv, "POST", f"/api/jobs/{job['id']}/cancel", {})
    assert status == 200 and c["state"] == "cancelled"
    srv.fake.gate.set()
    time.sleep(0.3)
    j = call(srv, "GET", f"/api/jobs/{job['id']}")[1]
    assert j["state"] == "cancelled" and "result" not in j
    _, again, _ = call(srv, "POST", "/api/jobs", {"view": "profile", "player": "PlayerOne"})
    assert not wait_job(srv, again["id"])["cached"]  # nothing was cached by the cancelled run
    assert call(srv, "POST", "/api/jobs/nope/cancel", {})[0] == 404


def test_bad_requests_rejected(srv):
    for body in ({"view": "nope", "player": "PlayerOne"}, {"view": "profile", "player": "a/b"},
                 {"view": "profile", "player": ""}, {"view": "guide", "player": "PlayerOne", "options": {"mood": "x"}},
                 {"view": "guide", "player": "PlayerOne", "options": {"tracks": ["nope"]}},
                 {"view": "focus", "player": "PlayerOne", "options": {}},
                 {"view": "next", "player": "PlayerOne", "options": {"hours": -1}}):
        assert call(srv, "POST", "/api/jobs", body)[0] == 400, body


def test_meta_and_prefs_roundtrip(srv):
    status, meta, _ = call(srv, "GET", "/api/meta")
    assert "quest_points" in meta["tracks"] and "short" in meta["moods"] and meta["prefs"] == {}
    call(srv, "POST", "/api/prefs", {"last_player": "PlayerOne", "junk": 1})
    assert call(srv, "GET", "/api/meta")[1]["prefs"] == {"last_player": "PlayerOne"}


def test_static_served_and_traversal_refused(srv):
    for path, kind in (("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css")):
        status, body, r = call(srv, "GET", path)
        assert status == 200 and kind in r.getheader("Content-Type") and body
        assert "default-src 'self'" in r.getheader("Content-Security-Policy")
    for path in ("/../server.py", "/%2e%2e/server.py", "/static/../../server.py", "/..%2fserver.py",
                 "/%2e%2e%2fjobs.py", "/nope.js", "//etc/hostname", "/..%5cserver.py", "/%00app.js"):
        assert call(srv, "GET", path)[0] == 404, path


def test_frontend_never_uses_innerhtml_or_remote_urls():
    for f in STATIC.iterdir():
        text = f.read_text()
        assert "innerHTML" not in text and "outerHTML" not in text and "document.write" not in text
        assert "http://" not in text and "https://" not in text, f.name


def test_normalise_is_canonical():
    a = normalise("guide", {"player": " PlayerOne ", "options": {"tracks": ["gear", "quest_points"], "top": "5"}})
    b = normalise("guide", {"player": "PlayerOne", "options": {"tracks": ["quest_points", "gear"], "top": 5}})
    assert a == b and a["options"]["top"] == 5


def test_jobmanager_queued_cancel():
    started = threading.Event()
    release = threading.Event()

    def compute(view, params, progress):
        started.set()
        release.wait(5)
        return {"v": params["n"]}

    jm = JobManager(compute)
    first = jm.submit("v", {"n": 1})
    started.wait(5)
    second = jm.submit("v", {"n": 2})  # waits behind the first (one planner call at a time)
    jm.cancel(second.id)
    release.set()
    time.sleep(0.3)
    assert first.state == "done" and second.state == "cancelled" and second.result is None


# ---- view models against the real planner structures (offline fixtures) ----

@pytest.fixture
def ctx(player):  # noqa: F811
    heralds = wiki.parse_quest_row(load("wiki_quest_heralds.json")["bucket"][0])
    return Context(player=player, cost=CostModel.default(use_wiki=False),
                   quest_info=lambda t: heralds if t == "Heralds of Crimson" else None)


def test_profile_view_shape(ctx):
    v = json.loads(json.dumps(views.profile_view(ctx)))
    assert v["name"] == "PlayerOne" and v["quest_points"] == 473 and v["skills"]
    assert {"name", "level", "xp", "rank", "bonus_xp"} <= set(v["skills"][0])


def test_guide_view_shape(ctx):
    seen = []
    v = json.loads(json.dumps(views.guide_view(ctx, ["quest_points"], top=3, progress=lambda *a: seen.append(a))))
    (track,) = v["tracks"]
    assert track["track"] == "quest_points" and track["description"] and track["entries"]
    e = track["entries"][0]
    assert e["headline"] == "Heralds of Crimson"
    assert {"hours", "play_hours", "value", "why", "left", "unsure", "steps", "tags", "confidence", "prep"} <= set(e)
    assert seen and seen[-1][2] == "ranking"
    # The bar is in typical seconds (views.STEP_SECONDS): it only moves forward, and every step fits in the total.
    dones = [d for d, _, _ in seen]
    assert dones == sorted(dones) and all(0 <= d <= t for d, t, _ in seen)
    only_short = views.guide_view(ctx, ["quest_points"], mood="short")["tracks"][0]["entries"]
    assert all("short" in x["tags"] for x in only_short)


def test_next_view_shape(ctx):
    v = json.loads(json.dumps(views.next_view(ctx, 5.0)))
    assert v["hours"] == 5.0 and isinstance(v["picks"], list) and "prep" in v
    for p in v["picks"]:
        assert {"headline", "shares", "extra_hours", "tags"} <= set(p)


def test_planner_reuses_context_per_player_and_options(ctx):
    built = []
    planner = Planner(lambda name, flags, group: built.append((name, flags, group)) or ctx)
    p1 = normalise("profile", {"player": "PlayerOne"})
    planner.compute("profile", p1, lambda *a: None)
    planner.compute("profile", p1, lambda *a: None)
    assert len(built) == 1
    planner.compute("profile", normalise("profile", {"player": "PlayerOne", "options": {"f2p": True}}), lambda *a: None)
    assert len(built) == 2
    planner.forget()
    planner.compute("profile", p1, lambda *a: None)
    assert len(built) == 3


# ---- the web planner (rune_advisor.app.web, packaging/web) ----

def test_web_run_answers_in_json(ctx, monkeypatch):
    from rune_advisor.app import web

    def factory(name, flags, group):
        if name == "Boom":
            raise RuntimeError("no plugin export for Boom")
        return ctx

    monkeypatch.setattr(web, "_planner", Planner(factory))
    seen = []
    ok = json.loads(web.run("profile", json.dumps({"player": "PlayerOne"}), False, lambda *a: seen.append(a)))
    assert ok["result"]["name"] == ctx.player.name and seen and seen[-1][2] == "building profile"
    assert json.loads(web.run("profile", json.dumps({"player": "Boom"}), False, lambda *a: None)) == {
        "error": "no plugin export for Boom"}
    assert "player must be" in json.loads(web.run("profile", json.dumps({"player": "!"}), False, lambda *a: None))["error"]
    assert set(json.loads(web.meta())) == {"tracks", "moods"}


def test_web_player_comes_from_the_export_api_alone(tmp_path, monkeypatch):
    """The web planner's player: the API's snapshot (state by config id, expanded with the configs the API
    serves for it), no hiscores or Adventurer's Log, the page's token winning over a settings file's."""
    from rune_advisor import api_client
    from rune_advisor.app import web
    from test_core import _ApiHarness

    h = _ApiHarness(tmp_path)
    try:
        key = h.call("POST", "/v1/accounts", {})[1]["key"]
        plugin = h.call("POST", "/v1/tokens", {"scopes": ["submit"]}, auth=key)[1]["token"]
        read = h.call("POST", "/v1/tokens", {"label": "Web planner", "scopes": ["read"]}, auth=key)[1]["token"]
        assert h.call("POST", "/v1/state", h.posts[0], auth=plugin)[0] == 200
        settings_file = tmp_path / "rune_advisor.toml"
        settings_file.write_text('ironman = true\n[api]\nurl = "https://elsewhere.invalid"\ntoken = "desktop"\n')
        monkeypatch.setenv("RUNE_ADVISOR_CONFIG", str(settings_file))
        monkeypatch.setenv("RUNE_ADVISOR_API_URL", f"http://127.0.0.1:{h.port}/export_api")
        monkeypatch.setenv("RUNE_ADVISOR_API_TOKEN", read)
        monkeypatch.setattr(api_client, "CACHE_DIR", tmp_path / "api_cache")
        monkeypatch.setattr(api_client, "CONFIGS_DIR", tmp_path / "api_configs")
        monkeypatch.setattr(web, "QUEST_TITLES", tmp_path / "quest_titles.json")
        settings = web._settings([], "PlayerOne")
        assert settings.api_tokens == [read] and settings.plugin_db != tmp_path
        settings.plugin_db = tmp_path / "none.db"
        p = web.web_player("PlayerOne", settings)
        assert p.ironman and p.quests and all(q.source == "plugin" for q in p.quests.values())
        assert any("no quest catalogue" in n for n in p.notes)
        with pytest.raises(RuntimeError, match="no plugin export for Nobody"):
            web.web_player("Nobody", settings)
    finally:
        h.close()


def test_api_settings_come_from_the_environment_without_a_file(tmp_path, monkeypatch):
    from rune_advisor.config import load_settings

    monkeypatch.setenv("RUNE_ADVISOR_API_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("RUNE_ADVISOR_API_TOKEN", "t")
    s = load_settings(tmp_path / "absent.toml")
    assert s.api_url == "http://127.0.0.1:1" and s.api_tokens == ["t"]


def test_cached_client_can_keep_stale_answers(tmp_path, monkeypatch):
    import os

    from rune_advisor.http import CachedClient

    c = CachedClient(tmp_path)
    c._client = None  # any request would fail: the answers below come from the cache
    import hashlib
    key = hashlib.sha256(json.dumps(["https://x.invalid/a", None], sort_keys=True).encode()).hexdigest()
    path = c.cache_dir / f"{key}.json"
    path.write_text('{"a": 1}')
    os.utime(path, (1, 1))
    with pytest.raises(AttributeError):
        c.get_json("https://x.invalid/a", ttl=60)
    monkeypatch.setattr(CachedClient, "keep_stale", True)
    assert c.get_json("https://x.invalid/a", ttl=60) == {"a": 1}


def test_web_frontend_keeps_the_sites_rules():
    """No markup from strings, nothing loaded from another host, and the key never named: the page holds a
    read token in sessionStorage only."""
    from pathlib import Path

    web_dir = Path(__file__).parent.parent / "packaging" / "web"
    for f in web_dir.iterdir():
        text = f.read_text()
        assert "innerHTML" not in text and "outerHTML" not in text and "document.write" not in text, f.name
        urls = [u for u in __import__("re").findall(r"https?://[^\s\"'<>)]+", text)]
        assert all(u.startswith("https://api.runeadvisor.com") for u in urls), (f.name, urls)
    page = (web_dir / "planner.js").read_text()
    assert 'sessionStorage.getItem(TOKEN)' in page and "raa_" not in page
    account = (web_dir.parent.parent / "site" / "assets" / "account.js").read_text()
    assert 'sessionStorage.setItem(PLANNER_TOKEN, await K.plannerToken(key, web.id, web.epoch))' in account
    assert account.count("sessionStorage.setItem") == 1 and "localStorage.setItem" not in account
