"""Overlay recipes for controller-owned status render states."""

from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class StatusOverlayStyle:
    icon: str
    color: tuple[int, int, int, int]
    title: str


STATUS_OVERLAY_STYLES: dict[str, StatusOverlayStyle] = {
    "ok": StatusOverlayStyle("lucide:circle-check", COLOR_GREEN, "OK"),
    "error": StatusOverlayStyle("lucide:circle-x", COLOR_RED, "Error"),
    "unavailable": StatusOverlayStyle(
        "lucide:circle-alert",
        COLOR_SLATE,
        "Unavailable",
    ),
    "pending": StatusOverlayStyle("lucide:circle-ellipsis", COLOR_BLUE, "Pending"),
    "loading": StatusOverlayStyle("lucide:circle-dashed", COLOR_BLUE, "Loading"),
    "unknown": StatusOverlayStyle(
        "lucide:circle-question-mark",
        COLOR_AMBER,
        "Unknown",
    ),
}

UNKNOWN_STATUS_OVERLAY = "unknown"


def status_overlay_style(template: str) -> StatusOverlayStyle:
    return STATUS_OVERLAY_STYLES.get(
        template,
        STATUS_OVERLAY_STYLES[UNKNOWN_STATUS_OVERLAY],
    )


def feedback_overlay(
    template: str,
    *,
    title: str | None = None,
    base: Node | SubGraphNode | None = None,
) -> SubGraphNode:
    """SubGraphNode: semantic transient feedback over an optional base image."""

    style = status_overlay_style(template)
    label = title or style.title
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
        params={"name": style.icon},
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
        params={"image": ref("icon_raster"), "color": style.color},
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
    """SubGraphNode: unknown status overlay for legacy alert call sites."""
    return feedback_overlay("unknown")


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
