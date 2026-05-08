"""Command routing: action updates -> store update -> resolve -> enqueue render."""

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

import anyio
from deckr.actions.messages import SettingsTargetRef
from deckr.contracts.models import thaw_json

from deckr.controller._render import RenderService, resolve
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import ControlStateStore, RenderOverlay
from deckr.controller.settings import SettingsService

if TYPE_CHECKING:
    from deckr.controller._device_layout import RasterImageFormat
    from deckr.controller._hardware_service import HardwareCommandService

logger = logging.getLogger(__name__)

OVERLAY_TEMPLATE_DEFAULT_SECONDS = {
    "ok": 1.2,
    "error": 2.0,
    "unavailable": 2.0,
    "unknown": 2.0,
}
OVERLAY_TEMPLATES = frozenset(
    {
        "ok",
        "error",
        "unavailable",
        "pending",
        "loading",
        "unknown",
    }
)


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

    @property
    def control_id(self) -> str:
        return self._control_id

    async def write(self, frame: bytes) -> None:
        await self._command_service.set_raster_frame(
            self._config_id,
            self._control_id,
            self._capability_id,
            frame,
        )
        self.last_frame = frame

    async def clear(self) -> None:
        await self._command_service.clear_raster(
            self._config_id,
            self._control_id,
            self._capability_id,
        )
        self.last_frame = None


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
        *,
        settings_service: SettingsService | None = None,
        settings_target: SettingsTargetRef | None = None,
    ):
        self._store = store
        self._render_service = render_service
        self._render_dispatcher = render_dispatcher
        self._output = output
        self._image_format = image_format
        self._start_soon = start_soon
        self._settings_service = settings_service
        self._settings_target = settings_target
        self._settings_hydrated = False

    async def _render(self, *, clear_when_empty: bool = False) -> None:
        if self._image_format is None or self._output is None:
            return
        model = resolve(self._store)
        request = self._render_service.build_request(
            model,
            self._image_format,
            context_id=self._store.context_id,
            binding_id=self._store.binding_id,
            control_id=self._output.control_id,
        )
        if request is None and clear_when_empty:
            await self._render_dispatcher.clear_control(
                self._output.control_id,
                context_id=self._store.context_id,
                binding_id=self._store.binding_id,
                output=self._output,
            )
            return
        await self._render_dispatcher.submit_request(
            control_id=self._output.control_id,
            context_id=self._store.context_id,
            binding_id=self._store.binding_id,
            request=request,
            output=self._output,
        )

    async def render(self) -> None:
        """Trigger resolve, encode, and write after state changes."""
        await self._render()

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
        if generation is None or self._store.base_output_generation > previous_generation:
            self._store.overlay = None
        await self._render()

    async def set_raster_image(self, image: str, *, generation: int | None = None) -> None:
        previous_generation = self._store.base_output_generation
        if not self._accept_base_generation(generation):
            return
        self._store.content.image = image
        self._store.content.title = None
        if generation is None or self._store.base_output_generation > previous_generation:
            self._store.overlay = None
        await self._render()

    async def clear(self, *, generation: int | None = None) -> None:
        previous_generation = self._store.base_output_generation
        if not self._accept_base_generation(generation):
            return
        self._store.content.image = None
        self._store.content.title = None
        if generation is None or self._store.base_output_generation > previous_generation:
            self._store.overlay = None
        if self._output is None:
            return
        await self._render_dispatcher.clear_control(
            self._output.control_id,
            context_id=self._store.context_id,
            binding_id=self._store.binding_id,
            output=self._output,
        )

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
            resolved_template = "unknown"

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
        await self._render()
        if resolved_duration is not None:
            self._start_soon(
                self._clear_overlay_after,
                overlay_id,
                generation,
                resolved_duration,
            )
        return True

    async def clear_overlay(
        self,
        *,
        overlay_id: str | None,
        generation: int,
        binding_output_generation: int,
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
        await self._render(clear_when_empty=True)
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

    async def hydrate_settings(self) -> None:
        """Load live runtime settings into store. Precedence: config, then runtime overlay."""
        if self._settings_hydrated:
            return

        if self._settings_service is not None and self._settings_target is not None:
            snapshot = await self._settings_service.get(self._settings_target)
            merged = dict(thaw_json(self._store.settings))
            merged.update(snapshot.settings)
            self._store.settings = merged

        self._settings_hydrated = True

    async def set_settings(self, settings: dict) -> SimpleNamespace:
        """Merge settings into the live runtime overlay."""
        if not self._settings_hydrated:
            await self.hydrate_settings()

        candidate = dict(thaw_json(self._store.settings))
        candidate.update(dict(thaw_json(settings)))

        merged = candidate
        if self._settings_service is not None and self._settings_target is not None:
            try:
                snapshot = await self._settings_service.patch(
                    self._settings_target,
                    settings,
                )
                merged = dict(thaw_json(snapshot.settings))
            except Exception:
                logger.exception(
                    "Failed to update runtime settings for context %s",
                    self._store.context_id,
                )
                raise

        self._store.settings = merged
        self._settings_hydrated = True
        return SimpleNamespace(**self._store.settings)

    async def get_settings(self) -> SimpleNamespace:
        if not self._settings_hydrated:
            await self.hydrate_settings()
        return SimpleNamespace(**self._store.settings)
