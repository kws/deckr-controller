"""Tests for the controller raster render pipeline."""

from deckr.controller._device_layout import RasterImageFormat
from deckr.controller._render import build_render_request, resolve
from deckr.controller._state_store import ControlStateStore, RenderContent


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
