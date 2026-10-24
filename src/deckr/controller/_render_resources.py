"""Bounded asynchronous artwork acquisition, independent of raster workers."""

import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlsplit

import anyio
import httpx
from invariant_gfx.artifacts import BlobArtifact

from deckr.controller._render_content import ContentFailure

logger = logging.getLogger(__name__)

MAX_RESOURCE_BYTES = 8 * 1024 * 1024
CACHE_BYTES = 16 * 1024 * 1024
CACHE_ENTRIES = 128
_FETCH_SLOTS = anyio.Semaphore(16)


def validate_url(url):
    if not isinstance(url, str):
        raise ContentFailure("invalid_content")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ContentFailure("invalid_content") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ContentFailure("invalid_content")
    return (
        parsed.scheme,
        parsed.hostname,
        port or (443 if parsed.scheme == "https" else 80),
    )


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


@dataclass
class _Entry:
    blob: BlobArtifact
    headers: dict[str, str]
    fresh_until: float


class ImageResources:
    def __init__(self, *, transport=None, clock=time.monotonic):
        self._transport, self._clock = transport, clock
        self._client = None
        self._origins = {}
        self._cache = OrderedDict()
        self._bytes = 0
        self._scopes = {}
        self._closed = False

    def _http(self):
        if self._closed:
            raise RuntimeError("Image resources are closed")
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
                cookies=CookieJar(policy=_NoCookies()),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
            )
        return self._client

    @asynccontextmanager
    async def _capacity(self, origin):
        entry = self._origins.setdefault(origin, [anyio.Semaphore(4), 0])
        entry[1] += 1
        try:
            async with entry[0], _FETCH_SLOTS:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._origins.pop(origin, None)

    def _remember(self, url, blob, headers):
        old = self._cache.pop(url, None)
        if old:
            self._bytes -= len(old.blob.data)
        directives = {
            part.strip().lower().split("=", 1)[0]: part.strip()
            .split("=", 1)[-1]
            .strip('"')
            for part in headers.get("cache-control", "").split(",")
        }
        if "no-store" in directives or "private" in directives or headers.get("vary"):
            return
        try:
            age = max(0, int(headers.get("age", "0")))
            ttl = max(0, min(60, int(directives.get("max-age", "0")) - age))
        except ValueError:
            ttl = 0
        if "no-cache" in directives:
            ttl = 0
        if not ttl and not (headers.get("etag") or headers.get("last-modified")):
            return
        size = len(blob.data)
        if size > CACHE_BYTES:
            return
        while self._cache and (
            self._bytes + size > CACHE_BYTES or len(self._cache) >= CACHE_ENTRIES
        ):
            _, removed = self._cache.popitem(last=False)
            self._bytes -= len(removed.blob.data)
        self._cache[url] = _Entry(blob, headers, self._clock() + ttl)
        self._bytes += size

    async def get(
        self,
        url,
        *,
        max_bytes,
        deadline,
        reserve=lambda size: None,
        media_types=("image/png", "image/jpeg"),
    ):
        started = time.monotonic()
        origin = validate_url(url)
        if self._closed:
            raise RuntimeError("Image resources are closed")
        if deadline <= time.monotonic():
            raise TimeoutError("Image acquisition deadline exceeded")
        done = anyio.Event()
        with anyio.CancelScope() as scope:
            self._scopes[scope] = done
            try:
                with anyio.fail_after(max(0, deadline - time.monotonic())):
                    entry = self._cache.get(url)
                    if entry is not None and entry.fresh_until > self._clock():
                        if len(entry.blob.data) > max_bytes:
                            raise ContentFailure("limit_exceeded")
                        if (
                            media_types is not None
                            and entry.blob.content_type not in media_types
                        ):
                            raise ContentFailure("invalid_content")
                        reserve(len(entry.blob.data))
                        self._cache.move_to_end(url)
                        logger.debug(
                            "Image resource host=%s cache=fresh bytes=%s total_ms=%.2f",
                            origin[1],
                            len(entry.blob.data),
                            (time.monotonic() - started) * 1000,
                        )
                        return entry.blob
                    async with self._capacity(origin):
                        acquired = time.monotonic()
                        headers = {"accept": "image/png, image/jpeg"}
                        if entry is not None:
                            if entry.headers.get("etag"):
                                headers["if-none-match"] = entry.headers["etag"]
                            elif entry.headers.get("last-modified"):
                                headers["if-modified-since"] = entry.headers[
                                    "last-modified"
                                ]
                        seconds = min(1.0, max(0, deadline - time.monotonic()))
                        with anyio.fail_after(seconds):
                            request = httpx.Request(
                                "GET",
                                url,
                                headers=headers,
                                extensions={
                                    "timeout": httpx.Timeout(seconds).as_dict()
                                },
                            )
                            response = await self._http().send(request, stream=True)
                            received = time.monotonic()
                            try:
                                metadata = dict(response.headers)
                                if response.status_code == 304:
                                    if entry is None or not any(
                                        k.startswith("if-") for k in headers
                                    ):
                                        raise ContentFailure("invalid_content")
                                    blob = entry.blob
                                    reserve(len(blob.data))
                                    metadata = {**entry.headers, **metadata}
                                    metadata["age"] = response.headers.get("age", "0")
                                else:
                                    response.raise_for_status()
                                    content_type = (
                                        metadata.get("content-type", "")
                                        .split(";")[0]
                                        .strip()
                                        .lower()
                                    )
                                    if content_type == "image/jpg":
                                        content_type = "image/jpeg"
                                    if (
                                        media_types is not None
                                        and content_type not in media_types
                                    ):
                                        raise ContentFailure("invalid_content")
                                    data = bytearray()
                                    async for chunk in response.aiter_bytes(
                                        chunk_size=16384
                                    ):
                                        if len(data) + len(chunk) > max_bytes:
                                            raise ContentFailure("limit_exceeded")
                                        reserve(len(chunk))
                                        data.extend(chunk)
                                    blob = BlobArtifact(bytes(data), content_type)
                                if len(blob.data) > max_bytes:
                                    raise ContentFailure("limit_exceeded")
                                if (
                                    media_types is not None
                                    and blob.content_type not in media_types
                                ):
                                    raise ContentFailure("invalid_content")
                                self._remember(url, blob, metadata)
                                logger.debug(
                                    "Image resource host=%s status=%s bytes=%s queue_ms=%.2f headers_ms=%.2f body_ms=%.2f total_ms=%.2f",
                                    origin[1],
                                    response.status_code,
                                    len(blob.data),
                                    (acquired - started) * 1000,
                                    (received - acquired) * 1000,
                                    (time.monotonic() - received) * 1000,
                                    (time.monotonic() - started) * 1000,
                                )
                                return blob
                            finally:
                                with anyio.move_on_after(0.5, shield=True):
                                    await response.aclose()
            finally:
                self._scopes.pop(scope, None)
                done.set()
        raise RuntimeError("Image resources closed during acquisition")

    async def acquire(self, urls, *, max_bytes, deadline):
        results, errors = {}, []
        total = 0

        def reserve(size):
            nonlocal total
            total += size
            if total > MAX_RESOURCE_BYTES:
                raise ContentFailure("limit_exceeded")

        async with anyio.create_task_group() as group:

            async def fetch(url):
                try:
                    blob = await self.get(
                        url, max_bytes=max_bytes, deadline=deadline, reserve=reserve
                    )
                    results[url] = blob
                except Exception as exc:
                    errors.append(exc)
                    group.cancel_scope.cancel()

            for url in dict.fromkeys(urls):
                group.start_soon(fetch, url)
        if errors:
            raise errors[0]
        return results

    async def aclose(self):
        self._closed = True
        scopes = tuple(self._scopes.items())
        for scope, _ in scopes:
            scope.cancel()
        for _, done in scopes:
            await done.wait()
        if self._client is not None:
            await self._client.aclose()
        self._cache.clear()
        self._bytes = 0
