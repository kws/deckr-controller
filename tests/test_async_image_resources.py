"""Async resource limits, HTTP freshness, cancellation and worker isolation."""

import io
import time

import anyio
import httpx
import pytest
from PIL import Image

from deckr.controller._render_content import ContentFailure
from deckr.controller._render_resources import ImageResources


def deadline():
    return time.monotonic() + 2


@pytest.mark.asyncio
async def test_parallel_acquisition_deduplicates_and_bounds_each_origin():
    active, peak, calls = 0, 0, []

    async def respond(request):
        nonlocal active, peak
        calls.append(str(request.url))
        active += 1
        peak = max(peak, active)
        await anyio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"bytes"
        )

    resources = ImageResources(transport=httpx.MockTransport(respond))
    urls = [f"https://art.test/{i}" for i in range(9)]
    result = await resources.acquire(
        [*urls, urls[0]], max_bytes=100, deadline=deadline()
    )
    assert set(result) == set(urls)
    assert len(calls) == 9
    assert peak == 4
    assert not resources._origins
    await resources.aclose()


@pytest.mark.asyncio
async def test_freshness_age_revalidation_and_no_cookies():
    now, requests = [0], []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={
                    "content-type": "Image/JPG; charset=binary",
                    "cache-control": "max-age=10",
                    "age": "8",
                    "etag": "v1",
                    "set-cookie": "secret=abc",
                },
                content=b"one",
            )
        assert request.headers["if-none-match"] == "v1"
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(304, headers={"cache-control": "max-age=10"})

    resources = ImageResources(
        transport=httpx.MockTransport(respond), clock=lambda: now[0]
    )

    async def get():
        return await resources.get(
            "https://art.test/a", max_bytes=10, deadline=deadline()
        )

    assert (await get()).content_type == "image/jpeg"
    now[0] = 1
    assert (await get()).data == b"one"
    assert len(requests) == 1
    now[0] = 3
    assert (await get()).data == b"one"
    assert len(requests) == 2
    now[0] = 12
    await get()
    assert len(requests) == 2
    await resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["", "no-store", "private", "no-cache", "max-age=0"])
async def test_responses_without_freshness_are_requested_again(policy):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "cache-control": policy},
            content=str(len(calls)).encode(),
        )

    resources = ImageResources(transport=httpx.MockTransport(respond))
    for i in (1, 2):
        blob = await resources.get(
            "https://art.test/a", max_bytes=10, deadline=deadline()
        )
        assert blob.data == str(i).encode()
    await resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["bytes", "deadline", "aggregate"])
async def test_stream_is_closed_when_a_budget_is_exceeded(monkeypatch, failure):
    import deckr.controller._render_resources as module

    received, closed = [], []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(100):
                received.append(i)
                if failure == "deadline":
                    await anyio.sleep(0.02)
                yield b"x" * 16384

        async def aclose(self):
            closed.append(True)

    resources = ImageResources(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, stream=Stream(), headers={"content-type": "image/png"}
            )
        )
    )
    if failure == "aggregate":
        monkeypatch.setattr(module, "MAX_RESOURCE_BYTES", 65536)
    with pytest.raises((ContentFailure, TimeoutError)):
        await resources.acquire(
            ["https://art.test/a"],
            max_bytes=20000 if failure == "bytes" else 2_000_000,
            deadline=time.monotonic() + (0.03 if failure == "deadline" else 2),
        )
    assert len(received) < 10
    assert closed == [True]
    await resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("controls", [1, 15, 32])
async def test_waiting_fetch_does_not_take_a_raster_worker(controls):
    from deckr.controller._raster import RasterFormat
    from deckr.controller._render import RenderModel, build_render_request
    from deckr.controller._render_workers import (
        ThreadRenderBackend,
        prepare_thread_rendering,
    )

    entered, release, finished = anyio.Event(), anyio.Event(), anyio.Event()
    data = io.BytesIO()
    Image.new("RGB", (72, 72), "red").save(data, format="PNG")

    async def respond(request):
        entered.set()
        await release.wait()
        return httpx.Response(
            200, content=data.getvalue(), headers={"content-type": "image/png"}
        )

    backend = ThreadRenderBackend()
    backend.resources = ImageResources(transport=httpx.MockTransport(respond))
    await prepare_thread_rendering()
    results = []

    async def slow(index):
        results.append(
            await backend.render(
                build_render_request(
                    RenderModel(image=f"https://art.test/{index}"), RasterFormat(72, 72)
                )
            )
        )
        finished.set()

    async with anyio.create_task_group() as group:
        for index in range(controls):
            group.start_soon(slow, index)
        await entered.wait()
        with anyio.fail_after(0.5):
            fast = await backend.render(
                build_render_request(RenderModel(title="Ready"), RasterFormat(72, 72))
            )
        assert fast.frame and not finished.is_set()
        release.set()
    assert len(results) == controls and all(result.frame for result in results)
    await backend.aclose()


@pytest.mark.asyncio
async def test_cache_is_bounded_and_expired_deadlines_cannot_use_a_hit(monkeypatch):
    import deckr.controller._render_resources as module

    monkeypatch.setattr(module, "CACHE_ENTRIES", 2)
    monkeypatch.setattr(module, "CACHE_BYTES", 5)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "cache-control": "max-age=3600"},
            content=b"xx",
        )

    resources = ImageResources(transport=httpx.MockTransport(respond))
    for name in ("a", "b", "c", "a"):
        await resources.get(
            f"https://art.test/{name}", max_bytes=10, deadline=deadline()
        )
    assert len(calls) == 4
    assert resources._bytes == 4 and len(resources._cache) == 2
    with pytest.raises(TimeoutError):
        await resources.get(
            "https://art.test/a", max_bytes=10, deadline=time.monotonic() - 1
        )
    await resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,status,headers",
    [
        ("https://user:password@art.test/a", 200, {"content-type": "image/png"}),
        ("file:///image.png", 200, {"content-type": "image/png"}),
        ("https://art.test/a", 302, {"location": "https://other.test/b"}),
        ("https://art.test/a", 304, {}),
        ("https://art.test/a", 200, {"content-type": "text/html"}),
    ],
)
async def test_resource_policy_rejects_credentials_redirects_and_invalid_replies(
    url, status, headers
):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers=headers, content=b"x")

    resources = ImageResources(transport=httpx.MockTransport(respond))
    with pytest.raises((ContentFailure, httpx.HTTPStatusError)):
        await resources.get(url, max_bytes=10, deadline=deadline())
    assert len(calls) <= 1
    await resources.aclose()


@pytest.mark.asyncio
async def test_close_cancels_active_and_queued_downloads():
    entered, cancelled = anyio.Event(), []

    async def respond(request):
        entered.set()
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.append(True)

    resources = ImageResources(transport=httpx.MockTransport(respond))

    async def fetch(index):
        try:
            await resources.get(
                f"https://art.test/{index}", max_bytes=10, deadline=deadline()
            )
        except RuntimeError as exc:
            assert "closed" in str(exc)

    async with anyio.create_task_group() as group:
        for index in range(10):
            group.start_soon(fetch, index)
        await entered.wait()
        await anyio.sleep(0)
        with anyio.fail_after(0.5):
            await resources.aclose()
    assert cancelled and not resources._scopes and not resources._origins
