"""Command routing: plugin commands → store update → resolve → enqueue render."""

import logging
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

import anyio

from deckr.controller._persistence import ControllerPersistence, PersistenceKey
from deckr.controller._render import RenderService, resolve
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import (
    ControlStateStore,
    StateOverride,
    TransientOverlay,
)

if TYPE_CHECKING:
    from deckr.hardware.events import HWDevice, HWSImageFormat

logger = logging.getLogger(__name__)


class DeviceOutput:
    """Thin wrapper: writes bytes to device; records last frame per slot."""

    def __init__(self, device: "HWDevice", slot_id: str):
        self._device = device
        self._slot_id = slot_id
        self.last_frame: bytes | None = None

    @property
    def slot_id(self) -> str:
        return self._slot_id

    async def write(self, frame: bytes) -> None:
        await self._device.set_image(self._slot_id, frame)
        self.last_frame = frame

    async def clear(self) -> None:
        await self._device.clear_slot(self._slot_id)
        self.last_frame = None


class CommandRouter:
    """Receives plugin commands, updates ControlStateStore, triggers resolve → encode → write."""

    def __init__(
        self,
        store: ControlStateStore,
        render_service: RenderService,
        render_dispatcher: RenderDispatcher,
        output: DeviceOutput,
        image_format: "HWSImageFormat | None",
        start_soon: Callable,
        *,
        persistence: ControllerPersistence | None = None,
        persistence_key: PersistenceKey | None = None,
        manifest_defaults: dict[int, StateOverride] | None = None,
    ):
        self._store = store
        self._render_service = render_service
        self._render_dispatcher = render_dispatcher
        self._output = output
        self._image_format = image_format
        self._start_soon = start_soon
        self._overlay_token: int = 0
        self._persistence = persistence
        self._persistence_key = persistence_key
        self._manifest_defaults = manifest_defaults
        self._settings_hydrated = False

    def _ensure_override(self, state_index: int) -> StateOverride:
        if state_index not in self._store.overrides:
            self._store.overrides[state_index] = StateOverride()
        return self._store.overrides[state_index]

    async def _render(self) -> None:
        if self._image_format is None:
            return
        model = resolve(self._store, manifest_defaults=self._manifest_defaults)
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

    async def set_title(self, text: str, state: int | None = None) -> None:
        target = state if state is not None else self._store.state_index
        override = self._ensure_override(target)
        override.title = text
        override.image = None
        await self._render()

    async def set_image(self, image: str, state: int | None = None) -> None:
        target = state if state is not None else self._store.state_index
        override = self._ensure_override(target)
        override.image = image
        override.title = None
        await self._render()

    async def set_state(self, state: int) -> None:
        self._store.state_index = state
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

        if self._persistence is not None and self._persistence_key is not None:
            persisted = self._persistence.get_settings(self._persistence_key)
            if persisted is None:
                # Legacy fallback: prior versions stored settings by runtime context only.
                legacy = self._persistence.get_value(self._store.context_id)
                if isinstance(legacy, dict):
                    self._persistence.set_settings(self._persistence_key, legacy)
                    self._persistence.delete_value(self._store.context_id)
                    persisted = legacy

            if persisted is not None:
                # Config defaults (already in _store.settings) then overlay persisted; last wins.
                merged = dict(self._store.settings)
                merged.update(persisted)
                self._store.settings = merged

        self._settings_hydrated = True

    async def set_settings(self, settings: dict) -> None:
        """Merge settings and persist. Fail-fast: on persistence write failure we do not update in-memory store."""
        candidate = dict(self._store.settings)
        candidate.update(settings)

        if self._persistence is not None and self._persistence_key is not None:
            try:
                self._persistence.set_settings(self._persistence_key, candidate)
            except Exception:
                logger.exception(
                    "Failed to persist settings for context %s",
                    self._store.context_id,
                )
                raise

        self._store.settings = candidate
        self._settings_hydrated = True

    async def get_settings(self) -> SimpleNamespace:
        if not self._settings_hydrated:
            await self.hydrate_settings()
        return SimpleNamespace(**self._store.settings)
