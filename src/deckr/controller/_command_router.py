"""Command routing: action updates -> store update -> resolve -> enqueue render."""

import logging
from collections.abc import Callable
from dataclasses import replace
from functools import wraps
from typing import TYPE_CHECKING

import anyio

from deckr.controller._render import RenderService, RenderSource, resolve
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import ControlStateStore, RenderOverlay
from deckr.controller.invariant.recipes import (
    STATUS_OVERLAY_STYLES,
    UNKNOWN_STATUS_OVERLAY,
)

if TYPE_CHECKING:
    from deckr.controller._device_layout import RasterImageFormat
    from deckr.controller._hardware import HardwareCommandService

logger = logging.getLogger(__name__)

OVERLAY_TEMPLATE_DEFAULT_SECONDS = {
    "ok": 1.2,
    "error": 2.0,
    "unavailable": 2.0,
    "unavailable_missing": 2.0,
    "unavailable_service": 2.0,
    "unavailable_session": 2.0,
    "unavailable_rejected": 2.0,
    "unavailable_unknown": 2.0,
    "unknown": 2.0,
}
OVERLAY_TEMPLATES = frozenset(STATUS_OVERLAY_STYLES)


def _store_content_kind(store: ControlStateStore) -> str:
    if store.overlay is not None:
        return f"overlay:{store.overlay.template}"
    if store.content.image is not None:
        if store.content.image.startswith("data:application/vnd.invariant.graph"):
            return "invariant_graph"
        if store.content.image.startswith("data:"):
            return "data_image"
        if store.content.image.startswith(("http://", "https://")):
            return "remote_image"
        return "image"
    if store.content.title is not None:
        return "title"
    return "empty"


class DeviceOutput:
    """Thin wrapper: writes raster frames to a selected control capability."""

    def __init__(
        self,
        command_service: "HardwareCommandService",
        config_id: str,
        control_id: str,
        capability_id: str,
    ):
        self._command_service = command_service
        self._config_id = config_id
        self._control_id = control_id
        self._capability_id = capability_id
        self.last_frame: bytes | None = None
        self._claim = (
            command_service.claim_identity(config_id)
            if hasattr(command_service, "claim_identity")
            else None
        )

    @property
    def control_id(self) -> str:
        return self._control_id

    async def write(self, frame: bytes) -> None:
        if (
            self._claim is not None
            and self._command_service.claim_identity(self._config_id) != self._claim
        ):
            raise ValueError("hardware claim changed")
        await self._command_service.set_raster_frame(
            self._config_id,
            self._control_id,
            self._capability_id,
            frame,
            **({"expected_claim": self._claim} if self._claim is not None else {}),
        )
        self.last_frame = frame

    async def clear(self) -> None:
        if (
            self._claim is not None
            and self._command_service.claim_identity(self._config_id) != self._claim
        ):
            raise ValueError("hardware claim changed")
        await self._command_service.clear_raster(
            self._config_id,
            self._control_id,
            self._capability_id,
            **({"expected_claim": self._claim} if self._claim is not None else {}),
        )
        self.last_frame = None

    async def write_tagged(self, frame: bytes, message_id: str) -> None:
        await self._command_service.set_raster_frame(
            self._config_id,
            self._control_id,
            self._capability_id,
            frame,
            message_id=message_id,
            expected_claim=self._claim,
        )
        self.last_frame = frame

    async def clear_tagged(self, message_id: str) -> None:
        await self._command_service.clear_raster(
            self._config_id,
            self._control_id,
            self._capability_id,
            message_id=message_id,
            expected_claim=self._claim,
        )
        self.last_frame = None


