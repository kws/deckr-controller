"""Render pipeline: resolve declarations to RenderModel and render requests."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from invariant import (
    Node,
    SubGraphNode,
    dump_graph_output_to_dict,
    load_graph_output_data_uri,
    load_graph_output_from_dict,
    ref,
)

from deckr.controller._device_layout import RasterImageFormat
from deckr.controller._state_store import ControlStateStore, RenderContent
from deckr.controller.invariant.executor import get_executor
from deckr.controller.invariant.recipes import (
    feedback_overlay,
    image_card,
    solid_card,
    title_card,
)

logger = logging.getLogger(__name__)


@dataclass
class RenderModel:
    """Ephemeral resolved content for one render. Not stored."""

    title: str | None = None
    image: str | None = None
    overlay_type: str | None = None
    overlay_title: str | None = None


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
    control_id: str
    generation: int
    image_format: RenderImageFormat
    graph: dict[str, Any]
    binding_id: str | None = None
    delay_ms: int = 0


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Rendered JPEG bytes for a specific control generation."""

    context_id: str
    control_id: str
    generation: int
    frame: bytes | None
    binding_id: str | None = None
    error: str | None = None


def _content_to_model(content: RenderContent) -> RenderModel:
    """Build RenderModel from the current render content."""
    if content.image is not None:
        return RenderModel(image=content.image)
    if content.title is not None:
        if content.title == "":
            return RenderModel(overlay_type="blank")
        return RenderModel(title=content.title)
    return RenderModel()


def resolve(
    store: ControlStateStore,
    now: float | None = None,
) -> RenderModel:
    """Pure function: declarations -> RenderModel."""
    del now
    model = _content_to_model(store.content)
    if store.overlay is not None:
        model.overlay_type = store.overlay.template
        model.overlay_title = store.overlay.title
    return model


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


def _to_render_image_format(image_format: RasterImageFormat) -> RenderImageFormat:
    return RenderImageFormat(
        width=image_format.width,
        height=image_format.height,
        rotation=image_format.rotation,
    )


def _to_hw_image_format(image_format: RenderImageFormat) -> RasterImageFormat:
    return RasterImageFormat(
        width=image_format.width,
        height=image_format.height,
        rotation=image_format.rotation,
    )


def _model_to_graph(
    model: RenderModel, image_format: RasterImageFormat
) -> Node | SubGraphNode | None:
    """Resolve a RenderModel to the graph that should be executed."""

    del image_format
    if model.overlay_type == "blank":
        return solid_card()
    if model.overlay_type is not None:
        base = _base_model_to_graph(model)
        return feedback_overlay(
            model.overlay_type,
            title=model.overlay_title,
            base=base,
        )
    return _base_model_to_graph(model)


def _base_model_to_graph(model: RenderModel) -> Node | SubGraphNode | None:
    if model.image is not None:
        parsed = load_graph_output_data_uri(model.image)
        if parsed is not None:
            return _graph_output_to_node(*parsed)
        return image_card(model.image)
    if model.title is not None:
        return title_card(model.title)
    return None


def build_render_request(
    model: RenderModel,
    image_format: RasterImageFormat,
    *,
    context_id: str = "",
    binding_id: str | None = None,
    control_id: str = "",
    generation: int = 0,
) -> RenderRequest | None:
    """Convert a RenderModel to a serialized render request."""

    graph = _model_to_graph(model, image_format)
    if graph is None:
        return None

    return RenderRequest(
        context_id=context_id,
        binding_id=binding_id,
        control_id=control_id,
        generation=generation,
        image_format=_to_render_image_format(image_format),
        graph=_node_to_wire(graph),
    )


def _graph_to_jpeg_bytes(
    node: Node | SubGraphNode, image_format: RasterImageFormat
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
        image_format: RasterImageFormat,
        *,
        context_id: str = "",
        binding_id: str | None = None,
        control_id: str = "",
        generation: int = 0,
    ) -> RenderRequest | None:
        return build_render_request(
            model,
            image_format,
            context_id=context_id,
            binding_id=binding_id,
            control_id=control_id,
            generation=generation,
        )
