"""The local HTTP server: JSON API plus the static frontend, loopback only, every request authenticated.

Defences against other local software and web pages (this serves the player's data and runs planner code):
  * bound to 127.0.0.1 on a random port;
  * a per-launch secret on every request, as the `X-RA-Secret` header or the `ra_secret` cookie. The shell
    opens `/?s=<secret>` once; the server answers by setting the cookie (HttpOnly, SameSite=Strict) and
    redirecting to `/`, so the secret does not stay in the window's URL or history;
  * the Host header must be the server's own address (stops DNS rebinding) and an Origin header, when sent,
    must be the server's own origin (stops cross-site posts);
  * POST bodies must be JSON, which a cross-site form cannot send without a preflight.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..config import STATE_DIR
from ..recommend.guide import MOODS
from ..recommend.tracks import ALL_TRACKS
from .jobs import JobManager
from .planner import Planner, normalise

STATIC = Path(__file__).parent / "static"
COOKIE = "ra_secret"
MAX_BODY = 64 * 1024
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
}


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, planner: Planner | None = None, secret: str | None = None, prefs_path: Path | None = None,
                 port: int = 0):
        self.secret = secret or secrets.token_urlsafe(32)
        self.planner = planner or Planner()
        self.jobs = JobManager(self.planner.compute)
        self.prefs_path = prefs_path or STATE_DIR / "ui.json"
        self._prefs_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def start_url(self) -> str:
        """The URL the shell opens: it carries the secret once, exchanged for a cookie."""
        return f"{self.origin}/?s={self.secret}"

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def prefs(self) -> dict:
        with self._prefs_lock:
            try:
                data = json.loads(self.prefs_path.read_text())
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

    def save_prefs(self, update: dict) -> dict:
        with self._prefs_lock:
            try:
                data = json.loads(self.prefs_path.read_text())
            except (OSError, ValueError):
                data = {}
            data.update({k: v for k, v in update.items() if k in ("last_player", "options")})
            try:
                self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
                self.prefs_path.write_text(json.dumps(data))
            except OSError:
                pass  # remembering the last player is a convenience
            return data


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet: the planner's own output matters more
        pass

    # -- plumbing --

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _cookie_secret(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _guard(self) -> bool:
        """Host, Origin and secret checks; answers the request itself when one fails."""
        if self.headers.get("Host", "") not in self.server.allowed_hosts():
            self._error(HTTPStatus.FORBIDDEN, "bad Host header")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (self.server.origin, f"http://localhost:{self.server.port}"):
            self._error(HTTPStatus.FORBIDDEN, "foreign Origin")
            return False
        given = self.headers.get("X-RA-Secret") or self._cookie_secret()
        if not hmac.compare_digest(given.encode(), self.server.secret.encode()):
            self._error(HTTPStatus.FORBIDDEN, "missing or wrong secret")
            return False
        return True

    def _body(self) -> dict | None:
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send application/json")
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return None
        if not isinstance(data, dict):
            self._error(HTTPStatus.BAD_REQUEST, "expected a JSON object")
            return None
        return data

    # -- routes --

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/" and "s" in parse_qs(url.query):
            return self._exchange(parse_qs(url.query)["s"][0])
        if not self._guard():
            return
        if url.path.startswith("/api/"):
            return self._api_get(url.path)
        self._static(url.path)

    def do_POST(self) -> None:
        if not self._guard():
            return
        path = urlsplit(self.path).path
        body = self._body()
        if body is None:
            return
        if path == "/api/jobs":
            view = str(body.get("view") or "")
            try:
                params = normalise(view, body)
            except ValueError as e:
                return self._error(HTTPStatus.BAD_REQUEST, str(e))
            refresh = bool(body.get("refresh"))
            if refresh:
                self.server.planner.forget()
            job = self.server.jobs.submit(view, params, refresh)
            return self._json(HTTPStatus.ACCEPTED, job.snapshot(with_result=False))
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job = self.server.jobs.cancel(path.split("/")[3])
            return self._json(200, job.snapshot(with_result=False)) if job else self._error(404, "no such job")
        if path == "/api/cache/clear":
            self.server.jobs.clear_cache()
            self.server.planner.forget()
            return self._json(200, {"ok": True})
        if path == "/api/prefs":
            return self._json(200, self.server.save_prefs(body))
        self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _exchange(self, given: str) -> None:
        """First load: swap the secret in the URL for a cookie and drop it from the address."""
        if self.headers.get("Host", "") not in self.server.allowed_hosts() \
                or not hmac.compare_digest(given.encode(), self.server.secret.encode()):
            return self._error(HTTPStatus.FORBIDDEN, "missing or wrong secret")
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{COOKIE}={self.server.secret}; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("Content-Length", "0")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def _api_get(self, path: str) -> None:
        if path == "/api/meta":
            return self._json(200, {"tracks": {n: t.description for n, t in ALL_TRACKS.items()},
                                    "moods": list(MOODS), "prefs": self.server.prefs()})
        if path.startswith("/api/jobs/"):
            job = self.server.jobs.get(path.split("/")[3])
            return self._json(200, job.snapshot()) if job else self._error(404, "no such job")
        self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _static(self, path: str) -> None:
        """A file from static/, never from outside it."""
        rel = unquote(path).lstrip("/") or "index.html"
        if "\\" in rel or "\0" in rel or any(part in ("..", "") for part in rel.split("/")):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        target = (STATIC / rel).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
