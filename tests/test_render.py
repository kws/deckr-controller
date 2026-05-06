"""Tests for the controller raster render pipeline."""

from invariant import Node, dump_graph_data_uri
from invariant.params import ref

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
