"""Command routing: plugin commands → store update → resolve → enqueue render."""

import logging
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

import anyio
from deckr.pluginhost.messages import TitleOptions

from deckr.controller._render import RenderService, resolve
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import (
    ControlStateStore,
    TransientOverlay,
)
from deckr.controller.settings import SettingsService, SettingsTarget

if TYPE_CHECKING:
    from deckr.hardware.events import HardwareImageFormat

    from deckr.controller._hardware_service import HardwareCommandService

logger = logging.getLogger(__name__)


class DeviceOutput:
    """Thin wrapper: writes bytes to device; records last frame per slot."""

    def __init__(
        self,
        command_service: "HardwareCommandService",
        config_id: str,
        slot_id: str,
    ):
        self._command_service = command_service
        self._config_id = config_id
        self._slot_id = slot_id
        self.last_frame: bytes | None = None

    @property
    def slot_id(self) -> str:
        return self._slot_id

    async def write(self, frame: bytes) -> None:
        await self._command_service.set_image(self._config_id, self._slot_id, frame)
        self.last_frame = frame

    async def clear(self) -> None:
        await self._command_service.clear_slot(self._config_id, self._slot_id)
        self.last_frame = None


class CommandRouter:
    """Receives plugin commands, updates ControlStateStore, triggers resolve → encode → write."""

    def __init__(
        self,
        store: ControlStateStore,
        render_service: RenderService,
        render_dispatcher: RenderDispatcher,
        output: DeviceOutput,
        image_format: "HardwareImageFormat | None",
        start_soon: Callable,
        *,
        settings_service: SettingsService | None = None,
        settings_target: SettingsTarget | None = None,
    ):
        self._store = store
        self._render_service = render_service
        self._render_dispatcher = render_dispatcher
        self._output = output
        self._image_format = image_format
        self._start_soon = start_soon
        self._overlay_token: int = 0
        self._settings_service = settings_service
        self._settings_target = settings_target
        self._settings_hydrated = False

    async def _render(self) -> None:
        if self._image_format is None:
            return
        model = resolve(self._store)
        request = self._render_service.build_request(
            model,
            self._image_format,
            context_id=self._store.context_id,
            slot_id=self._output.slot_id,
        )
        await self._render_dispatcher.submit_request(
            slot_id=self._output.slot_id,
            context_id=self._store.context_id,
            request=request,
            output=self._output,
        )

    async def render(self) -> None:
        """Trigger resolve → encode → write (e.g. after willAppear)."""
        await self._render()

    async def set_title(
        self,
        text: str,
        *,
        title_options: TitleOptions | None = None,
    ) -> None:
        self._store.content.title = text
        self._store.content.image = None
        self._store.content.title_options = title_options
        await self._render()

    async def set_image(self, image: str) -> None:
        self._store.content.image = image
        self._store.content.title = None
        await self._render()

    async def show_alert(self) -> None:
        self._overlay_token += 1
        token = self._overlay_token
        self._store.overlay = TransientOverlay(
            type="alert", expires_at=time.monotonic() + 1.0
        )
        await self._render()
        self._start_soon(self._expire_overlay, token)

    async def show_ok(self) -> None:
        self._overlay_token += 1
        token = self._overlay_token
        self._store.overlay = TransientOverlay(
            type="ok", expires_at=time.monotonic() + 1.0
        )
        await self._render()
        self._start_soon(self._expire_overlay, token)

    async def _expire_overlay(self, token: int) -> None:
        try:
            await anyio.sleep(1.0)
            if token != self._overlay_token:
                return
            self._store.overlay = None
            await self._render()
        except Exception:
            logger.exception(
                "Failed to expire overlay for context %s",
                self._store.context_id,
            )

    async def hydrate_settings(self) -> None:
        """Load settings from persistence into store. Precedence: config defaults, then persisted overrides (persisted wins)."""
        if self._settings_hydrated:
            return

        if self._settings_service is not None and self._settings_target is not None:
            persisted = await self._settings_service.get(self._settings_target)
            merged = dict(self._store.settings)
            merged.update(persisted)
            self._store.settings = merged

        self._settings_hydrated = True

    async def set_settings(self, settings: dict) -> SimpleNamespace:
        """Merge settings and persist. Fail-fast: on persistence write failure we do not update in-memory store."""
        if not self._settings_hydrated:
            await self.hydrate_settings()

        candidate = dict(self._store.settings)
        candidate.update(settings)

        merged = candidate
        if self._settings_service is not None and self._settings_target is not None:
            try:
                target_exists = await self._settings_service.exists(self._settings_target)
                patch = settings if target_exists else candidate
                merged = await self._settings_service.merge(
                    self._settings_target,
                    patch,
                )
            except Exception:
                logger.exception(
                    "Failed to persist settings for context %s",
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
