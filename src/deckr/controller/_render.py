"""Render pipeline: resolve declarations to RenderModel and render requests."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from deckr.hardware.messages import HardwareImageFormat
from invariant import (
    Node,
    SubGraphNode,
    dump_graph_output_to_dict,
    load_graph_output_data_uri,
    load_graph_output_from_dict,
    ref,
)

from deckr.controller._state_store import ControlStateStore, RenderContent, TitleOptions
from deckr.controller._title_defaults import (
    DEFAULT_FONT_FAMILY,
    DEFAULT_FONT_SIZE,
    DEFAULT_TITLE_ALIGNMENT,
    DEFAULT_TITLE_COLOR,
)
from deckr.controller.invariant.executor import get_executor
from deckr.controller.invariant.recipes import (
    alert_overlay,
    image_card,
    ok_overlay,
    solid_card,
    title_card,
    unavailable_overlay,
)

logger = logging.getLogger(__name__)


@dataclass
class RenderModel:
    """Ephemeral resolved content for one render. Not stored."""

    title: str | None = None
    image: str | None = None
    overlay_type: Literal["alert", "ok", "unavailable", "blank"] | None = None
    title_options: TitleOptions | None = None


@dataclass(frozen=True, slots=True)
class RenderImageFormat:
    """JSON/pickle-friendly image format for worker render requests."""

    width: int
    height: int
    rotation: int = 0


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """Serialized render payload suitable for thread/process backends."""

    context_id: str
    slot_id: str
    generation: int
    image_format: RenderImageFormat
    graph: dict[str, Any]
    binding_id: str | None = None
    delay_ms: int = 0


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Rendered JPEG bytes for a specific slot generation."""

    context_id: str
    slot_id: str
    generation: int
    frame: bytes | None
    binding_id: str | None = None
    error: str | None = None


def _content_to_model(
    content: RenderContent, default_title_options: TitleOptions | None = None
) -> RenderModel:
    """Build RenderModel from the current render content."""
    if content.image is not None:
        return RenderModel(image=content.image)
    if content.title is not None:
        if content.title == "":
            return RenderModel(overlay_type="blank")
        opts = (
            content.title_options
            if content.title_options is not None
            else default_title_options
        )
        return RenderModel(title=content.title, title_options=opts)
    return RenderModel()


def resolve(
    store: ControlStateStore,
    now: float | None = None,
) -> RenderModel:
    """Pure function: declarations → RenderModel."""
    if now is None:
        now = time.monotonic()

    if store.overlay is not None and now < store.overlay.expires_at:
        return RenderModel(overlay_type=store.overlay.type)

    return _content_to_model(store.content, store.default_title_options)


def _hex_to_rgba(hex_color: str) -> tuple[int, int, int, int]:
    """Convert hex color (e.g. #FFFFFF or #FFF) to RGBA tuple (0-255)."""
    hex_color = hex_color.strip().lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    if len(hex_color) != 6 or not re.match(r"^[0-9a-fA-F]+$", hex_color):
        return (255, 255, 255, 255)
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return (r, g, b, 255)


def _font_style_to_weight_and_style(
    font_style: str | None,
) -> tuple[int | None, str]:
    """Map title font_style strings to render-text weight/style values."""
    if not font_style or font_style == "Regular":
        return (400, "normal")
    if font_style == "Bold":
        return (700, "normal")
    if font_style == "Italic":
        return (None, "italic")
    if font_style == "Bold Italic":
        return (700, "italic")
    return (400, "normal")


def _parse_font_size(
    font_size: int | str | None,
    default_font_size: str = DEFAULT_FONT_SIZE,
) -> dict:
    """Parse font_size config into size/fit_width/needs_canvas for title_card."""
    if font_size is None:
        font_size = default_font_size
    elif isinstance(font_size, int):
        return {"size": font_size, "fit_width": None, "needs_canvas": False}

    s = str(font_size).strip()
    if not s:
        s = str(default_font_size).strip()

    px_match = re.match(r"^(\d+)\s*px$", s, re.IGNORECASE)
    if px_match:
        return {
            "size": int(px_match.group(1)),
            "fit_width": None,
            "needs_canvas": False,
        }

    rem_match = re.match(r"^(\d+(?:\.\d+)?)\s*(?:rem|em)$", s, re.IGNORECASE)
    if rem_match:
        multiplier = float(rem_match.group(1))
        base = int(14 * multiplier)
        cel = f'${{decimal("{base}") * canvas.width / 72}}'
        return {"size": cel, "fit_width": None, "needs_canvas": True}

    vw_match = re.match(r"^(\d+(?:\.\d+)?)\s*vw$", s, re.IGNORECASE)
    if vw_match:
        value = float(vw_match.group(1))
        fraction = value / 100
        if fraction == 1.0:
            fit_width_cel = "${canvas.width}"
        else:
            fit_width_cel = f'${{decimal("{fraction}") * canvas.width}}'
        return {"size": None, "fit_width": fit_width_cel, "needs_canvas": True}

    raise ValueError(
        f"font_size must be int, '14px', '1rem', '80vw', etc.; got {font_size!r}"
    )


