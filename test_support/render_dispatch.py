from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
from invariant import Node, SubGraphNode, dump_graph_data_uri, dump_graph_to_dict
from invariant.params import ref
from invariant.store.disk import DiskStore
from invariant_gfx.artifacts import BlobArtifact
from PIL import Image

from deckr.controller._raster import RasterFormat
from deckr.controller._render import (
    RenderRequest,
    RenderResult,
    RenderSource,
)


class FakeHardwareCommandService:
    def claim_identity(self, config_id):
        return None

    def __init__(self):
        self.set_raster_frame = AsyncMock()
        self.clear_raster = AsyncMock()


def _png_data_uri() -> str:
    image = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class ControlledBackend:
    """Backend that allows tests to control when each generation completes."""

    def __init__(self):
        self.calls: list[int] = []
        self.requests: list[RenderRequest] = []
        self._events: dict[int, anyio.Event] = {}

    async def render(self, request: RenderRequest) -> RenderResult:
        self.calls.append(request.generation)
        self.requests.append(request)
        event = self._events.setdefault(request.generation, anyio.Event())
        await event.wait()
        return RenderResult(
            context_id=request.context_id,
            binding_id=request.binding_id,
            control_id=request.control_id,
            generation=request.generation,
            frame=f"frame-{request.generation}".encode(),
        )

    def release(self, generation: int) -> None:
        self._events.setdefault(generation, anyio.Event()).set()

    async def aclose(self) -> None:
        return


def _solid_request() -> RenderRequest:
    graph = {
        "output": Node(
            op_name="stdlib:identity",
            params={"value": None},
            deps=[],
        )
    }
    return RenderRequest(
        context_id="ctx",
        control_id="0,0",
        generation=0,
        image_format=RasterFormat(width=72, height=72),
        graph=dump_graph_to_dict(graph, output="output"),
    )


class ImmediateBackend:
    def __init__(
        self,
        *,
        frame: bytes | None = b"frame",
        error: str | None = None,
    ):
        self.frame = frame
        self.error = error
        self.closed = False
        self.calls: list[RenderRequest] = []

    async def render(self, request: RenderRequest) -> RenderResult:
        self.calls.append(request)
        return RenderResult(
            context_id=request.context_id,
            binding_id=request.binding_id,
            control_id=request.control_id,
            generation=request.generation,
            frame=self.frame,
            error=self.error,
        )

    async def aclose(self) -> None:
        self.closed = True


def _request_with_graph(graph: dict) -> RenderRequest:
    return RenderRequest(
        config_id="device-1",
        context_id="ctx-1",
        binding_id="binding-1",
        control_id="0,0",
        generation=7,
        image_format=RasterFormat(width=72, height=64),
        graph=graph,
        context={"setting": "value"},
        source=RenderSource(
            provider_instance_id="provider-instance",
            provider_id="dev.deckr.clock",
            provider_session_id="provider-session",
            action_id="dev.deckr.clock.action.digital",
            action_instance_id="action-instance",
            action_message_id="message-1",
            action_causation_id="cause-1",
            trace={"traceParent": "00-abc"},
            command_type="set_frame",
            content_kind="invariant_graph",
            binding_output_generation=3,
        ),
    )


def _json_hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _frame_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_observations(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _custom_graph() -> SubGraphNode:
    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (32, 64, 96, 255),
            },
            deps=["canvas"],
        )
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")},
        deps=["canvas"],
        graph=inner,
        output="bg",
    )


def _graph_data_uri() -> str:
    graph = _custom_graph()
    return dump_graph_data_uri(graph.graph, output=graph.output)


def _graph_data_uri_with_query_context() -> str:
    graph = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": ref("color"),
            },
            deps=["canvas", "color"],
        )
    }
    return dump_graph_data_uri(
        graph,
        output="bg",
        context={"color": (32, 64, 96, 255)},
    )


def _write_blob_to_store(cache_dir: str, payload: bytes) -> bytes:
    store = DiskStore(cache_dir=cache_dir)
    blob = BlobArtifact(data=payload, content_type="application/octet-stream")
    store.put("test:blob", "a" * 64, blob)
    return store.get("test:blob", "a" * 64).data
