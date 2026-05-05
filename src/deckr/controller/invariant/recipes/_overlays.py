"""Overlay recipes for controller-owned status render states."""

from invariant import Node, SubGraphNode
from invariant.params import ref
from invariant_gfx.anchors import relative

# RGBA 0-255 for gfx:create_solid
BLACK = (0, 0, 0, 255)

# RGBA 0-255 for gfx:colorize
COLOR_AMBER = (245, 158, 11, 255)
COLOR_RED = (239, 68, 68, 255)
COLOR_GREEN = (34, 197, 94, 255)
COLOR_SLATE = (148, 163, 184, 255)
COLOR_BLUE = (96, 165, 250, 255)

_OVERLAY_ICON: dict[str, str] = {
    "ok": "lucide:check",
    "error": "lucide:triangle-alert",
    "unavailable": "lucide:circle-alert",
    "loading": "lucide:ellipsis",
    "unknown": "lucide:message-circle-question-mark",
}

_OVERLAY_COLOR: dict[str, tuple[int, int, int, int]] = {
    "ok": COLOR_GREEN,
    "error": COLOR_RED,
    "unavailable": COLOR_SLATE,
    "loading": COLOR_BLUE,
    "unknown": COLOR_AMBER,
}

_OVERLAY_TITLE: dict[str, str] = {
    "ok": "OK",
    "error": "Error",
    "unavailable": "Unavailable",
    "loading": "Loading",
    "unknown": "Unknown",
}


def feedback_overlay(
    template: str,
    *,
    title: str | None = None,
    base: Node | SubGraphNode | None = None,
) -> SubGraphNode:
    """SubGraphNode: semantic transient feedback over an optional base image."""

    icon = _OVERLAY_ICON.get(template, _OVERLAY_ICON["unknown"])
    color = _OVERLAY_COLOR.get(template, _OVERLAY_COLOR["unknown"])
    label = title or _OVERLAY_TITLE.get(template, _OVERLAY_TITLE["unknown"])
    graph: dict[str, Node | SubGraphNode] = {}
    layers: list[dict] = []
    deps: list[str] = []

    if base is None:
        graph["bg"] = Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (40, 40, 40, 255),
            },
            deps=["canvas"],
        )
        layers.append({"image": ref("bg"), "id": "bg"})
        deps.append("bg")
    else:
        graph["base"] = base
        graph["shade"] = Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (0, 0, 0, 180),
            },
            deps=["canvas"],
        )
        layers.append({"image": ref("base"), "id": "base"})
        layers.append(
            {
                "image": ref("shade"),
                "anchor": relative("base", "c@c"),
                "id": "shade",
            }
        )
        deps.extend(["base", "shade"])

    graph["icon_blob"] = Node(
        op_name="gfx:resolve_resource",
        params={"name": icon},
        deps=[],
    )
    graph["icon_raster"] = Node(
        op_name="gfx:render_svg",
        params={
            "svg_content": ref("icon_blob"),
            "width": "${decimal(canvas.width) * decimal('0.62')}",
            "height": "${decimal(canvas.height) * decimal('0.62')}",
        },
        deps=["icon_blob", "canvas"],
    )
    graph["icon"] = Node(
        op_name="gfx:colorize",
        params={"image": ref("icon_raster"), "color": color},
        deps=["icon_raster"],
    )
    graph["label"] = Node(
        op_name="gfx:render_text",
        params={
            "text": label,
            "font": "Inter",
            "size": "${decimal('14') * canvas.width / 72}",
            "color": (255, 255, 255, 255),
        },
        deps=["canvas"],
    )
    anchor_id = layers[0]["id"]
    layers.append(
        {
            "image": ref("icon"),
            "anchor": relative(anchor_id, "cs@cs"),
            "id": "icon",
        }
    )
    layers.append(
        {
            "image": ref("label"),
            "anchor": relative(anchor_id, "ce@ce"),
            "id": "label",
        }
    )
    deps.extend(["icon", "label"])
    graph["output"] = Node(
        op_name="gfx:composite",
        params={"layers": layers},
        deps=deps,
    )
    return SubGraphNode(
        params={"canvas": ref("canvas")},
        deps=["canvas"],
        graph=graph,
        output="output",
    )


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
    return feedback_overlay("unavailable")


def ok_overlay() -> SubGraphNode:
    """SubGraphNode: green check icon centered on dark background."""
    return feedback_overlay("ok")


def solid_card(color: tuple[int, int, int, int] = BLACK) -> SubGraphNode:
    """SubGraphNode: solid color fill (canvas size). Use for blank/empty controls."""
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
