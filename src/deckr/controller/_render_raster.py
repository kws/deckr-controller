"""Composition of an already validated logical PNG without a wire round trip."""

import io

from invariant import Node, ref
from invariant_gfx.artifacts import ImageArtifact
from PIL import Image

from deckr.controller.invariant.executor import get_executor
from deckr.controller.invariant.ops.encode_jpeg import encode_raster
from deckr.controller.invariant.recipes import feedback_overlay


def compose_raster(request):
    fmt = request.image_format
    width, height = (
        (fmt.height, fmt.width) if fmt.rotation % 180 else (fmt.width, fmt.height)
    )
    with Image.open(io.BytesIO(request.raster)) as source:
        if source.size != (width, height):
            raise ValueError("Logical raster dimensions do not match the target")
        image = ImageArtifact(source.convert("RGBA"))
    if request.overlay_type:
        graph = feedback_overlay(
            request.overlay_type,
            title=request.overlay_title,
            base=Node(
                "stdlib:identity",
                {"value": ref("accepted_base")},
                ["accepted_base"],
                cache=False,
            ),
        ).graph
        image = get_executor().execute(
            graph,
            ["output"],
            context={
                "accepted_base": image,
                "canvas": {"width": width, "height": height},
            },
        )["output"]
    if fmt.rotation:
        image = ImageArtifact(
            image.image.rotate(
                fmt.rotation, expand=True, resample=Image.Resampling.BICUBIC
            )
        )
    return encode_raster(image, quality=50, format=fmt.format).data
