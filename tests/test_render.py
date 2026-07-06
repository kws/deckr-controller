"""Tests for the controller raster render pipeline."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import httpx
import pytest
from invariant import Node, dump_graph_data_uri
from invariant.params import ref

import deckr.controller.invariant.executor as executor_module
import deckr.controller.invariant.ops.fetch_url as fetch_url_module
from deckr.controller._device_layout import RasterImageFormat
from deckr.controller._render import (
    RenderModel,
    _wire_to_node,
    build_render_request,
    resolve,
)
from deckr.controller._state_store import ControlStateStore, RenderContent
from deckr.controller.invariant.executor import get_executor


def test_resolve_title_content_as_raster_model():
    store = ControlStateStore(context_id="dev.0,0")
    store.content = RenderContent(title="Hello")

    model = resolve(store)

    assert model.title == "Hello"
    assert model.image is None


def test_resolve_image_takes_precedence_over_title():
    store = ControlStateStore(context_id="dev.0,0")
    store.content = RenderContent(
        title="Hello",
        image="https://example.invalid/image.png",
    )

    model = resolve(store)

    assert model.image == "https://example.invalid/image.png"
    assert model.title is None


def test_blank_title_resolves_to_blank_raster_overlay():
    store = ControlStateStore(context_id="dev.0,0")
    store.content = RenderContent(title="")

    model = resolve(store)

    assert model.overlay_type == "blank"


def test_title_content_builds_raster_render_request():
    store = ControlStateStore(context_id="dev.0,0", binding_id="binding-1")
    store.content = RenderContent(title="Hello")
    model = resolve(store)

    request = build_render_request(
        model,
        RasterImageFormat(width=72, height=72),
        context_id=store.context_id,
        binding_id=store.binding_id,
        control_id="0,0",
        generation=3,
    )

    assert request is not None
    assert request.context_id == "dev.0,0"
    assert request.binding_id == "binding-1"
    assert request.control_id == "0,0"
    assert request.generation == 3
    assert request.graph


def test_graph_uri_query_context_is_literal_when_bound_to_render_graph():
    graph = {
        "value": Node(
            op_name="stdlib:identity",
            params={"value": ref("text")},
            deps=["text"],
        )
    }
    uri = dump_graph_data_uri(
        graph,
        output="value",
        context={"text": "${canvas.width}"},
    )

    request = build_render_request(
        RenderModel(image=uri),
        RasterImageFormat(width=72, height=72),
    )
    assert request is not None

    render_node = _wire_to_node(request.graph, request.context)
    context = dict(render_node.context)
    context["canvas"] = {"width": 72, "height": 72}
    result = get_executor().execute({"src": render_node.node}, ["src"], context=context)

    assert result["src"] == "${canvas.width}"


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

        def get(self, url):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(fetch_url_module.httpx, "Client", TimeoutClient)

    with pytest.raises(httpx.ReadTimeout):
        fetch_url_module.fetch_image_url("https://example.test/image.png")

    assert TimeoutClient.kwargs == {
        "timeout": fetch_url_module.HTTP_IMAGE_TIMEOUT,
        "follow_redirects": True,
    }
