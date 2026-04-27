import logging
import time
import uuid
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
    REPLACE_PAGE,
    REQUEST_SETTINGS,
    SET_IMAGE,
    SET_PAGE,
    SET_SETTINGS,
    SET_TITLE,
    SHOW_ALERT,
    SHOW_OK,
    SLEEP_SCREEN,
    UPDATE_PAGE,
    WAKE_SCREEN,
    ControlBindingDescriptor,
    DynamicPageDescriptor,
    SettingsBody,
    TitleOptions,
    context_subject,
    controller_address,
    host_address,
    make_binding_id,
    make_context_id,
    make_dynamic_page_id,
    make_page_session_id,
    plugin_body_dict,
    plugin_message,
    subject_action_instance_id,
    subject_binding_id,
    subject_config_id,
    subject_context_id,
    subject_page_session_id,
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
from deckr.controller.config._data import DeviceConfig, Profile
from deckr.controller.plugin.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.plugin.context import ControlContext
from deckr.controller.plugin.provider import PluginManager
from deckr.controller.settings import (
    InMemorySettingsService,
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
    bindings_data = data.get("bindings")
    if not bindings_data:
        return None
    try:
        return DynamicPageDescriptor.model_validate(data)
    except ValidationError:
        logger.warning("Ignoring invalid dynamic page descriptor payload", exc_info=True)
        return None


def _find_slot(
    device: hw_messages.HardwareDevice, slot_id: str
) -> hw_messages.HardwareSlot | None:
    for slot in device.slots:
        if slot.id == slot_id:
            return slot
    return None


_ACTION_INSTANCE_NAMESPACE = uuid.UUID("dcd72f2a-65cb-4d9f-b0e8-4e0ef3d334f1")


def _action_instance_id(
    *,
    controller_id: str,
    config_id: str,
    profile_id: str,
    page_id: str,
    control_id: str,
    action_uuid: str,
) -> str:
    seed = "\x1f".join(
        (controller_id, config_id, profile_id, page_id, control_id, action_uuid)
    )
    return str(uuid.uuid5(_ACTION_INSTANCE_NAMESPACE, seed))


@dataclass(slots=True)
class DynamicPageSession:
    page_id: str
    page_session_id: str
    context_id: str
    action_instance_id: str
    owner_context_id: str
    owner_binding_id: str
    owner_control_id: str
    owner_action_uuid: str
    owner_host_id: str
    owner_profile: str
    owner_page: int
    timeout_ms: int
    last_activity: float
    settings_target: SettingsTarget | None


@dataclass(slots=True)
class BindingLease:
    binding_id: str
    context_id: str
    action_instance_id: str
    action_uuid: str
    host_id: str
    control_id: str
    slot: hw_messages.HardwareSlot
    profile_id: str
    page_id: str
    settings_target: SettingsTarget | None
    context: ControlContext
    page_session_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizedCommandTarget:
    sender_host_id: str
    context_id: str
    binding: BindingLease | None = None
    page_session: DynamicPageSession | None = None


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
        self._settings_service = settings_service or InMemorySettingsService()
        self.action_contexts = AsyncMap[str, ControlContext]()
        self._translator = EventTranslator(controller_id=controller_id)
        self._nav = NavigationService(config)
        self._dynamic_page_session: DynamicPageSession | None = None
        self._binding_leases: dict[str, BindingLease] = {}
        self._binding_by_context: dict[str, str] = {}
        self._active_binding_by_control: dict[str, str] = {}
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
        context_id = make_context_id()
        request = render_service.build_request(
            model,
            slot.image_format,
            context_id=context_id,
            slot_id=slot.id,
        )
        await self._render_dispatcher.submit_request(
            slot_id=slot.id,
            context_id=context_id,
            request=request,
            output=output,
        )

    def _find_profile(self, profile_name: str) -> Profile:
        for profile in self.config.profiles:
            if profile.name == profile_name:
                return profile
        logger.error(f"Profile {profile_name} not found. Returning the first profile.")
        return self.config.profiles[0]

    async def _revoke_binding(
        self,
        binding_id: str,
        *,
        clear_output: bool = True,
    ) -> BindingLease | None:
        lease = self._binding_leases.pop(binding_id, None)
        if lease is None:
            return None
        self._binding_by_context.pop(lease.context_id, None)
        active_binding = self._active_binding_by_control.get(lease.control_id)
        if active_binding == binding_id:
            self._active_binding_by_control.pop(lease.control_id, None)
            await self.action_contexts.delete(lease.control_id)
        await lease.context.on_will_disappear()
        await self._render_dispatcher.clear_slot(
            lease.control_id,
            context_id=lease.context_id,
            binding_id=lease.binding_id,
            output=DeviceOutput(self._command_service, self.config_id, lease.control_id),
            clear_output=clear_output,
        )
        return lease

    async def _revoke_active_bindings(self, *, clear_outputs: bool = True) -> None:
        for binding_id in list(self._binding_leases):
            await self._revoke_binding(binding_id, clear_output=clear_outputs)

    async def _clear_all_image_slots(self) -> None:
        """Clear all image-capable slots before rendering new page. Prevents stale content."""
        layout = build_device_layout(self.device)
        for slot_info in layout.image_grid.slots:
            await self._render_dispatcher.clear_slot(
                slot_info.slot_id,
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
                    output=DeviceOutput(
                        self._command_service,
                        self.config_id,
                        enc.slot_id,
                    ),
                )

    def _build_context_settings_target_for_binding(
        self,
        *,
        profile_id: str,
        page_id: str,
        binding: ControlBindingDescriptor,
        plugin_uuid: str | None = None,
    ) -> SettingsTarget:
        return SettingsTarget.for_context(
            controller_id=self._controller_id,
            config_id=self.config_id,
            profile_id=profile_id,
            page_id=page_id,
            slot_id=binding.control_id,
            action_uuid=binding.action_uuid,
            plugin_uuid=plugin_uuid,
        )

    async def _try_resolve_binding(
        self,
        binding: ControlBindingDescriptor,
        slot: hw_messages.HardwareSlot,
        *,
        profile_id: str,
        page_id: str,
        action_instance_id: str,
        page_session_id: str | None = None,
        persist_settings: bool = True,
    ) -> bool:
        """Resolve a binding: create ControlContext and call on_will_appear if action available.
        Returns True if context was created, False if action not found (caller should render unavailable).
        """
        action_meta = await self.manager.get_action(binding.action_uuid)
        if action_meta is None:
            logger.info(
                "Binding unresolved on profile=%s page=%s control=%s action=%s",
                profile_id,
                page_id,
                binding.control_id,
                binding.action_uuid,
            )
            return False
        settings_target = (
            self._build_context_settings_target_for_binding(
                profile_id=profile_id,
                page_id=page_id,
                binding=binding,
                plugin_uuid=action_meta.plugin_uuid,
            )
            if persist_settings
            else None
        )
        builtin_action = None
        if action_meta.host_id == BUILTIN_ACTION_PROVIDER_ID and hasattr(
            self.manager, "get_builtin_action"
        ):
            builtin_action = self.manager.get_builtin_action(action_meta.uuid)
        binding_id = make_binding_id()
        context_id = make_context_id()
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
            action_instance_id=action_instance_id,
            binding_id=binding_id,
            context_id=context_id,
            page_session_id=page_session_id,
        )
        lease = BindingLease(
            binding_id=binding_id,
            context_id=context_id,
            action_instance_id=action_instance_id,
            action_uuid=action_meta.uuid,
            host_id=action_meta.host_id,
            control_id=slot.id,
            slot=slot,
            profile_id=profile_id,
            page_id=page_id,
            settings_target=settings_target,
            context=ctx,
            page_session_id=page_session_id,
        )
        self._binding_leases[binding_id] = lease
        self._binding_by_context[context_id] = binding_id
        self._active_binding_by_control[slot.id] = binding_id
        await self.action_contexts.set(slot.id, ctx)
        await ctx.on_will_appear()
        logger.info(
            "Binding resolved on profile=%s page=%s control=%s action=%s host=%s binding=%s",
            profile_id,
            page_id,
            binding.control_id,
            binding.action_uuid,
            action_meta.host_id,
            binding_id,
        )
        return True

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
        session = self._dynamic_page_session
        if session is not None:
            session.last_activity = self._clock()

    async def _page_timeout_loop(self) -> None:
        while True:
            await anyio.sleep(self._page_timeout_check_interval)
            session = self._dynamic_page_session
            if session is None:
                continue
            if session.timeout_ms <= 0:
                continue
            elapsed_ms = int((self._clock() - session.last_activity) * 1000)
            if elapsed_ms >= session.timeout_ms:
                await self.close_page(
                    context_id=session.context_id, reason="timeout"
                )

    async def _emit_page_appear(
        self,
        session: DynamicPageSession,
        *,
        causation_id: str | None = None,
    ) -> None:
        if session.owner_host_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        settings = (
            await self._settings_service.get(session.settings_target)
            if session.settings_target is not None
            else {}
        )
        event = PageAppear(
            context=session.context_id,
            page_id=session.page_id,
            timeout_ms=session.timeout_ms,
        )
        msg = plugin_message(
            sender=controller_address(self._controller_id),
            recipient=host_address(session.owner_host_id),
            message_type=PAGE_APPEAR,
            body={
                "settings": settings,
                "event": event.model_dump(
                    by_alias=True,
                    exclude={"context"},
                ),
            },
            subject=context_subject(
                session.context_id,
                config_id=self.config_id,
                action_instance_id=session.action_instance_id,
                page_session_id=session.page_session_id,
                action_uuid=session.owner_action_uuid,
            ),
            causation_id=causation_id,
        )
        await self._plugin_bus.send(msg)

    async def _emit_page_disappear(
        self,
        session: DynamicPageSession,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> None:
        if session.owner_host_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        event = PageDisappear(
            context=session.context_id,
            page_id=session.page_id,
            reason=reason,
        )
        msg = plugin_message(
            sender=controller_address(self._controller_id),
            recipient=host_address(session.owner_host_id),
            message_type=PAGE_DISAPPEAR,
            body={
                "event": event.model_dump(
                    by_alias=True,
                    exclude={"context"},
                ),
            },
            subject=context_subject(
                session.context_id,
                config_id=self.config_id,
                action_instance_id=session.action_instance_id,
                page_session_id=session.page_session_id,
                action_uuid=session.owner_action_uuid,
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
        session = self._dynamic_page_session
        if session is None:
            return
        await self._emit_page_disappear(
            session,
            reason,
            causation_id=causation_id,
        )
        self._dynamic_page_session = None

    async def _execute_transition(
        self,
        transition: PageTransition,
        *,
        page_session: DynamicPageSession | None = None,
    ) -> bool:
        arriving = transition.arriving

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
                list(arriving.bindings),
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
            await self._revoke_active_bindings()

        await self._clear_all_image_slots()

        if isinstance(arriving, StaticPageRef):
            bindings = self._nav.resolve_static_bindings(arriving)
            for binding in bindings:
                slot = _find_slot(self.device, binding.control_id)
                if slot is None:
                    continue
                action_instance_id = _action_instance_id(
                    controller_id=self._controller_id,
                    config_id=self.config_id,
                    profile_id=arriving.profile_name,
                    page_id=str(arriving.page_index),
                    control_id=binding.control_id,
                    action_uuid=binding.action_uuid,
                )
                if not await self._try_resolve_binding(
                    binding,
                    slot,
                    profile_id=arriving.profile_name,
                    page_id=str(arriving.page_index),
                    action_instance_id=action_instance_id,
                ):
                    await self._render_unavailable_to_slot(slot)
        elif isinstance(arriving, DynamicPageDescriptor):
            if page_session is None:
                logger.error("Dynamic page transition missing page session")
                return False
            for binding in arriving.bindings:
                slot = _find_slot(self.device, binding.control_id)
                if slot is None:
                    logger.error(
                        "Control %s not found on device %s",
                        binding.control_id,
                        self.config_id,
                    )
                    continue
                if not await self._try_resolve_binding(
                    binding,
                    slot,
                    profile_id="_dynamic",
                    page_id=arriving.page_id,
                    action_instance_id=page_session.action_instance_id,
                    page_session_id=page_session.page_session_id,
                    persist_settings=False,
                ):
                    await self._render_unavailable_to_slot(slot)
        return True

    async def _set_page_locked(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageDescriptor | None = None,
        page_session: DynamicPageSession | None = None,
        close_dynamic: bool = True,
        close_reason: str = "navigate",
        causation_id: str | None = None,
    ) -> bool:
        """Navigate to a static page (profile, page) or dynamic page (descriptor). Caller must hold _nav_lock."""
        session_to_close = self._dynamic_page_session if close_dynamic else None
        if descriptor is not None:
            transition = self._nav.set_page(descriptor)
        else:
            profile_name = profile or "default"
            page_index = page if page is not None else 0
            profile_obj = self._find_profile(profile_name)
            transition = self._nav.set_page(
                StaticPageRef(profile_name=profile_obj.name, page_index=page_index)
            )
        ok = await self._execute_transition(
            transition,
            page_session=page_session,
        )
        if ok and session_to_close is not None:
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
        if not descriptor or not descriptor.bindings:
            return

        async with self._nav_lock:
            if self._dynamic_page_session is not None:
                logger.warning("open_page rejected: dynamic page already active")
                return

            binding_id = self._binding_by_context.get(context_id)
            owner_lease = (
                self._binding_leases.get(binding_id) if binding_id is not None else None
            )
            if owner_lease is None:
                logger.warning("open_page ignored: no active context for %s", context_id)
                return
            if owner_lease.page_session_id is not None:
                logger.warning("open_page rejected from dynamic child binding")
                return

            try:
                owner_page = int(owner_lease.page_id)
            except ValueError:
                owner_page = 0

            timeout_ms = self._resolve_widget_timeout_ms(
                owner_lease.profile_id, owner_page
            )
            page_id = descriptor.page_id or make_dynamic_page_id()
            descriptor = DynamicPageDescriptor(
                page_id=page_id,
                bindings=descriptor.bindings,
            )

            session = DynamicPageSession(
                page_id=page_id,
                page_session_id=make_page_session_id(),
                context_id=make_context_id(),
                action_instance_id=owner_lease.action_instance_id,
                owner_context_id=context_id,
                owner_binding_id=owner_lease.binding_id,
                owner_control_id=owner_lease.control_id,
                owner_action_uuid=owner_lease.action_uuid,
                owner_host_id=owner_lease.host_id,
                owner_profile=owner_lease.profile_id,
                owner_page=owner_page,
                timeout_ms=timeout_ms,
                last_activity=self._clock(),
                settings_target=owner_lease.settings_target,
            )

            ok = await self._set_page_locked(
                descriptor=descriptor,
                page_session=session,
                close_dynamic=False,
            )
            if ok:
                self._dynamic_page_session = session
                await self._emit_page_appear(session, causation_id=causation_id)

    def _page_control_session(self, context_id: str) -> DynamicPageSession | None:
        session = self._dynamic_page_session
        if session is None:
            return None
        if context_id == session.context_id:
            return session
        binding_id = self._binding_by_context.get(context_id)
        lease = self._binding_leases.get(binding_id) if binding_id is not None else None
        if lease is None:
            return None
        if lease.page_session_id != session.page_session_id:
            return None
        if lease.action_instance_id != session.action_instance_id:
            return None
        if lease.host_id != session.owner_host_id:
            return None
        return session

    async def update_page(
        self,
        *,
        descriptor: DynamicPageDescriptor,
        context_id: str,
        causation_id: str | None = None,
    ) -> None:
        """Refresh child bindings inside the active page session."""
        if not descriptor or not descriptor.bindings:
            return
        async with self._nav_lock:
            session = self._page_control_session(context_id)
            if session is None:
                logger.warning("update_page ignored: no active page for %s", context_id)
                return
            if descriptor.page_id != session.page_id:
                logger.warning(
                    "update_page rejected: descriptor page %s does not match session page %s",
                    descriptor.page_id,
                    session.page_id,
                )
                return
            ok = await self._set_page_locked(
                descriptor=descriptor,
                page_session=session,
                close_dynamic=False,
            )
            if ok:
                session.last_activity = self._clock()

    async def replace_page(
        self,
        *,
        descriptor: DynamicPageDescriptor,
        context_id: str,
        causation_id: str | None = None,
    ) -> None:
        """Replace the active page session with a new concrete session."""
        if not descriptor or not descriptor.bindings:
            return
        async with self._nav_lock:
            current = self._page_control_session(context_id)
            if current is None:
                logger.warning("replace_page ignored: no active page for %s", context_id)
                return
            page_id = descriptor.page_id or make_dynamic_page_id()
            replacement = DynamicPageDescriptor(
                page_id=page_id,
                bindings=descriptor.bindings,
            )
            next_session = DynamicPageSession(
                page_id=page_id,
                page_session_id=make_page_session_id(),
                context_id=make_context_id(),
                action_instance_id=current.action_instance_id,
                owner_context_id=current.owner_context_id,
                owner_binding_id=current.owner_binding_id,
                owner_control_id=current.owner_control_id,
                owner_action_uuid=current.owner_action_uuid,
                owner_host_id=current.owner_host_id,
                owner_profile=current.owner_profile,
                owner_page=current.owner_page,
                timeout_ms=current.timeout_ms,
                last_activity=self._clock(),
                settings_target=current.settings_target,
            )
            ok = await self._set_page_locked(
                descriptor=replacement,
                page_session=next_session,
                close_dynamic=False,
            )
            if ok:
                await self._emit_page_disappear(
                    current,
                    reason="replaced",
                    causation_id=causation_id,
                )
                self._dynamic_page_session = next_session
                await self._emit_page_appear(next_session, causation_id=causation_id)

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None:
        """Close the active widget page and return to its owner profile page."""
        async with self._nav_lock:
            session = self._page_control_session(context_id)
            if session is None:
                logger.info("No owner for dynamic page")
                return
            self._dynamic_page_session = None
            await self._set_page_locked(
                profile=session.owner_profile,
                page=session.owner_page,
                close_dynamic=False,
            )
            await self._emit_page_disappear(
                session,
                reason=reason,
                causation_id=causation_id,
            )

    async def clear_page(self, *, clear_outputs: bool = True):
        async with self._nav_lock:
            await self._finalize_dynamic_page(reason="clear")
            await self._revoke_active_bindings(clear_outputs=clear_outputs)
            if clear_outputs:
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
        session = self._dynamic_page_session
        if (
            session is not None
            and f"{session.owner_host_id}::{session.owner_action_uuid}"
            in unregistered_set
        ):
            await self.close_page(
                context_id=session.context_id,
                reason="action_unregistered",
            )

        to_remove: list[BindingLease] = []
        to_reappear: list[ControlContext] = []
        for lease in list(self._binding_leases.values()):
            ctx = lease.context
            ctx_qualified = f"{lease.host_id}::{lease.action_uuid}"
            if ctx_qualified in unregistered_set:
                to_remove.append(lease)
                continue
            if ctx_qualified in registered_set:
                to_reappear.append(ctx)
        for lease in to_remove:
            await self._revoke_binding(lease.binding_id)
            await self._render_unavailable_to_slot(lease.slot)
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
            page_session_id = None
            action_instance_id = None
            persist_settings = True
        else:
            bindings = current_page.bindings
            profile_id = "_dynamic"
            page_id = current_page.page_id
            session = self._dynamic_page_session
            if session is None:
                return
            page_session_id = session.page_session_id
            action_instance_id = session.action_instance_id
            persist_settings = False

        logger.info(
            "Re-evaluating page bindings for config=%s page=%s after actions change +%s -%s",
            self.config_id,
            page_id,
            registered,
            unregistered,
        )

        for binding in bindings:
            if await self.action_contexts.has_key(binding.control_id):
                continue  # Already has context
            slot = _find_slot(self.device, binding.control_id)
            if slot is None:
                continue
            resolved_action_instance_id = action_instance_id or _action_instance_id(
                controller_id=self._controller_id,
                config_id=self.config_id,
                profile_id=profile_id,
                page_id=page_id,
                control_id=binding.control_id,
                action_uuid=binding.action_uuid,
            )
            await self._try_resolve_binding(
                binding,
                slot,
                profile_id=profile_id,
                page_id=page_id,
                action_instance_id=resolved_action_instance_id,
                page_session_id=page_session_id,
                persist_settings=persist_settings,
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
            cleared = await self._settings_service.clear_config_targets(
                controller_id=self._controller_id,
                config_id=self.config_id,
            )
            if cleared:
                logger.info(
                    "Cleared %d runtime settings overlay(s) for %s after config change",
                    cleared,
                    self.config_id,
                )
            self.config = config
            if self._dynamic_page_session is not None:
                await self._finalize_dynamic_page(reason="config_change")
            transition = self._nav.update_config(config)
            await self._execute_transition(transition)

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

        action_instance_id = subject_action_instance_id(msg.subject)
        binding_id = subject_binding_id(msg.subject)
        page_session_id = subject_page_session_id(msg.subject)

        if binding_id is not None:
            lease = self._binding_leases.get(binding_id)
            if lease is None or lease.context_id != context_id:
                logger.warning(
                    "Ignoring plugin command %s from %s for inactive binding %s",
                    msg.message_type,
                    msg.sender,
                    binding_id,
                )
                return None
            active_binding_id = self._active_binding_by_control.get(lease.control_id)
            if active_binding_id != binding_id:
                logger.warning(
                    "Ignoring plugin command %s for inactive control binding %s",
                    msg.message_type,
                    binding_id,
                )
                return None
            if sender_host_id != lease.host_id:
                logger.warning(
                    "Ignoring plugin command %s from host %s for binding owned by host %s",
                    msg.message_type,
                    sender_host_id,
                    lease.host_id,
                )
                return None
            if (
                action_instance_id is not None
                and action_instance_id != lease.action_instance_id
            ):
                logger.warning(
                    "Ignoring plugin command %s for mismatched action instance %s",
                    msg.message_type,
                    action_instance_id,
                )
                return None
            if page_session_id is not None and page_session_id != lease.page_session_id:
                logger.warning(
                    "Ignoring plugin command %s for mismatched page session %s",
                    msg.message_type,
                    page_session_id,
                )
                return None
            return AuthorizedCommandTarget(
                sender_host_id=sender_host_id,
                context_id=context_id,
                binding=lease,
            )

        session = self._dynamic_page_session
        if page_session_id is not None:
            if (
                session is None
                or page_session_id != session.page_session_id
                or context_id != session.context_id
            ):
                logger.warning(
                    "Ignoring plugin command %s for inactive page session %s",
                    msg.message_type,
                    page_session_id,
                )
                return None
            if sender_host_id != session.owner_host_id:
                logger.warning(
                    "Ignoring plugin command %s from host %s for page owned by host %s",
                    msg.message_type,
                    sender_host_id,
                    session.owner_host_id,
                )
                return None
            if (
                action_instance_id is not None
                and action_instance_id != session.action_instance_id
            ):
                logger.warning(
                    "Ignoring plugin command %s for mismatched page action instance %s",
                    msg.message_type,
                    action_instance_id,
                )
                return None
            return AuthorizedCommandTarget(
                sender_host_id=sender_host_id,
                context_id=context_id,
                page_session=session,
            )

        logger.warning(
            "Ignoring plugin command %s from %s without binding or page session subject",
            msg.message_type,
            msg.sender,
        )
        return None

    async def handle_command(self, msg: DeckrMessage) -> None:
        """Handle a command message from a plugin host (setTitle, setImage, etc.)."""
        payload = plugin_body_dict(msg)
        context_id = subject_context_id(msg.subject) or ""
        if not context_id:
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
                subject=msg.subject,
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

        if msg_type == UPDATE_PAGE:
            desc_data = payload.get("descriptor")
            descriptor = _descriptor_from_payload(desc_data) if desc_data else None
            if descriptor is not None:
                await self.update_page(
                    descriptor=descriptor,
                    context_id=context_id,
                    causation_id=msg.message_id,
                )
            return

        if msg_type == REPLACE_PAGE:
            desc_data = payload.get("descriptor")
            descriptor = _descriptor_from_payload(desc_data) if desc_data else None
            if descriptor is not None:
                await self.replace_page(
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

        page_session = authorization.page_session
        if page_session is not None:
            if msg_type == SET_SETTINGS:
                target = page_session.settings_target
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
                    await self._settings_service.get(page_session.settings_target)
                    if page_session.settings_target is not None
                    else {}
                )
                await send_settings_response(current_settings)
            return

        lease = authorization.binding
        if lease is None:
            return

        router = lease.context._router

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
            if lease.page_session_id is not None:
                logger.warning("Ignoring setPage from dynamic child binding")
                return
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
        if self._dynamic_page_session is not None:
            self._record_page_activity()

        control_id = translated.slot_id
        binding_id = self._active_binding_by_control.get(control_id)
        lease = self._binding_leases.get(binding_id) if binding_id is not None else None
        if lease is None:
            return

        method_name = translated.method_name
        plugin_event = translated.plugin_event.model_copy(
            update={"context": lease.context_id}
        )
        try:
            await getattr(lease.context, method_name)(plugin_event)
        except Exception as e:
            logger.error(
                "Error calling %s on action %s: %s",
                method_name,
                lease.action_uuid,
                e,
                exc_info=True,
            )
