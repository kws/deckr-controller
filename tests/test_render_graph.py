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


@pytest.mark.parametrize(
    "title", ["ABBA", "Adele", "AC/DC", "Chasing Pavements", "Make\nYou\nFeel My Love"]
)
def test_artist_labels_fit_without_splitting_words(monkeypatch, title):
    from PIL import ImageDraw

    from deckr.controller._bounded_text import packed_text

    rendered_lines = []
    original = ImageDraw.ImageDraw.text

    def draw_text(self, xy, text, *args, **kwargs):
        rendered_lines.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", draw_text)
    result = packed_text(
        text=title,
        size=[96, 96],
        font="Inter",
        min_font_size=4,
        limits=DEFAULT_LIMITS,
        max_dimension=1024,
    )
    assert result.image.size == (96, 96)
    assert " ".join(rendered_lines).split() == title.split()


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


@pytest.mark.parametrize(
    "content_type", ["image/jpeg", "image/jpg", "Image/JPG; charset=binary"]
)
def test_external_artwork_jpeg_media_types(monkeypatch, content_type):
    import time

    import httpx

    from deckr.controller._render_graph import _Registry

    stream = io.BytesIO()
    Image.new("RGB", (96, 96), "red").save(stream, format="JPEG")
    client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": content_type}, content=stream.getvalue()
        )
    )
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs)
    )
    registry = _Registry(DEFAULT_LIMITS, time.monotonic() + 2)
    image = registry.decode(registry.fetch("https://artwork.example/album.jpg"))
    assert image.image.size == (96, 96)
    assert image.image.getpixel((48, 48))[0] > 240


def test_detailed_640px_album_art_renders_and_download_limit_still_applies(monkeypatch):
    import random
    import time

    import httpx

    from deckr.controller._render_graph import _Registry

    out = io.BytesIO()
    Image.frombytes("RGB", (640, 640), random.Random(7).randbytes(640 * 640 * 3)).save(
        out, format="JPEG", quality=90
    )
    artwork = out.getvalue()
    assert 131072 < len(artwork) < DEFAULT_LIMITS.max_image_bytes
    client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=artwork if request.url.path == "/album" else b"x" * 1048577,
        )
    )
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs)
    )
    wire = {
        "format": "invariant-graph",
        "version": 1,
        "output": "output",
        "graph": {
            "blob": {
                "kind": "node",
                "op_name": "deckr:fetch_image_url",
                "params": {"url": "https://artwork.example/album"},
                "deps": [],
            },
            "output": {
                "kind": "node",
                "op_name": "gfx:blob_to_image",
                "params": {"blob": {"$ref": "blob"}},
                "deps": ["blob"],
            },
        },
    }
    result = render_graph(wire, 96, 96, DEFAULT_LIMITS)
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (96, 96)
    registry = _Registry(DEFAULT_LIMITS, time.monotonic() + 2)
    with pytest.raises(ContentFailure, match="limit_exceeded"):
        registry.fetch("https://artwork.example/oversized")


@pytest.mark.parametrize("dimensions", [(1000, 1333), (3000, 3000)])
def test_fetched_artist_portraits_are_downscaled_with_a_source_pixel_bound(
    monkeypatch, dimensions
):
    import time

    import httpx

    from deckr.controller._render_graph import _Registry

    out = io.BytesIO()
    Image.new("RGB", dimensions, "red").save(out, format="JPEG")
    client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=out.getvalue()
        )
    )
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=transport, **kw))
    registry = _Registry(DEFAULT_LIMITS, time.monotonic() + 2)
    blob = registry.fetch("https://artwork.example/artist")
    if dimensions == (3000, 3000):
        with pytest.raises(ContentFailure, match="limit_exceeded"):
            registry.decode(blob)
    else:
        image = registry.decode(blob).image
        assert image.size == (768, 1024)
        assert image.getpixel((384, 512))[0] > 240
