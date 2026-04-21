"""Overlay recipes for show_alert / show_ok (temporary key feedback)."""

from invariant import Node, SubGraphNode
from invariant.params import ref
from invariant_gfx.anchors import relative

# RGBA 0-255 for gfx:create_solid
BLACK = (0, 0, 0, 255)

# RGBA 0-255 for gfx:colorize
COLOR_AMBER = (245, 158, 11, 255)
COLOR_GREEN = (34, 197, 94, 255)
COLOR_SLATE = (148, 163, 184, 255)


def alert_overlay() -> SubGraphNode:
    """SubGraphNode: amber triangle-alert icon centered on dark background."""
    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (40, 40, 40, 255),
            },
            deps=["canvas"],
        ),
        "icon_blob": Node(
            op_name="gfx:resolve_resource",
            params={"name": "lucide:triangle-alert"},
            deps=[],
        ),
        "icon_raster": Node(
            op_name="gfx:render_svg",
            params={
                "svg_content": ref("icon_blob"),
                "width": 48,
                "height": 48,
            },
            deps=["icon_blob"],
        ),
        "icon": Node(
            op_name="gfx:colorize",
            params={"image": ref("icon_raster"), "color": COLOR_AMBER},
            deps=["icon_raster"],
        ),
        "output": Node(
            op_name="gfx:composite",
            params={
                "layers": [
                    {"image": ref("bg"), "id": "bg"},
                    {
                        "image": ref("icon"),
                        "anchor": relative("bg", "c@c"),
                        "id": "icon",
                    },
                ],
            },
            deps=["bg", "icon"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="output"
    )


def unavailable_overlay() -> SubGraphNode:
    """SubGraphNode: 'Not available' icon on dark background for missing actions."""
    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (40, 40, 40, 255),
            },
            deps=["canvas"],
        ),
        "icon_blob": Node(
            op_name="gfx:resolve_resource",
            params={"name": "lucide:circle-alert"},
            deps=[],
        ),
        "icon_raster": Node(
            op_name="gfx:render_svg",
            params={
                "svg_content": ref("icon_blob"),
                "width": 32,
                "height": 32,
            },
            deps=["icon_blob"],
        ),
        "icon": Node(
            op_name="gfx:colorize",
            params={"image": ref("icon_raster"), "color": COLOR_SLATE},
            deps=["icon_raster"],
        ),
        "output": Node(
            op_name="gfx:composite",
            params={
                "layers": [
                    {"image": ref("bg"), "id": "bg"},
                    {
                        "image": ref("icon"),
                        "anchor": relative("bg", "c@c"),
                        "id": "icon",
                    },
                ],
            },
            deps=["bg", "icon"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="output"
    )


def ok_overlay() -> SubGraphNode:
    """SubGraphNode: green check icon centered on dark background."""
    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (40, 40, 40, 255),
            },
            deps=["canvas"],
        ),
        "icon_blob": Node(
            op_name="gfx:resolve_resource",
            params={"name": "lucide:check"},
            deps=[],
        ),
        "icon_raster": Node(
            op_name="gfx:render_svg",
            params={
                "svg_content": ref("icon_blob"),
                "width": 48,
                "height": 48,
            },
            deps=["icon_blob"],
        ),
        "icon": Node(
            op_name="gfx:colorize",
            params={"image": ref("icon_raster"), "color": COLOR_GREEN},
            deps=["icon_raster"],
        ),
        "output": Node(
            op_name="gfx:composite",
            params={
                "layers": [
                    {"image": ref("bg"), "id": "bg"},
                    {
                        "image": ref("icon"),
                        "anchor": relative("bg", "c@c"),
                        "id": "icon",
                    },
                ],
            },
            deps=["bg", "icon"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="output"
    )


def solid_card(color: tuple[int, int, int, int] = BLACK) -> SubGraphNode:
    """SubGraphNode: solid color fill (canvas size). Use for blank/empty slots."""
    inner = {
        "output": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": color,
            },
            deps=["canvas"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="output"
    )