def _title_options_to_params(
    opts: TitleOptions | None, image_format: HardwareImageFormat
) -> dict:
    """Convert TitleOptions to kwargs for title_card."""
    if opts is None:
        parsed = _parse_font_size(None)
        return {
            "font": DEFAULT_FONT_FAMILY,
            "font_size": parsed["size"],
            "fit_width": parsed["fit_width"],
            "needs_canvas": parsed["needs_canvas"],
            "color": _hex_to_rgba(DEFAULT_TITLE_COLOR),
            "title_alignment": DEFAULT_TITLE_ALIGNMENT,
            "weight": None,
            "style": "normal",
        }

    font = opts.font_family if opts.font_family else DEFAULT_FONT_FAMILY
    parsed = _parse_font_size(opts.font_size if opts.font_size is not None else None)
    color = (
        _hex_to_rgba(opts.title_color)
        if opts.title_color
        else _hex_to_rgba(DEFAULT_TITLE_COLOR)
    )
    weight, style = _font_style_to_weight_and_style(opts.font_style)

    return {
        "font": font,
        "font_size": parsed["size"],
        "fit_width": parsed["fit_width"],
        "needs_canvas": parsed["needs_canvas"],
        "color": color,
        "title_alignment": opts.title_alignment,
        "weight": weight,
        "style": style,
    }


def _node_to_wire(node: Node | SubGraphNode) -> dict[str, Any]:
    """Serialize a render node to the invariant graph wire format."""

    if isinstance(node, SubGraphNode):
        return dump_graph_output_to_dict(node.graph, node.output)
    return dump_graph_output_to_dict({"output": node}, "output")


def _graph_output_to_node(graph_dict: dict[str, Any], output: str) -> SubGraphNode:
    """Build a canvas-aware SubGraphNode from graph/output parts."""

    return SubGraphNode(
        params={"canvas": ref("canvas")},
        deps=["canvas"],
        graph=graph_dict,
        output=output,
    )


def _wire_to_node(wire: dict[str, Any]) -> SubGraphNode:
    """Rehydrate a wire-serialized graph into a canvas-aware SubGraphNode."""

    graph_dict, output = load_graph_output_from_dict(wire)
    return _graph_output_to_node(graph_dict, output)


def _to_render_image_format(image_format: HardwareImageFormat) -> RenderImageFormat:
    return RenderImageFormat(
        width=image_format.width,
        height=image_format.height,
        rotation=image_format.rotation,
    )


def _to_hw_image_format(image_format: RenderImageFormat) -> HardwareImageFormat:
    return HardwareImageFormat(
        width=image_format.width,
        height=image_format.height,
        rotation=image_format.rotation,
    )


def _model_to_graph(
    model: RenderModel, image_format: HardwareImageFormat
) -> Node | SubGraphNode | None:
    """Resolve a RenderModel to the graph that should be executed."""

    if model.overlay_type == "alert":
        return alert_overlay()
    if model.overlay_type == "ok":
        return ok_overlay()
    if model.overlay_type == "unavailable":
        return unavailable_overlay()
    if model.overlay_type == "blank":
        return solid_card()
    if model.image is not None:
        parsed = load_graph_output_data_uri(model.image)
        if parsed is not None:
            return _graph_output_to_node(*parsed)
        return image_card(model.image)
    if model.title is not None:
        params = _title_options_to_params(model.title_options, image_format)
        return title_card(model.title, **params)
    return None


def build_render_request(
    model: RenderModel,
    image_format: HardwareImageFormat,
    *,
    context_id: str = "",
    binding_id: str | None = None,
    slot_id: str = "",
    generation: int = 0,
) -> RenderRequest | None:
    """Convert a RenderModel to a serialized render request."""

    graph = _model_to_graph(model, image_format)
    if graph is None:
        return None

    return RenderRequest(
        context_id=context_id,
        binding_id=binding_id,
        slot_id=slot_id,
        generation=generation,
        image_format=_to_render_image_format(image_format),
        graph=_node_to_wire(graph),
    )


def _graph_to_jpeg_bytes(
    node: Node | SubGraphNode, image_format: HardwareImageFormat
) -> bytes:
    """Run invariant-gfx graph with canvas context; apply rotation; return JPEG bytes."""

    graph = {"src": node}
    img_name = "src"
    if image_format.rotation != 0:
        graph["rotated"] = Node(
            op_name="gfx:rotate",
            params={"image": ref("src"), "angle": image_format.rotation},
            deps=["src"],
        )
        img_name = "rotated"

    graph["output"] = Node(
        op_name="deckr:encode_jpeg",
        params={"image": ref(img_name), "quality": 100},
        deps=[img_name],
    )

    canvas = {"width": image_format.width, "height": image_format.height}
    results = get_executor().execute(graph, context={"canvas": canvas})
    artifact = results["output"]
    return artifact.data


def render_request_to_jpeg(request: RenderRequest) -> bytes:
    """Worker-side render function used by thread/process backends."""

    if request.delay_ms > 0:
        time.sleep(request.delay_ms / 1000)
    node = _wire_to_node(request.graph)
    image_format = _to_hw_image_format(request.image_format)
    return _graph_to_jpeg_bytes(node, image_format)


class RenderService:
    """Builds render requests for the render dispatcher."""

    def build_request(
        self,
        model: RenderModel,
        image_format: HardwareImageFormat,
        *,
        context_id: str = "",
        binding_id: str | None = None,
        slot_id: str = "",
        generation: int = 0,
    ) -> RenderRequest | None:
        return build_render_request(
            model,
            image_format,
            context_id=context_id,
            binding_id=binding_id,
            slot_id=slot_id,
            generation=generation,
        )
