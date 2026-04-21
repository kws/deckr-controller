"""image_card recipe: image from URL/data URI resized to canvas."""

from invariant import Node, SubGraphNode
from invariant.params import ref


def image_card(url: str) -> SubGraphNode:
    """Build a SubGraphNode that fetches an image from url and resizes to canvas.

    Canvas size is injected by the controller via context["canvas"] = {width, height}.
    """
    inner = {
        "blob": Node(
            op_name="deckr:fetch_image_url",
            params={"url": url},
            deps=[],
        ),
        "img": Node(
            op_name="gfx:blob_to_image",
            params={"blob": ref("blob")},
            deps=["blob"],
        ),
        "output": Node(
            op_name="gfx:resize",
            params={
                "image": ref("img"),
                "width": "${canvas.width}",
                "height": "${canvas.height}",
            },
            deps=["img", "canvas"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="output"
    )
