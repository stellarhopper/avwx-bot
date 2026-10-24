"""One HTTP client for every weather API, with a shared TTL cache.

Every server the bot is in asks for much the same data — Front Range servers
all want KDEN, every brief wants the same SIGMETs — so responses are cached by
URL for a time that matches how often the source actually updates, and
concurrent requests for the same URL share one fetch. Nothing per-guild is
ever cached here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=20)


class SourceError(Exception):
    """A weather source failed. The message is safe to show to a user."""


def _key(url: str, params: dict | None) -> str:
    if not params:
        return url
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


class Http:
    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, int, str]] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    async def _fetch(self, url: str, params: dict | None, accept: str) -> tuple[int, str]:
        """The one place that touches the network. Tests replace this."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=TIMEOUT, headers={"User-Agent": self.user_agent}
            )
        async with self._session.get(url, params=params, headers={"Accept": accept}) as resp:
            return resp.status, await resp.text()

    async def get_text(
        self, url: str, params: dict | None = None, *, ttl: float, accept: str = "*/*"
    ) -> tuple[int, str]:
        key = _key(url, params)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1], hit[2]

        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            try:
                status, body = await self._fetch(url, params, accept)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("GET %s failed: %r", key, exc)
                raise SourceError(f"Couldn't reach {_host(url)} just now.") from exc
            if status >= 500 or status == 429:
                log.warning("GET %s → %s", key, status)
                raise SourceError(f"{_host(url)} is having trouble (HTTP {status}).")
            if status >= 400:
                log.warning("GET %s → %s: %s", key, status, body[:200])
                raise SourceError(f"{_host(url)} rejected the request (HTTP {status}).")
            self._cache[key] = (time.monotonic() + ttl, status, body)
            future.set_result((status, body))
            return status, body
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
                # Mark retrieved so a failure with no other waiters isn't
                # logged as "exception was never retrieved".
                future.exception()
            raise
        finally:
            self._inflight.pop(key, None)

    async def get_json(
        self, url: str, params: dict | None = None, *, ttl: float, accept: str = "application/json"
    ) -> Any:
        status, body = await self.get_text(url, params, ttl=ttl, accept=accept)
        if status == 204 or not body.strip():
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError(f"{_host(url)} sent something I couldn't read.") from exc

    def purge(self) -> None:
        """Drop expired entries. Called periodically so the cache can't grow forever."""
        cutoff = time.monotonic()
        for key in [k for k, v in self._cache.items() if v[0] <= cutoff]:
            del self._cache[key]

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else url
