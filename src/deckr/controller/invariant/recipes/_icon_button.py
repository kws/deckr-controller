"""icon_button recipe: icon plus label on a dark background."""

from invariant import Node, SubGraphNode
from invariant.params import ref
from invariant_gfx.anchors import relative


def icon_button(
    *,
    icon: str = "lucide:message-circle-question-mark",
    title: str = "Title",
    icon_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    title_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    title_size: int = 15,
    title_font: str = "Inter",
) -> SubGraphNode:
    """Build a SubGraphNode that renders an icon button."""
    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (0, 0, 0, 255),
            },
            deps=["canvas"],
        ),
        "icon_blob": Node(
            op_name="gfx:resolve_resource",
            params={"name": icon},
            deps=[],
        ),
        "icon_raster": Node(
            op_name="gfx:render_svg",
            params={
                "svg_content": ref("icon_blob"),
                "width": "${decimal(canvas.width) * decimal('0.7')}",
                "height": "${decimal(canvas.height) * decimal('0.7')}",
            },
            deps=["icon_blob", "canvas"],
        ),
        "icon": Node(
            op_name="gfx:colorize",
            params={"image": ref("icon_raster"), "color": icon_color},
            deps=["icon_raster"],
        ),
        "label": Node(
            op_name="gfx:render_text",
            params={
                "text": title,
                "font": title_font,
                # Scale the requested title size with the key width while keeping the
                # result positive on wider-than-72px devices.
                "size": f'${{decimal("{title_size}") * canvas.width / 72}}',
                "color": title_color,
            },
            deps=["canvas"],
        ),
        "output": Node(
            op_name="gfx:composite",
            params={
                "layers": [
                    {"image": ref("bg"), "id": "bg"},
                    {
                        "image": ref("icon"),
                        "anchor": relative("bg", "cs@cs"),
                        "id": "icon",
                    },
                    {
                        "image": ref("label"),
                        "anchor": relative("bg", "ce@ce"),
                        "id": "label",
                    },
                ],
            },
            deps=["bg", "icon", "label"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="output"
    )
