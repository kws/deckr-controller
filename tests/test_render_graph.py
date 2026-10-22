"""The Invariant extension uses its negotiated, bounded execution contract."""

import io

import pytest
from invariant import dump_graph_to_dict
from PIL import Image

from deckr.controller._bindings._render_policy import DEFAULT_LIMITS
from deckr.controller._render_content import ContentFailure
from deckr.controller._render_graph import (
    INVARIANT_FORMAT,
    render_graph,
    validate_graph,
)
from deckr.controller.invariant.recipes import icon_button, title_card
from test_support.action_runtime import running_controller
from test_support.rendering import parameters, real_backend, result_for


def document():
    graph = title_card("Clock")
    return dump_graph_to_dict(graph.graph, output=graph.output)


@pytest.mark.parametrize(
    "graph", [title_card("Clock"), icon_button(icon="lucide:play", title="Sonos")]
)
def test_provider_recipes_render_to_logical_canvas(graph):
    data = render_graph(
        dump_graph_to_dict(graph.graph, output=graph.output), 72, 72, DEFAULT_LIMITS
    )
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (72, 72)
        assert image.format == "PNG"
        assert image.getbbox() is not None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["graph"]["bg"].update(op_name="stdlib:identity"),
        lambda d: d["graph"]["bg"]["params"].update(size=[100000, 100000]),
        lambda d: d["graph"]["bg"]["params"].update(size=["${2 ** 32}", 72]),
        lambda d: d["graph"]["bg"].update(deps=["output"]),
        lambda d: d["graph"]["text"]["params"].update(text="x" * 4096),
    ],
)
def test_invalid_graph_rejected_before_execution(mutation):
    wire = document()
    mutation(wire)
    with pytest.raises(ContentFailure):
        validate_graph(wire, DEFAULT_LIMITS)


def test_computed_size_is_bounded_before_allocation():
    wire = document()
    wire["graph"]["bg"]["params"]["size"] = ["${4096 * 4096}", 72]
    with pytest.raises(ContentFailure, match="limit_exceeded"):
        render_graph(wire, 72, 72, DEFAULT_LIMITS)


@pytest.mark.asyncio
async def test_extension_crosses_managed_render_boundary(managed_substrate):
    async with running_controller(managed_substrate, with_output=True) as h:
        real_backend(h)
        params = parameters(h)
        params["content"] = {"format": INVARIANT_FORMAT, "data": document()}
        request = await h.channel.send("binding_render", params)
        result = await result_for(h, request)
        assert result["status"] == "dispatched", result


@pytest.mark.parametrize("packed", [False, True])
def test_text_bounds_are_checked_before_large_image_allocation(monkeypatch, packed):
    original = Image.new

    def allocate(mode, size, *args, **kwargs):
        assert max(size) <= 1024, "Large text image allocated before checking limits"
        return original(mode, size, *args, **kwargs)

    monkeypatch.setattr(Image, "new", allocate)
    wire = document()
    node = wire["graph"]["text"]
    node["params"] = {
        "text": "W" * 1024,
        "font": "Inter",
        "color": {"$tuple": [255, 255, 255, 255]},
        **(
            {"size": [72, 72], "min_font_size": 128, "max_font_size": 128}
            if packed
            else {"size": 128}
        ),
    }
    if packed:
        node["op_name"] = "gfx:packed_text"
    if packed:
        # Packed text truncates this input to its canvas before allocation.
        assert render_graph(wire, 72, 72, DEFAULT_LIMITS)
    else:
        with pytest.raises(ContentFailure, match="limit_exceeded"):
            render_graph(wire, 72, 72, DEFAULT_LIMITS)


def test_packed_text_recipe_renders_with_bounded_layout():
    wire = document()
    node = wire["graph"]["text"]
    node["op_name"] = "gfx:packed_text"
    node["params"] = {"text": "A favourite album", "font": "Inter", "size": [72, 72]}
    assert render_graph(wire, 72, 72, DEFAULT_LIMITS)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"invalid": "document"}, None])
async def test_invalid_graph_does_not_supersede_admitted_text(managed_substrate, data):
    from test_support.managed_hardware import eventually
    from test_support.rendering import GatedBackend

    if data is None:
        data = document()
        data["graph"]["bg"]["params"]["size"] = ["${1 +}", 72]
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        old = await h.channel.send("binding_render", parameters(h, text="valid"))
        await eventually(lambda: len(backend.started) == 1)
        bad = parameters(h)
        bad["content"] = {"format": INVARIANT_FORMAT, "data": data}
        request = await h.channel.send("binding_render", bad)
        result = await result_for(h, request)
        assert (result["status"], result["code"]) == ("rejected", "invalid_content")
        backend.gates[0].set()
        assert (await result_for(h, old))["status"] == "dispatched"