def _output_transaction(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        async with self._render_dispatcher.output_transaction():
            return await method(self, *args, **kwargs)

    return guarded


class CommandRouter:
    """Receives action commands, updates ControlStateStore, triggers resolve → encode → write."""

    def __init__(
        self,
        store: ControlStateStore,
        render_service: RenderService,
        render_dispatcher: RenderDispatcher,
        output: DeviceOutput | None,
        image_format: "RasterImageFormat | None",
        start_soon: Callable,
    ):
        self._store = store
        self._render_service = render_service
        self._render_dispatcher = render_dispatcher
        self._output = output
        self._image_format = image_format
        self._start_soon = start_soon

    @property
    def store(self):
        return self._store

    @property
    def output(self):
        return self._output

    @property
    def render_service(self):
        return self._render_service

    @property
    def image_format(self):
        return self._image_format

    async def _render(
        self,
        *,
        clear_when_empty: bool = False,
        source: RenderSource | None = None,
        base_update: bool = False,
    ) -> None:
        if self._image_format is None or self._output is None:
            return
        model = resolve(self._store)
        request = self._render_service.build_request(
            model,
            self._image_format,
            context_id=self._store.context_id,
            binding_id=self._store.binding_id,
            control_id=self._output.control_id,
            source=source,
        )
        if request is None and clear_when_empty:
            generation = await self._render_dispatcher.clear_control(
                self._output.control_id,
                context_id=self._store.context_id,
                binding_id=self._store.binding_id,
                output=self._output,
                base_update=base_update,
            )
            logger.debug(
                "Command router render clear enqueued control=%s binding=%s "
                "base_generation=%s overlay_generation=%s content_kind=%s "
                "request_generation=%s",
                self._output.control_id,
                self._store.binding_id,
                self._store.base_output_generation,
                self._store.overlay_generation,
                _store_content_kind(self._store),
                generation,
            )
            return
        generation = await self._render_dispatcher.submit_request(
            control_id=self._output.control_id,
            context_id=self._store.context_id,
            binding_id=self._store.binding_id,
            request=request,
            output=self._output,
            base_update=base_update,
        )
        logger.debug(
            "Command router render enqueue control=%s binding=%s "
            "base_generation=%s overlay_generation=%s content_kind=%s "
            "request_generation=%s request_present=%s",
            self._output.control_id,
            self._store.binding_id,
            self._store.base_output_generation,
            self._store.overlay_generation,
            _store_content_kind(self._store),
            generation,
            request is not None,
        )

    @_output_transaction
    async def render(
        self,
        *,
        source: RenderSource | None = None,
    ) -> None:
        """Trigger resolve, encode, and write after state changes."""
        await self._render(source=_source_with_content_kind(source, self._store))

    @_output_transaction
    async def set_title(
        self,
        text: str,
        *,
        generation: int | None = None,
    ) -> None:
        previous_generation = self._store.base_output_generation
        if not self._accept_base_generation(generation):
            return
        self._store.content.title = text
        self._store.content.image = None
        if (
            generation is None
            or self._store.base_output_generation > previous_generation
        ):
            self._store.overlay = None
        await self._render(base_update=True)

    @_output_transaction
    async def set_raster_image(
        self,
        image: str,
        *,
        generation: int | None = None,
        source: RenderSource | None = None,
    ) -> None:
        previous_generation = self._store.base_output_generation
        if not self._accept_base_generation(generation):
            return
        self._store.content.image = image
        self._store.content.title = None
        if (
            generation is None
            or self._store.base_output_generation > previous_generation
        ):
            self._store.overlay = None
        await self._render(source=source, base_update=True)

    @_output_transaction
    async def clear(self, *, generation: int | None = None) -> None:
        previous_generation = self._store.base_output_generation
        if not self._accept_base_generation(generation):
            return
        self._store.content.image = None
        self._store.content.title = None
        if (
            generation is None
            or self._store.base_output_generation > previous_generation
        ):
            self._store.overlay = None
        if self._output is None:
            return
        await self._render(clear_when_empty=True, base_update=True)

    @_output_transaction
    async def show_overlay(
        self,
        *,
        template: str,
        title: str | None,
        params: dict,
        duration_seconds: float | None,
        overlay_id: str | None,
        generation: int,
        binding_output_generation: int,
        source: RenderSource | None = None,
    ) -> bool:
        if binding_output_generation < self._store.base_output_generation:
            return False
        if generation < self._store.overlay_generation:
            return False

        resolved_template = template
        resolved_duration = duration_seconds
        if template not in OVERLAY_TEMPLATES:
            logger.warning(
                "Unknown binding overlay template %s; rendering unknown fallback",
                template,
            )
            resolved_template = UNKNOWN_STATUS_OVERLAY

        if resolved_duration is None:
            resolved_duration = OVERLAY_TEMPLATE_DEFAULT_SECONDS.get(resolved_template)

        self._store.overlay_generation = generation
        self._store.overlay = RenderOverlay(
            template=resolved_template,
            title=title,
            params=params,
            overlay_id=overlay_id,
            generation=generation,
        )
        await self._render(source=_source_with_content_kind(source, self._store))
        if resolved_duration is not None:
            self._start_soon(
                self._clear_overlay_after,
                overlay_id,
                generation,
                resolved_duration,
            )
        return True

    @_output_transaction
    async def clear_overlay(
        self,
        *,
        overlay_id: str | None,
        generation: int,
        binding_output_generation: int,
        source: RenderSource | None = None,
    ) -> bool:
        if binding_output_generation < self._store.base_output_generation:
            return False
        if generation < self._store.overlay_generation:
            return False
        overlay = self._store.overlay
        if overlay is None:
            return False
        if overlay_id is not None and overlay.overlay_id != overlay_id:
            return False

        self._store.overlay_generation = generation
        self._store.overlay = None
        await self._render(
            clear_when_empty=True,
            source=_source_with_content_kind(source, self._store),
        )
        return True

    async def _clear_overlay_after(
        self,
        overlay_id: str | None,
        generation: int,
        duration_seconds: float,
    ) -> None:
        await anyio.sleep(duration_seconds)
        await self.clear_overlay(
            overlay_id=overlay_id,
            generation=generation,
            binding_output_generation=self._store.base_output_generation,
        )

    def _accept_base_generation(self, generation: int | None) -> bool:
        if generation is None:
            self._store.base_output_generation += 1
            return True
        if generation < self._store.base_output_generation:
            return False
        self._store.base_output_generation = generation
        return True


def _source_with_content_kind(
    source: RenderSource | None,
    store: ControlStateStore,
) -> RenderSource | None:
    if source is None:
        return None
    return replace(source, content_kind=_store_content_kind(store))
