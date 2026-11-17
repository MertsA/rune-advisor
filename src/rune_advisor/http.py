"""Small HTTP client with an on-disk TTL cache, to stay polite to Jagex and the wiki."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from .config import CACHE_DIR, USER_AGENT


class CachedClient:
    def __init__(self, cache_dir: Path = CACHE_DIR, ttl_seconds: float = 15 * 60):
        self.cache_dir = cache_dir / "http"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self._client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)

    def get_json(self, url: str, params: dict[str, Any] | None = None, ttl: float | None = None) -> Any:
        ttl = self.ttl if ttl is None else ttl
        key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / f"{key}.json"
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            return json.loads(path.read_text())
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        path.write_text(json.dumps(data))
        return data


def error_code(e: httpx.HTTPStatusError) -> str | None:
    """The `error` code of a Platform API failure body ({"error": "PLAYER_NOT_FOUND"}), if any."""
    try:
        body = e.response.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None
