"""title_card recipe: centered title text on dark background."""

from invariant import Node, SubGraphNode
from invariant.params import ref
from invariant_gfx.anchors import relative


def _alignment_to_anchor(alignment: str | None) -> str:
    """Map title_alignment to relative() align string."""
    if alignment == "top":
        return "cs@cs"
    if alignment == "bottom":
        return "ce@ce"
    return "c@c"  # middle (default)


def title_card(
    title: str,
    font: str = "Inter",
    color: tuple[int, int, int, int] = (255, 255, 255, 255),
    font_size: int | str = 24,
    fit_width: str | None = None,
    needs_canvas: bool = False,
    title_alignment: str | None = None,
    weight: int | None = None,
    style: str = "normal",
) -> SubGraphNode:
    """Build a SubGraphNode that renders title text on a dark background.

    Canvas size is injected by the controller via context["canvas"] = {width, height}.
    Either font_size (int or CEL str) or fit_width (CEL str) is used; fit_width takes
    precedence when set.
    """
    align = _alignment_to_anchor(title_alignment)
    text_params: dict = {
        "text": title,
        "font": font,
        "color": color,
    }
    if fit_width:
        text_params["fit_width"] = fit_width
        text_deps = ["canvas"]
    else:
        text_params["size"] = font_size
        text_deps = ["canvas"] if needs_canvas else []
    if weight is not None:
        text_params["weight"] = weight
    if style != "normal":
        text_params["style"] = style

    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (0, 0, 0, 255),
            },
            deps=["canvas"],
        ),
        "text": Node(
            op_name="gfx:render_text",
            params=text_params,
            deps=text_deps,
        ),
        "output": Node(
            op_name="gfx:composite",
            params={
                "layers": [
                    {"image": ref("bg"), "id": "bg"},
                    {
                        "image": ref("text"),
                        "anchor": relative("bg", align),
                        "id": "text",
                    },
                ],
            },
            deps=["bg", "text"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="output"
    )
