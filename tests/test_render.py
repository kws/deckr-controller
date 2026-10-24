"""Tests for the controller raster render pipeline."""

import io
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import httpx
import pytest
from deckr.hardware.descriptors import CapabilityDescriptor
from PIL import Image

import deckr.controller.invariant.executor as executor_module
import deckr.controller.invariant.ops.fetch_url as fetch_url_module
from deckr.controller._device_layout import _raster_image_format
from deckr.controller._render import (
    build_render_request,
    render_request_to_frame,
    resolve,
)
from deckr.controller._state_store import ControlStateStore, RenderContent


def test_blank_title_resolves_to_blank_raster_overlay():
    store = ControlStateStore(context_id="dev.0,0")
    store.content = RenderContent(title="")

    model = resolve(store)

    assert model.overlay_type == "blank"


def test_png_only_descriptor_is_preserved_through_render_worker():
    capability = CapabilityDescriptor(
        capabilityId="raster",
        family="dev.deckr.output.raster",
        type="bitmap",
        direction="output",
        access=("settable",),
        commandTypes=("set_frame", "clear"),
        constraints=(
            {"type": "enum", "subject": "encoding", "values": ["png"]},
            {"type": "enum", "subject": "width", "values": [96]},
            {"type": "range", "subject": "height", "minimum": 80, "maximum": 100},
        ),
    )
    image_format = _raster_image_format(capability)
    assert image_format.format == "PNG"
    store = ControlStateStore(context_id="render-test")
    store.content = RenderContent(title="PNG")
    request = build_render_request(resolve(store), image_format)
    with Image.open(io.BytesIO(render_request_to_frame(request))) as image:
        assert image.format == "PNG"
        assert image.size == (96, 80)


def test_get_executor_builds_singleton_once_under_concurrent_calls(monkeypatch):
    monkeypatch.setattr(executor_module, "_EXECUTOR", None)
    sentinel = object()
    barrier = Barrier(8)
    call_lock = Lock()
    calls = 0

    def fake_build_executor():
        nonlocal calls
        with call_lock:
            calls += 1
        time.sleep(0.02)
        return sentinel

    def call_get_executor(_index: int):
        barrier.wait(timeout=1.0)
        return executor_module.get_executor()

    monkeypatch.setattr(executor_module, "build_executor", fake_build_executor)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call_get_executor, range(8)))

    assert results == [sentinel] * 8
    assert calls == 1


def test_fetch_image_url_http_uses_bounded_timeout(monkeypatch):
    class TimeoutClient:
        kwargs = None

        def __init__(self, **kwargs):
            type(self).kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def stream(self, method, url):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(fetch_url_module.httpx, "Client", TimeoutClient)

    with pytest.raises(httpx.ReadTimeout):
        fetch_url_module.fetch_image_url("https://example.test/image.png")

    assert TimeoutClient.kwargs == {
        "timeout": fetch_url_module.HTTP_IMAGE_TIMEOUT,
        "follow_redirects": True,
    }
