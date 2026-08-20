"""Shared HTTP client: honest UA, ETag caching, per-host rate limit, retries (SPEC §6.4)."""

from __future__ import annotations

import json
import random
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import db

DEFAULT_TIMEOUT = 30.0
LARGE_BOARD_TIMEOUT = 60.0  # spacex, andurilindustries, rocketlab, openai (SPEC §6.4)
_MIN_INTERVAL = 1.0
_RETRIES = 3


class NotModified(Exception):
    """Server answered 304 — snapshot unchanged, skip the diff (SPEC §6.3)."""


class Http:
    def __init__(self, conn: sqlite3.Connection | None, owner_email: str):
        self._conn = conn
        self._last_request: dict[str, float] = {}
        ua = f"internship-radar/0.1 (personal aggregator; contact {owner_email or 'unset'})"
        self._client = httpx.Client(
            headers={"User-Agent": ua}, timeout=DEFAULT_TIMEOUT, follow_redirects=True
        )

    def close(self) -> None:
        self._client.close()

    def _throttle(self, url: str, min_interval: float = _MIN_INTERVAL) -> None:
        host = urlparse(url).netloc
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request[host] = time.monotonic()

    def _conditional_headers(self, url: str) -> dict[str, str]:
        if self._conn is None:
            return {}
        headers = {}
        etag = db.kv_get(self._conn, f"etag:{url}")
        if etag:
            headers["If-None-Match"] = etag
        lastmod = db.kv_get(self._conn, f"lastmod:{url}")
        if lastmod:
            headers["If-Modified-Since"] = lastmod
        return headers

    def _store_validators(self, url: str, resp: httpx.Response) -> None:
        if self._conn is None:
            return
        if resp.headers.get("etag"):
            db.kv_set(self._conn, f"etag:{url}", resp.headers["etag"])
        if resp.headers.get("last-modified"):
            db.kv_set(self._conn, f"lastmod:{url}", resp.headers["last-modified"])

    def get(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval: float = _MIN_INTERVAL,
        conditional: bool = True,
    ) -> httpx.Response:
        """GET with throttle + conditional caching + backoff. Raises NotModified on 304.

        Retries ×3 with exponential backoff + jitter on 429/5xx and transport
        errors; never retries other 4xx (SPEC §6.4).
        """
        headers = self._conditional_headers(url) if conditional else {}
        last_exc: Exception | None = None
        for attempt in range(_RETRIES):
            self._throttle(url, min_interval)
            try:
                resp = self._client.get(url, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep((2**attempt) + random.random())
                continue
            if resp.status_code == 304:
                raise NotModified(url)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} from {url}", request=resp.request, response=resp
                )
                time.sleep((2**attempt) + random.random())
                continue
            resp.raise_for_status()
            self._store_validators(url, resp)
            return resp
        raise last_exc  # type: ignore[misc]

    def get_json(self, url: str, **kwargs):
        return self.get(url, **kwargs).json()


class FixtureHttp:
    """Zero-network stand-in for dry-run and tests: url -> fixture file path."""

    def __init__(self, mapping: dict[str, Path]):
        self._mapping = mapping

    def close(self) -> None:
        pass

    def get_json(self, url: str, **_kwargs):
        path = self._mapping.get(url)
        if path is None:
            raise KeyError(f"no fixture registered for {url}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)
