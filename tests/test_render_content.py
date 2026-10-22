"""Rendering contracts exercised through the production managed service boundary."""

import base64
import io

import pytest
from deckr.actions.rendering import RenderContent, RenderImage
from PIL import Image

from deckr.controller._bindings._render_policy import DEFAULT_LIMITS
from deckr.controller._render_content import (
    ContentFailure,
    decode_image,
    render_content,
)
from test_support.rendering import (
    png,
)


@pytest.mark.parametrize("fit", ["contain", "cover", "stretch"])
def test_inline_geometry_and_alpha(fit):
    content = RenderContent(
        format="dev.deckr.render.image.v1",
        data={
            "image": png(color=(255, 0, 0, 128)),
            "encoding": "png",
            "fit": fit,
            "background": "#0000FF",
        },
    )
    result = render_content(content, 20, 20, DEFAULT_LIMITS)
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (20, 20)
        assert image.getpixel((10, 10)) == (128, 0, 127)
        assert image.getpixel((10, 0)) == (
            (0, 0, 255) if fit == "contain" else (128, 0, 127)
        )


def test_image_limits_before_decode_and_encoding_check():
    data = RenderImage(image=png(), encoding="png")
    with pytest.raises(ContentFailure, match="limit_exceeded"):
        decode_image(data, DEFAULT_LIMITS.model_copy(update={"max_image_pixels": 1}))
    with pytest.raises(ContentFailure, match="invalid_content"):
        decode_image(data.model_copy(update={"encoding": "jpeg"}), DEFAULT_LIMITS)


def test_non_square_orientation_and_static_image_validation():
    from deckr.controller._raster import RasterFormat
    from deckr.controller._render import (
        RenderModel,
        build_render_request,
        render_request_to_frame,
    )

    request = build_render_request(
        RenderModel(image="data:image/png;base64," + png((10, 20))),
        RasterFormat(width=20, height=10, rotation=90, format="PNG"),
    )
    with Image.open(io.BytesIO(render_request_to_frame(request))) as image:
        assert image.size == (20, 10)
    for raw in (base64.b64decode(png())[:-20], b"not png"):
        with pytest.raises(ContentFailure, match="invalid_content"):
            decode_image(
                RenderImage(image=base64.b64encode(raw).decode(), encoding="png"),
                DEFAULT_LIMITS,
            )
    out = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        out,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ContentFailure, match="invalid_content"):
        decode_image(
            RenderImage(
                image=base64.b64encode(out.getvalue()).decode(), encoding="png"
            ),
            DEFAULT_LIMITS,
        )
