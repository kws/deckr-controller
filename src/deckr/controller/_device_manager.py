import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import anyio
from deckr.contracts.messages import (
    RESERVED_BUILTIN_PROVIDER_IDS,
    DeckrMessage,
    parse_host_address,
)
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import messages as hw_messages
from deckr.pluginhost.messages import (
    CLOSE_PAGE,
    HERE_ARE_SETTINGS,
    OPEN_PAGE,
    PAGE_APPEAR,
    PAGE_DISAPPEAR,
    REQUEST_SETTINGS,
    SET_IMAGE,
    SET_PAGE,
    SET_SETTINGS,
    SET_TITLE,
    SHOW_ALERT,
    SHOW_OK,
    SLEEP_SCREEN,
    WAKE_SCREEN,
    DynamicPageDescriptor,
    SettingsBody,
    SlotBinding,
    TitleOptions,
    build_context_id,
    context_subject,
    controller_address,
    host_address,
    make_dynamic_page_id,
    plugin_body_dict,
    plugin_message,
    subject_config_id,
    subject_context_id,
    subject_controller_id,
    subject_slot_id,
)
from deckr.python_plugin.events import PageAppear, PageDisappear
from pydantic import ValidationError

from deckr.controller._binding_validator import (
    BLOCKING_ERROR_CODES,
    format_validation_summary,
    validate_page_bindings,
)
from deckr.controller._command_router import DeviceOutput
from deckr.controller._device_layout import build_device_layout
from deckr.controller._event_translator import EventTranslator
from deckr.controller._hardware_service import HardwareCommandService
from deckr.controller._navigation_service import (
    NavigationService,
    PageTransition,
    StaticPageRef,
)
from deckr.controller._render import RenderModel, RenderService
from deckr.controller._render_dispatcher import (
    RenderBackend,
    RenderDispatcher,
    ThreadRenderBackend,
)
from deckr.controller.config._data import Control, DeviceConfig, Profile
from deckr.controller.plugin.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.plugin.context import ControlContext
from deckr.controller.plugin.provider import PluginManager
from deckr.controller.settings import (
    FileBackedSettingsService,
    SettingsService,
    SettingsTarget,
)

logger = logging.getLogger(__name__)

DEFAULT_WIDGET_TIMEOUT_MS = 60_000


def _title_options_from_payload(payload: object) -> TitleOptions | None:
    if payload is None:
        return None
    return TitleOptions.model_validate(payload)


def _descriptor_from_payload(data: dict) -> DynamicPageDescriptor | None:
    """Validate a dynamic page descriptor from a bus payload."""
    if not data:
        return None
    slots_data = data.get("slots")
    if not slots_data:
        return None
    try:
        return DynamicPageDescriptor.model_validate(data)
    except ValidationError:
        logger.warning("Ignoring invalid dynamic page descriptor payload", exc_info=True)
        return None


def _find_slot(device: hw_messages.HardwareDevice, slot_id: str) -> hw_messages.HardwareSlot | None:
    for slot in device.slots:
        if slot.id == slot_id:
            return slot
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class SlotRef:
    device_id: str
    slot_id: str


@dataclass(slots=True)
class DynamicPageOwner:
    page_id: str
    page_context_id: str
    owner_context_id: str
    owner_slot_id: str
    owner_action_uuid: str
    owner_host_id: str
    owner_profile: str
    owner_page: int
    timeout_ms: int
    last_activity: float
    settings_target: SettingsTarget | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizedCommandTarget:
    sender_host_id: str
    context_id: str
    slot_id: str | None = None
    context: ControlContext | None = None
    dynamic_owner: DynamicPageOwner | None = None


