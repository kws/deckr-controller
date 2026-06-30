"""Render pipeline: resolve declarations to RenderModel and render requests."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from invariant import (
    Node,
    SubGraphNode,
    dump_graph_to_dict,
    load_graph_data_uri,
    load_graph_document_from_dict,
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
class RenderSource:
    """Diagnostic source metadata for a render request."""

    provider_instance_id: str | None = None
    provider_id: str | None = None
    action_id: str | None = None
    action_instance_id: str | None = None
    action_message_id: str | None = None
    action_causation_id: str | None = None
    trace: dict[str, Any] | None = None
    command_type: str | None = None
    content_kind: str | None = None
    binding_output_generation: int | None = None


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """Serialized render payload suitable for thread/process backends."""

    context_id: str
    control_id: str
    generation: int
    image_format: RenderImageFormat
    graph: dict[str, Any]
    config_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    binding_id: str | None = None
    source: RenderSource | None = None
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


@dataclass(frozen=True, slots=True)
class _GraphDocument:
    graph: dict[str, Any]
    output: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _RenderNode:
    node: SubGraphNode
    context: dict[str, Any] = field(default_factory=dict)


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


def _require_output(output: str | None) -> str:
    if output is None:
        raise ValueError("Invariant graph render documents must declare an output")
    return output


def _node_to_document(node: Node | SubGraphNode) -> _GraphDocument:
    """Convert a render node to a graph document."""

    if isinstance(node, SubGraphNode):
        return _GraphDocument(node.graph, node.output)
    return _GraphDocument({"output": node}, "output")


def _document_to_wire(document: _GraphDocument) -> dict[str, Any]:
    """Serialize a render graph document to the invariant graph wire format."""

    return dump_graph_to_dict(document.graph, output=document.output)


def _bind_document_context(
    context: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Bind graph execution context through refs so values stay literal."""

    params: dict[str, Any] = {"canvas": ref("canvas")}
    deps = ["canvas"]
    bound_context: dict[str, Any] = {}
    for index, key in enumerate(sorted(context)):
        if not isinstance(key, str) or not key:
            raise ValueError(
                "Invariant graph render context keys must be non-empty strings"
            )
        if key == "canvas":
            raise ValueError("Invariant graph render context must not include 'canvas'")
        dep = f"__deckr_graph_context_{index}"
        params[key] = ref(dep)
        deps.append(dep)
        bound_context[dep] = context[key]
    return params, deps, bound_context


def _graph_document_to_node(
    graph_dict: dict[str, Any],
    output: str | None,
    context: dict[str, Any] | None = None,
) -> _RenderNode:
    """Build a canvas-aware SubGraphNode from a graph document."""

    params, deps, bound_context = _bind_document_context(context or {})
    return _RenderNode(
        node=SubGraphNode(
            params=params,
            deps=deps,
            graph=graph_dict,
            output=_require_output(output),
        ),
        context=bound_context,
    )


def _wire_to_node(wire: dict[str, Any], context: dict[str, Any]) -> _RenderNode:
    """Rehydrate a wire-serialized graph into a canvas-aware SubGraphNode."""

    graph_dict, output = load_graph_document_from_dict(wire)
    return _graph_document_to_node(graph_dict, output, context)


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


def _model_to_graph_document(
    model: RenderModel, image_format: RasterImageFormat
) -> _GraphDocument | None:
    """Resolve a RenderModel to the graph that should be executed."""

    del image_format
    if model.overlay_type == "blank":
        return _node_to_document(solid_card())
    if model.overlay_type is not None:
        base_document = _base_model_to_graph_document(model)
        base_node = None
        context: dict[str, Any] = {}
        if base_document is not None:
            bound_base = _graph_document_to_node(
                base_document.graph,
                base_document.output,
                base_document.context,
            )
            base_node = bound_base.node
            context = bound_base.context
        overlay = feedback_overlay(
            model.overlay_type,
            title=model.overlay_title,
            base=base_node,
        )
        document = _node_to_document(overlay)
        return _GraphDocument(document.graph, document.output, context)
    return _base_model_to_graph_document(model)


def _base_model_to_graph_document(model: RenderModel) -> _GraphDocument | None:
    if model.image is not None:
        parsed = load_graph_data_uri(model.image)
        if parsed is not None:
            graph_dict, output, context = parsed
            return _GraphDocument(graph_dict, _require_output(output), context)
        return _node_to_document(image_card(model.image))
    if model.title is not None:
        return _node_to_document(title_card(model.title))
    return None


def build_render_request(
    model: RenderModel,
    image_format: RasterImageFormat,
    *,
    config_id: str = "",
    context_id: str = "",
    binding_id: str | None = None,
    control_id: str = "",
    generation: int = 0,
    source: RenderSource | None = None,
) -> RenderRequest | None:
    """Convert a RenderModel to a serialized render request."""

    document = _model_to_graph_document(model, image_format)
    if document is None:
        return None

    return RenderRequest(
        config_id=config_id,
        context_id=context_id,
        binding_id=binding_id,
        control_id=control_id,
        generation=generation,
        image_format=_to_render_image_format(image_format),
        graph=_document_to_wire(document),
        context=document.context,
        source=source,
    )


def _graph_to_jpeg_bytes(
    render_node: _RenderNode, image_format: RasterImageFormat
) -> bytes:
    """Run invariant-gfx graph with canvas context; apply rotation; return JPEG bytes."""

    graph = {"src": render_node.node}
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
        params={"image": ref(img_name), "quality": 50},
        deps=[img_name],
    )

    canvas = {"width": image_format.width, "height": image_format.height}
    context = dict(render_node.context)
    context["canvas"] = canvas
    results = get_executor().execute(graph, ["output"], context=context)
    artifact = results["output"]
    return artifact.data


def render_request_to_jpeg(request: RenderRequest) -> bytes:
    """Worker-side render function used by thread/process backends."""

    if request.delay_ms > 0:
        time.sleep(request.delay_ms / 1000)
    node = _wire_to_node(request.graph, request.context)
    image_format = _to_hw_image_format(request.image_format)
    return _graph_to_jpeg_bytes(node, image_format)


class RenderService:
    """Builds render requests for the render dispatcher."""

    def build_request(
        self,
        model: RenderModel,
        image_format: RasterImageFormat,
        *,
        config_id: str = "",
        context_id: str = "",
        binding_id: str | None = None,
        control_id: str = "",
        generation: int = 0,
        source: RenderSource | None = None,
    ) -> RenderRequest | None:
        return build_render_request(
            model,
            image_format,
            config_id=config_id,
            context_id=context_id,
            binding_id=binding_id,
            control_id=control_id,
            generation=generation,
            source=source,
        )
