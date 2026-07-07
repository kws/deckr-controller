"""Render backend wrapper for diagnostic render observations."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from deckr.controller._render import RenderRequest, RenderResult, RenderSource
from deckr.controller._render_dispatcher import RenderBackend


@dataclass(frozen=True, slots=True)
class RenderObservationOptions:
    """Configuration for JSONL render observations."""

    path: Path
    include_graph: bool = False
    include_context: bool = False


class ObservingRenderBackend:
    """Wrap a render backend and append one JSONL record per render result."""

    def __init__(
        self,
        backend: RenderBackend,
        *,
        controller_id: str,
        options: RenderObservationOptions,
    ) -> None:
        self._backend = backend
        self._controller_id = controller_id
        self._options = options
        self._lock = anyio.Lock()
        options.path.parent.mkdir(parents=True, exist_ok=True)

    async def render(self, request: RenderRequest) -> RenderResult:
        started = time.perf_counter()
        try:
            result = await self._backend.render(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            result = RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                control_id=request.control_id,
                generation=request.generation,
                frame=None,
                error=str(exc),
            )
            await self._append_result(request, result, duration_ms=duration_ms)
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        await self._append_result(request, result, duration_ms=duration_ms)
        return result

    async def aclose(self) -> None:
        await self._backend.aclose()

    async def _append_result(
        self,
        request: RenderRequest,
        result: RenderResult,
        *,
        duration_ms: float,
    ) -> None:
        record = self._record(request, result, duration_ms=duration_ms)
        line = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        async with self._lock, await anyio.open_file(
            self._options.path,
            "a",
            encoding="utf-8",
        ) as stream:
            await stream.write(line)
            await stream.write("\n")

    def _record(
        self,
        request: RenderRequest,
        result: RenderResult,
        *,
        duration_ms: float,
    ) -> dict[str, Any]:
        source = request.source
        record: dict[str, Any] = {
            "event": "render.result",
            "controllerId": self._controller_id,
            "configId": request.config_id or None,
            "controlId": request.control_id,
            "contextId": request.context_id,
            "bindingId": request.binding_id,
            "renderGeneration": request.generation,
            "bindingOutputGeneration": _source_value(
                source,
                "binding_output_generation",
            ),
            "overlayGeneration": _source_value(source, "overlay_generation"),
            "providerInstanceId": _source_value(source, "provider_instance_id"),
            "providerId": _source_value(source, "provider_id"),
            "actionId": _source_value(source, "action_id"),
            "actionInstanceId": _source_value(source, "action_instance_id"),
            "actionMessageId": _source_value(source, "action_message_id"),
            "actionCausationId": _source_value(source, "action_causation_id"),
            "trace": _source_value(source, "trace"),
            "commandType": _source_value(source, "command_type"),
            "contentKind": _source_value(source, "content_kind"),
            "availabilityCause": _source_value(source, "availability_cause"),
            "availabilityState": _source_value(source, "availability_state"),
            "availabilitySource": _source_value(source, "availability_source"),
            "availabilityReason": _source_value(source, "availability_reason"),
            "graphSha256": _json_sha256(request.graph),
            "frameSha256": _bytes_sha256(result.frame),
            "encoding": "jpeg",
            "width": request.image_format.width,
            "height": request.image_format.height,
            "durationMs": duration_ms,
            "error": result.error,
        }
        if self._options.include_graph:
            record["graph"] = request.graph
        if self._options.include_context:
            record["context"] = request.context
        return record


def _source_value(source: RenderSource | None, name: str) -> Any:
    if source is None:
        return None
    return getattr(source, name)


def _json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bytes_sha256(value: bytes | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value).hexdigest()