class DeviceManager:
    def __init__(
        self,
        *,
        controller_id: str,
        device: hw_messages.HardwareDevice,
        hardware_ref: hw_messages.HardwareDeviceRef,
        command_service: HardwareCommandService,
        config: DeviceConfig,
        manager: PluginManager,
        plugin_bus: Any,
        start_soon: Callable,
        render_backend: RenderBackend | None = None,
        settings_service: SettingsService | None = None,
        config_stream: AsyncIterator[DeviceConfig | None] | None = None,
        clock: Callable[[], float] | None = None,
        page_timeout_check_interval: float = 0.25,
    ):
        self._controller_id = controller_id
        self.device = device
        self.hardware_ref = hardware_ref
        self.config_id = config.id
        self._command_service = command_service
        self.config = config
        self.manager = manager
        self._plugin_bus = plugin_bus
        self._start_soon = start_soon
        self._config_stream = config_stream
        self._config_listener_task = None
        self._render_backend = render_backend or ThreadRenderBackend()
        self._render_dispatcher = RenderDispatcher(
            command_service=command_service,
            config_id=self.config_id,
            backend=self._render_backend,
            start_soon=start_soon,
        )
        self._settings_service = settings_service or FileBackedSettingsService()
        self.action_contexts = AsyncMap[str, ControlContext]()
        self._translator = EventTranslator(controller_id=controller_id)
        self._nav = NavigationService(config)
        # Keys for dynamic (plugin-defined) pages; add when setting such a page so they are not pruned.
        self._dynamic_persistence_keys: set[str] = set()
        self._dynamic_page_owner: DynamicPageOwner | None = None
        self._clock = clock or time.monotonic
        self._page_timeout_check_interval = page_timeout_check_interval
        self._nav_lock = anyio.Lock()
        self._start_soon(self._page_timeout_loop)

    async def _render_unavailable_to_slot(self, slot: hw_messages.HardwareSlot) -> None:
        """Render 'not available' overlay to a slot (e.g. when action is missing)."""
        if slot.image_format is None:
            return
        model = RenderModel(overlay_type="unavailable")
        render_service = RenderService()
        output = DeviceOutput(self._command_service, self.config_id, slot.id)
        request = render_service.build_request(
            model,
            slot.image_format,
            context_id=build_context_id(self._controller_id, self.config_id, slot.id),
            slot_id=slot.id,
        )
        await self._render_dispatcher.submit_request(
            slot_id=slot.id,
            context_id=build_context_id(self._controller_id, self.config_id, slot.id),
            request=request,
            output=output,
        )

    def _find_profile(self, profile_name: str) -> Profile:
        for profile in self.config.profiles:
            if profile.name == profile_name:
                return profile
        logger.error(f"Profile {profile_name} not found. Returning the first profile.")
        return self.config.profiles[0]

    async def _remove_action(self, slot_id: str):
        ctx = await self.action_contexts.get(slot_id)
        if ctx is not None:
            await ctx.on_will_disappear()
            await self.action_contexts.delete(slot_id)
            await self._render_dispatcher.clear_slot(
                slot_id,
                context_id=build_context_id(
                    self._controller_id, self.config_id, slot_id
                ),
                output=DeviceOutput(self._command_service, self.config_id, slot_id),
            )

    async def _slot_id_for_context(self, context_id: str) -> str | None:
        for slot_id, ctx in await self.action_contexts.items():
            if ctx.id == context_id:
                return slot_id
        return None

    async def _clear_all_image_slots(self) -> None:
        """Clear all image-capable slots before rendering new page. Prevents stale content."""
        layout = build_device_layout(self.device)
        for slot_info in layout.image_grid.slots:
            await self._render_dispatcher.clear_slot(
                slot_info.slot_id,
                context_id=build_context_id(
                    self._controller_id, self.config_id, slot_info.slot_id
                ),
                output=DeviceOutput(
                    self._command_service,
                    self.config_id,
                    slot_info.slot_id,
                ),
            )
        for enc in layout.encoders:
            if enc.image_format is not None:
                await self._render_dispatcher.clear_slot(
                    enc.slot_id,
                    context_id=build_context_id(
                        self._controller_id, self.config_id, enc.slot_id
                    ),
                    output=DeviceOutput(
                        self._command_service,
                        self.config_id,
                        enc.slot_id,
                    ),
                )

    def _build_context_settings_target(
        self,
        *,
        profile_name: str,
        page_index: int,
        control: Control,
        plugin_uuid: str | None = None,
        dynamic_page_uuid: str | None = None,
    ) -> SettingsTarget:
        return SettingsTarget.for_context(
            controller_id=self._controller_id,
            config_id=self.config_id,
            profile_id=profile_name,
            page_id=str(page_index),
            slot_id=control.slot,
            action_uuid=control.action,
            dynamic_page_uuid=dynamic_page_uuid,
            plugin_uuid=plugin_uuid,
        )

    def _build_context_settings_target_for_binding(
        self,
        *,
        profile_id: str,
        page_id: str,
        binding: SlotBinding,
        plugin_uuid: str | None = None,
        dynamic_page_uuid: str | None = None,
    ) -> SettingsTarget:
        return SettingsTarget.for_context(
            controller_id=self._controller_id,
            config_id=self.config_id,
            profile_id=profile_id,
            page_id=page_id,
            slot_id=binding.slot_id,
            action_uuid=binding.action_uuid,
            dynamic_page_uuid=dynamic_page_uuid,
            plugin_uuid=plugin_uuid,
        )

    async def _try_resolve_binding(
        self,
        binding: SlotBinding,
        slot: hw_messages.HardwareSlot,
        *,
        profile_id: str,
        page_id: str,
        seed_config: bool,
        dynamic_page_uuid: str | None = None,
    ) -> bool:
        """Resolve a binding: create ControlContext and call on_will_appear if action available.
        Returns True if context was created, False if action not found (caller should render unavailable).
        """
        action_meta = await self.manager.get_action(binding.action_uuid)
        if action_meta is None:
            logger.info(
                "Binding unresolved on profile=%s page=%s slot=%s action=%s",
                profile_id,
                page_id,
                binding.slot_id,
                binding.action_uuid,
            )
            return False
        settings_target = self._build_context_settings_target_for_binding(
            profile_id=profile_id,
            page_id=page_id,
            binding=binding,
            plugin_uuid=action_meta.plugin_uuid,
            dynamic_page_uuid=dynamic_page_uuid,
        )
        if seed_config:
            target_exists = await self._settings_service.exists(settings_target)
            if not target_exists and binding.settings:
                await self._settings_service.merge(
                    settings_target,
                    dict(binding.settings),
                )
        builtin_action = None
        if action_meta.host_id == BUILTIN_ACTION_PROVIDER_ID and hasattr(
            self.manager, "get_builtin_action"
        ):
            builtin_action = self.manager.get_builtin_action(action_meta.uuid)
        ctx = ControlContext(
            controller_id=self._controller_id,
            device=self.device,
            config_id=self.config_id,
            command_service=self._command_service,
            host_id=action_meta.host_id,
            action_uuid=action_meta.uuid,
            slot=slot,
            settings=binding.settings,
            manager=self,
            plugin_bus=self._plugin_bus,
            start_soon=self._start_soon,
            render_dispatcher=self._render_dispatcher,
            settings_service=self._settings_service,
            context_settings_target=settings_target,
            profile_id=profile_id,
            page_id=page_id,
            title_options=binding.title_options,
            builtin_action=builtin_action,
        )
        await self.action_contexts.set(slot.id, ctx)
        await ctx.on_will_appear()
        logger.info(
            "Binding resolved on profile=%s page=%s slot=%s action=%s host=%s",
            profile_id,
            page_id,
            binding.slot_id,
            binding.action_uuid,
            action_meta.host_id,
        )
        return True

    def _build_valid_settings_keys(self) -> set[str]:
        """Keys that are currently valid: config bindings plus active dynamic pages."""

        valid_keys: set[str] = set(self._dynamic_persistence_keys)
        for profile in self.config.profiles:
            for page_index, page in enumerate(profile.pages):
                for control in page.controls:
                    key = self._build_context_settings_target(
                        profile_name=profile.name,
                        page_index=page_index,
                        control=control,
                    )
                    valid_keys.add(key.as_key())
        return valid_keys

    async def _reconcile_persistence(self) -> None:
        """Prune stale persisted context settings for this config."""

        prune = getattr(self._settings_service, "prune_context_targets", None)
        if not callable(prune):
            return
        valid_keys = self._build_valid_settings_keys()
        pruned = await prune(
            controller_id=self._controller_id,
            config_id=self.config_id,
            valid_keys=valid_keys,
        )
        if pruned > 0:
            logger.info(
                "Pruned %d stale settings records for %s", pruned, self.config_id
            )

    def _resolve_widget_timeout_ms(self, profile_name: str, page_index: int) -> int:
        profile = self._find_profile(profile_name)
        timeout_ms: int | None = None
        if 0 <= page_index < len(profile.pages):
            timeout_ms = profile.pages[page_index].widget_timeout_ms
        if timeout_ms is None:
            timeout_ms = profile.widget_timeout_ms
        if timeout_ms is None:
            timeout_ms = DEFAULT_WIDGET_TIMEOUT_MS
        return max(0, int(timeout_ms))

    def _record_page_activity(self) -> None:
        owner = self._dynamic_page_owner
        if owner is not None:
            owner.last_activity = self._clock()

    async def _page_timeout_loop(self) -> None:
        while True:
            await anyio.sleep(self._page_timeout_check_interval)
            owner = self._dynamic_page_owner
            if owner is None:
                continue
            if owner.timeout_ms <= 0:
                continue
            elapsed_ms = int((self._clock() - owner.last_activity) * 1000)
            if elapsed_ms >= owner.timeout_ms:
                await self.close_page(
                    context_id=owner.page_context_id, reason="timeout"
                )

    async def _emit_page_appear(
        self,
        owner: DynamicPageOwner,
        *,
        causation_id: str | None = None,
    ) -> None:
        if owner.owner_host_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        settings = (
            await self._settings_service.get(owner.settings_target)
            if owner.settings_target is not None
            else {}
        )
        event = PageAppear(
            context=owner.page_context_id,
            page_id=owner.page_id,
            timeout_ms=owner.timeout_ms,
        )
        msg = plugin_message(
            sender=controller_address(self._controller_id),
            recipient=host_address(owner.owner_host_id),
            message_type=PAGE_APPEAR,
            body={
                "settings": settings,
                "event": event.model_dump(
                    by_alias=True,
                    exclude={"context"},
                ),
            },
            subject=context_subject(
                owner.page_context_id,
                action_uuid=owner.owner_action_uuid,
            ),
            causation_id=causation_id,
        )
        await self._plugin_bus.send(msg)

    async def _emit_page_disappear(
        self,
        owner: DynamicPageOwner,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> None:
        if owner.owner_host_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        event = PageDisappear(
            context=owner.page_context_id,
            page_id=owner.page_id,
            reason=reason,
        )
        msg = plugin_message(
            sender=controller_address(self._controller_id),
            recipient=host_address(owner.owner_host_id),
            message_type=PAGE_DISAPPEAR,
            body={
                "event": event.model_dump(
                    by_alias=True,
                    exclude={"context"},
                ),
            },
            subject=context_subject(
                owner.page_context_id,
                action_uuid=owner.owner_action_uuid,
            ),
            causation_id=causation_id,
        )
        await self._plugin_bus.send(msg)

    async def _finalize_dynamic_page(
        self,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> None:
        owner = self._dynamic_page_owner
        if owner is None:
            return
        await self._emit_page_disappear(
            owner,
            reason,
            causation_id=causation_id,
        )
        self._dynamic_page_owner = None

    async def _execute_transition(
        self, transition: PageTransition, *, config_changed: bool = False
    ) -> bool:
        arriving = transition.arriving
        seed_config = config_changed or transition.departing is None

        if isinstance(arriving, StaticPageRef):
            bindings = self._nav.resolve_static_bindings(arriving)
            result = await validate_page_bindings(
                bindings,
                self.device,
                self.manager.get_action,
                profile_id=arriving.profile_name,
                page_id=str(arriving.page_index),
            )
            if result.has_blocking_errors:
                logger.error(
                    "Page transition rejected (capability validation): %s",
                    format_validation_summary(result),
                )
                for err in result.errors:
                    if err.code in BLOCKING_ERROR_CODES:
                        logger.error(
                            "Binding validation failed [%s]: %s (slot=%s action=%s) %s",
                            err.code,
                            err.message,
                            err.slot_id,
                            err.action_uuid,
                            err.details,
                        )
                if transition.departing is not None:
                    self._nav.set_page(transition.departing)
                return False
            for err in result.errors:
                if err.code not in BLOCKING_ERROR_CODES:
                    logger.warning(
                        "Action unavailable (slot will show 'not available'): %s (slot=%s action=%s)",
                        err.message,
                        err.slot_id,
                        err.action_uuid,
                    )
        elif isinstance(arriving, DynamicPageDescriptor):
            result = await validate_page_bindings(
                arriving.slots,
                self.device,
                self.manager.get_action,
                profile_id="_dynamic",
                page_id=arriving.page_id,
            )
            if result.has_blocking_errors:
                logger.error(
                    "Dynamic page transition rejected (capability validation): %s",
                    format_validation_summary(result),
                )
                for err in result.errors:
                    if err.code in BLOCKING_ERROR_CODES:
                        logger.error(
                            "Dynamic page binding validation failed [%s]: %s (slot=%s action=%s) %s",
                            err.code,
                            err.message,
                            err.slot_id,
                            err.action_uuid,
                            err.details,
                        )
                if transition.departing is not None:
                    self._nav.set_page(transition.departing)
                return False
            for err in result.errors:
                if err.code not in BLOCKING_ERROR_CODES:
                    logger.warning(
                        "Action unavailable (slot will show 'not available'): %s (slot=%s action=%s)",
                        err.message,
                        err.slot_id,
                        err.action_uuid,
                    )

        if transition.departing is not None:
            for ctx in await self.action_contexts.values():
                await ctx.on_will_disappear()
            await self.action_contexts.clear()

        await self._clear_all_image_slots()

        if isinstance(arriving, StaticPageRef):
            bindings = self._nav.resolve_static_bindings(arriving)
            for binding in bindings:
                slot = _find_slot(self.device, binding.slot_id)
                if slot is None:
                    continue
                if not await self._try_resolve_binding(
                    binding,
                    slot,
                    profile_id=arriving.profile_name,
                    page_id=str(arriving.page_index),
                    seed_config=seed_config,
                ):
                    await self._render_unavailable_to_slot(slot)
        elif isinstance(arriving, DynamicPageDescriptor):
            for b in arriving.slots:
                self._dynamic_persistence_keys.add(
                    self._build_context_settings_target_for_binding(
                        profile_id="_dynamic",
                        page_id=arriving.page_id,
                        binding=b,
                        dynamic_page_uuid=arriving.page_id,
                    ).as_key()
                )
            for binding in arriving.slots:
                slot = _find_slot(self.device, binding.slot_id)
                if slot is None:
                    logger.error(
                        "Slot %s not found on device %s",
                        binding.slot_id,
                        self.config_id,
                    )
                    continue
                if not await self._try_resolve_binding(
                    binding,
                    slot,
                    profile_id="_dynamic",
                    page_id=arriving.page_id,
                    seed_config=seed_config,
                    dynamic_page_uuid=arriving.page_id,
                ):
                    await self._render_unavailable_to_slot(slot)
        return True

    async def _set_page_locked(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageDescriptor | None = None,
        close_dynamic: bool = True,
        close_reason: str = "navigate",
        causation_id: str | None = None,
    ) -> bool:
        """Navigate to a static page (profile, page) or dynamic page (descriptor). Caller must hold _nav_lock."""
        await self._reconcile_persistence()
        owner_to_close = self._dynamic_page_owner if close_dynamic else None
        if descriptor is not None:
            transition = self._nav.set_page(descriptor)
        else:
            profile_name = profile or "default"
            page_index = page if page is not None else 0
            profile_obj = self._find_profile(profile_name)
            transition = self._nav.set_page(
                StaticPageRef(profile_name=profile_obj.name, page_index=page_index)
            )
        ok = await self._execute_transition(transition)
        if ok and owner_to_close is not None:
            await self._finalize_dynamic_page(
                close_reason,
                causation_id=causation_id,
            )
        return ok

    async def set_page(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageDescriptor | None = None,
        causation_id: str | None = None,
    ) -> bool:
        """Navigate to a static page (profile, page) or dynamic page (descriptor)."""
        async with self._nav_lock:
            return await self._set_page_locked(
                profile=profile,
                page=page,
                descriptor=descriptor,
                close_dynamic=True,
                close_reason="navigate",
                causation_id=causation_id,
            )

    async def open_page(
        self,
        *,
        descriptor: DynamicPageDescriptor,
        context_id: str,
        causation_id: str | None = None,
    ) -> None:
        """Open a widget-owned dynamic page anchored to the owner's profile page."""
        if not descriptor or not descriptor.slots:
            return

        async with self._nav_lock:
            owner = self._dynamic_page_owner

            async def _replace_dynamic_page(current_owner: DynamicPageOwner) -> None:
                old_page_id = current_owner.page_id
                page_id = descriptor.page_id or make_dynamic_page_id()
                replacement = DynamicPageDescriptor(
                    page_id=page_id, slots=descriptor.slots
                )
                ok = await self._set_page_locked(
                    descriptor=replacement,
                    close_dynamic=False,
                )
                if ok:
                    await self._emit_page_disappear(
                        DynamicPageOwner(
                            page_id=old_page_id,
                            page_context_id=current_owner.page_context_id,
                            owner_context_id=current_owner.owner_context_id,
                            owner_slot_id=current_owner.owner_slot_id,
                            owner_action_uuid=current_owner.owner_action_uuid,
                            owner_host_id=current_owner.owner_host_id,
                            owner_profile=current_owner.owner_profile,
                            owner_page=current_owner.owner_page,
                            timeout_ms=current_owner.timeout_ms,
                            last_activity=current_owner.last_activity,
                            settings_target=current_owner.settings_target,
                        ),
                        reason="replaced",
                        causation_id=causation_id,
                    )
                    current_owner.page_id = page_id
                    current_owner.last_activity = self._clock()
                    await self._emit_page_appear(
                        current_owner,
                        causation_id=causation_id,
                    )

            if owner is not None and context_id in {
                owner.page_context_id,
                owner.owner_context_id,
            }:
                # Replace current dynamic page within the same widget session.
                await _replace_dynamic_page(owner)
                return

            slot_id = await self._slot_id_for_context(context_id)
            if slot_id is None:
                logger.warning("open_page ignored: no active context for %s", context_id)
                return
            ctx = await self.action_contexts.get(slot_id)
            if ctx is None:
                logger.warning("open_page ignored: no active context for %s", slot_id)
                return
            if ctx.profile_id == "_dynamic":
                if (
                    owner is not None
                    and owner.owner_action_uuid == ctx.action_uuid
                    and owner.owner_host_id == ctx.host_id
                ):
                    await _replace_dynamic_page(owner)
                    return
                logger.warning(
                    "open_page rejected for dynamic page context %s", slot_id
                )
                return

            try:
                owner_page = int(ctx.page_id)
            except ValueError:
                owner_page = 0

            timeout_ms = self._resolve_widget_timeout_ms(ctx.profile_id, owner_page)
            page_id = descriptor.page_id or make_dynamic_page_id()
            page_context_id = build_context_id(
                self._controller_id, self.config_id, f"page:{make_dynamic_page_id()}"
            )
            descriptor = DynamicPageDescriptor(page_id=page_id, slots=descriptor.slots)

            new_owner = DynamicPageOwner(
                page_id=page_id,
                page_context_id=page_context_id,
                owner_context_id=context_id,
                owner_slot_id=slot_id,
                owner_action_uuid=ctx.action_uuid,
                owner_host_id=ctx.host_id,
                owner_profile=ctx.profile_id,
                owner_page=owner_page,
                timeout_ms=timeout_ms,
                last_activity=self._clock(),
                settings_target=ctx.settings_target,
            )

            ok = await self._set_page_locked(
                descriptor=descriptor,
                close_dynamic=False,
            )
            if ok:
                if owner is not None:
                    await self._emit_page_disappear(
                        owner,
                        reason="replaced",
                        causation_id=causation_id,
                    )
                self._dynamic_page_owner = new_owner
                await self._emit_page_appear(new_owner, causation_id=causation_id)

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None:
        """Close the active widget page and return to its owner profile page."""
        async with self._nav_lock:
            owner = self._dynamic_page_owner
            if owner is None:
                logger.info("No owner for dynamic page")
                return
            await self._emit_page_disappear(
                owner,
                reason=reason,
                causation_id=causation_id,
            )
            self._dynamic_page_owner = None
            await self._set_page_locked(
                profile=owner.owner_profile,
                page=owner.owner_page,
                close_dynamic=False,
            )

    async def clear_page(self):
        async with self._nav_lock:
            await self._finalize_dynamic_page(reason="clear")
            for ctx in await self.action_contexts.values():
                await ctx.on_will_disappear()
            await self.action_contexts.clear()
            await self._clear_all_image_slots()

    async def on_actions_changed(
        self, registered: list[str], unregistered: list[str]
    ) -> None:
        """Re-resolve bindings when actions become available or unavailable.

        registered/unregistered carry qualified IDs (host_id::action_uuid).
        """
        unregistered_set = frozenset(unregistered)
        registered_set = frozenset(registered)

        # Handle unregistered first (order matters for re-register scenario)
        to_remove: list[tuple[str, ControlContext]] = []
        to_reappear: list[ControlContext] = []
        for slot_id, ctx in await self.action_contexts.items():
            ctx_qualified = f"{ctx.host_id}::{ctx.action_uuid}"
            if ctx_qualified in unregistered_set:
                to_remove.append((slot_id, ctx))
                continue
            if ctx_qualified in registered_set:
                to_reappear.append(ctx)
        for slot_id, ctx in to_remove:
            await ctx.on_will_disappear()
            await self.action_contexts.delete(slot_id)
            await self._render_unavailable_to_slot(ctx.slot)
        for ctx in to_reappear:
            await ctx.on_will_appear()

        # Handle registered: try to resolve bindings that were previously unavailable
        current_page = self._nav.current_page
        if current_page is None:
            return

        if isinstance(current_page, StaticPageRef):
            bindings = self._nav.resolve_static_bindings(current_page)
            profile_id = current_page.profile_name
            page_id = str(current_page.page_index)
            dynamic_page_uuid = None
        else:
            bindings = current_page.slots
            profile_id = "_dynamic"
            page_id = current_page.page_id
            dynamic_page_uuid = current_page.page_id

        logger.info(
            "Re-evaluating page bindings for config=%s page=%s after actions change +%s -%s",
            self.config_id,
            page_id,
            registered,
            unregistered,
        )

        for binding in bindings:
            if await self.action_contexts.has_key(binding.slot_id):
                continue  # Already has context
            slot = _find_slot(self.device, binding.slot_id)
            if slot is None:
                continue
            await self._try_resolve_binding(
                binding,
                slot,
                profile_id=profile_id,
                page_id=page_id,
                seed_config=False,
                dynamic_page_uuid=dynamic_page_uuid,
            )

    async def _config_listener(self) -> None:
        """Consume config stream and apply changes."""
        if self._config_stream is None:
            return
        async for config in self._config_stream:
            await self._on_config_changed(config)

    async def _on_config_changed(self, config: DeviceConfig | None) -> None:
        """Handle config update or removal."""
        if config is None:
            await self.clear_page()
            return
        async with self._nav_lock:
            self.config = config
            if self._dynamic_page_owner is not None:
                await self._finalize_dynamic_page(reason="config_change")
            transition = self._nav.update_config(config)
            await self._execute_transition(transition, config_changed=True)

    def _command_sender_host_id(self, msg: DeckrMessage) -> str | None:
        host_id = parse_host_address(msg.sender)
        if host_id is None:
            logger.warning(
                "Ignoring plugin command %s from non-host sender %s",
                msg.message_type,
                msg.sender,
            )
            return None
        if host_id in RESERVED_BUILTIN_PROVIDER_IDS:
            logger.warning(
                "Ignoring plugin command %s from route-owned host using reserved provider id %s",
                msg.message_type,
                host_id,
            )
            return None
        return host_id

    async def _authorize_plugin_command(
        self,
        msg: DeckrMessage,
        *,
        context_id: str,
    ) -> AuthorizedCommandTarget | None:
        sender_host_id = self._command_sender_host_id(msg)
        if sender_host_id is None:
            return None

        subject_slot = subject_slot_id(msg.subject)
        owner = self._dynamic_page_owner
        if owner is not None and context_id in {
            owner.page_context_id,
            owner.owner_context_id,
        }:
            if sender_host_id != owner.owner_host_id:
                logger.warning(
                    "Ignoring plugin command %s from host %s for dynamic page owned by host %s",
                    msg.message_type,
                    sender_host_id,
                    owner.owner_host_id,
                )
                return None
            return AuthorizedCommandTarget(
                sender_host_id=sender_host_id,
                context_id=context_id,
                dynamic_owner=owner,
            )

        if msg.message_type == CLOSE_PAGE and owner is not None:
            logger.warning(
                "Ignoring closePage from host %s for non-owner context %s",
                sender_host_id,
                context_id,
            )
            return None

        if subject_slot is None:
            logger.warning(
                "Ignoring plugin command %s from %s without slot in context subject",
                msg.message_type,
                msg.sender,
            )
            return None

        ctx = await self.action_contexts.get(subject_slot)
        if ctx is None or ctx.id != context_id:
            logger.warning(
                "Ignoring plugin command %s from %s for inactive context %s",
                msg.message_type,
                msg.sender,
                context_id,
            )
            return None

        if sender_host_id != ctx.host_id:
            logger.warning(
                "Ignoring plugin command %s from host %s for context %s owned by host %s",
                msg.message_type,
                sender_host_id,
                context_id,
                ctx.host_id,
            )
            return None

        return AuthorizedCommandTarget(
            sender_host_id=sender_host_id,
            context_id=context_id,
            slot_id=subject_slot,
            context=ctx,
        )

    async def handle_command(self, msg: DeckrMessage) -> None:
        """Handle a command message from a plugin host (setTitle, setImage, etc.)."""
        payload = plugin_body_dict(msg)
        context_id = subject_context_id(msg.subject) or ""
        if not context_id:
            return
        context_controller_id = subject_controller_id(msg.subject)
        if (
            context_controller_id is not None
            and context_controller_id != self._controller_id
        ):
            return
        config_id = subject_config_id(msg.subject)
        if config_id != self.config_id:
            return
        msg_type = msg.message_type
        authorization = await self._authorize_plugin_command(
            msg,
            context_id=context_id,
        )
        if authorization is None:
            return

        async def send_settings_response(settings: dict) -> None:
            await self._plugin_bus.reply_to(
                msg,
                sender=controller_address(self._controller_id),
                message_type=HERE_ARE_SETTINGS,
                body=SettingsBody(settings=settings).to_dict(),
                subject=context_subject(context_id),
            )

        if msg_type == OPEN_PAGE:
            desc_data = payload.get("descriptor")
            descriptor = _descriptor_from_payload(desc_data) if desc_data else None
            if descriptor is not None:
                await self.open_page(
                    descriptor=descriptor,
                    context_id=context_id,
                    causation_id=msg.message_id,
                )
            return

        if msg_type == CLOSE_PAGE:
            await self.close_page(
                context_id=context_id,
                reason="close",
                causation_id=msg.message_id,
            )
            return

        owner = authorization.dynamic_owner
        if owner is not None and context_id == owner.page_context_id:
            if msg_type == SET_SETTINGS:
                target = owner.settings_target
                new_settings = (
                    await self._settings_service.merge(
                        target,
                        dict(payload.get("settings", {})),
                    )
                    if target is not None
                    else {}
                )
                await send_settings_response(new_settings)
            elif msg_type == REQUEST_SETTINGS:
                current_settings = (
                    await self._settings_service.get(owner.settings_target)
                    if owner.settings_target is not None
                    else {}
                )
                await send_settings_response(current_settings)
            return

        slot_id = authorization.slot_id
        ctx = authorization.context
        if not slot_id or ctx is None:
            return

        router = ctx._router

        if msg_type == SET_TITLE:
            await router.set_title(
                payload.get("text", ""),
                title_options=_title_options_from_payload(payload.get("titleOptions")),
            )
        elif msg_type == SET_IMAGE:
            await router.set_image(payload.get("image", ""))
        elif msg_type == SHOW_ALERT:
            await router.show_alert()
        elif msg_type == SHOW_OK:
            await router.show_ok()
        elif msg_type == SET_SETTINGS:
            await router.set_settings(payload.get("settings", {}))
            settings = await router.get_settings()
            await send_settings_response(vars(settings))
        elif msg_type == REQUEST_SETTINGS:
            settings = await router.get_settings()
            await send_settings_response(vars(settings))
        elif msg_type == SET_PAGE:
            await self.set_page(
                profile=payload.get("profile", "default"),
                page=payload.get("page", 0),
                causation_id=msg.message_id,
            )
        elif msg_type == SLEEP_SCREEN:
            await self._command_service.sleep_screen(self.config_id)
        elif msg_type == WAKE_SCREEN:
            await self._command_service.wake_screen(self.config_id)

    async def on_event(self, message: DeckrMessage):
        event = hw_messages.hardware_body_from_message(message)
        translated = self._translator.translate(event, self.config_id)
        if translated is None:
            return
        if self._dynamic_page_owner is not None:
            self._record_page_activity()

        slot_id = translated.slot_id
        action_ctx = await self.action_contexts.get(slot_id)
        if action_ctx is None:
            return

        method_name = translated.method_name
        try:
            await getattr(action_ctx, method_name)(translated.plugin_event)
        except Exception as e:
            logger.error(
                "Error calling %s on action %s: %s",
                method_name,
                action_ctx.action_uuid,
                e,
                exc_info=True,
            )
