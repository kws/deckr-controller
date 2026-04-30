import base64
import binascii
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
from deckr.contracts.messages import (
    RESERVED_BUILTIN_PROVIDER_IDS,
    DeckrMessage,
    parse_host_address,
)
from deckr.contracts.models import thaw_json
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    DECKR_OUTPUT_RASTER,
    CapabilityRef,
    ControlRef,
    DeviceDescriptor,
    DeviceRef,
)
from deckr.pluginhost.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_INSTANCE_DESTROYED,
    BINDING_OUTPUT,
    CLOSE_PAGE,
    OPEN_PAGE,
    PAGE_SESSION_CLOSED,
    PAGE_SESSION_OPENED,
    REPLACE_PAGE,
    SET_PAGE,
    SETTINGS_PATCH,
    SETTINGS_REPLACE,
    SETTINGS_REQUEST,
    SETTINGS_SNAPSHOT,
    UPDATE_PAGE,
    ActionInstanceLifecycleBody,
    ActionInstanceMetadata,
    BindingMetadata,
    BindingOutputBody,
    DynamicPageCommand,
    MatchedCapability,
    PageSessionLifecycleBody,
    PageSessionMetadata,
    SettingsPatchBody,
    SettingsReplaceBody,
    SettingsSnapshotBody,
    SettingsTargetRef,
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
from pydantic import ValidationError

from deckr.controller._binding_resolution import ResolvedControlBinding
from deckr.controller._binding_validator import (
    BLOCKING_ERROR_CODES,
    format_validation_summary,
    validate_exact_control_bindings,
    validate_page_bindings,
)
from deckr.controller._command_router import DeviceOutput
from deckr.controller._device_layout import (
    ControlSurface,
    control_surface_for_raster_capability,
    raster_controls,
)
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
    SettingsService,
    derive_action_instance_id,
)

logger = logging.getLogger(__name__)

DEFAULT_WIDGET_TIMEOUT_MS = 60_000


def _descriptor_from_payload(data: dict) -> DynamicPageCommand | None:
    """Validate a dynamic page descriptor from a bus payload."""
    if not data:
        return None
    bindings_data = data.get("bindings")
    if not bindings_data:
        return None
    try:
        return DynamicPageCommand.model_validate(data)
    except ValidationError:
        logger.warning("Ignoring invalid dynamic page descriptor payload", exc_info=True)
        return None


def _find_control_surface(
    device: DeviceDescriptor,
    control_id: str,
    *,
    raster_capability_id: str | None = None,
) -> ControlSurface | None:
    for control in device.controls:
        if control.control_id == control_id:
            return control_surface_for_raster_capability(control, raster_capability_id)
    return None


def _selected_raster_capability_id(binding: ResolvedControlBinding) -> str | None:
    for capability in binding.control.output_capabilities:
        if capability.capability_id not in binding.output_capability_ids:
            continue
        if (
            capability.family == DECKR_OUTPUT_RASTER
            and capability.capability_type == "bitmap"
        ):
            return capability.capability_id
    return None


@dataclass(slots=True)
class DynamicPageSession:
    page_id: str
    page_session_id: str
    context_id: str
    action_instance_id: str
    template_id: str | None
    owner_context_id: str
    owner_binding_id: str
    owner_control_id: str
    owner_action_uuid: str
    owner_host_id: str
    owner_profile: str
    owner_page: int
    timeout_ms: int
    last_activity: float
    settings_target: SettingsTargetRef | None


@dataclass(slots=True)
class BindingLease:
    binding_id: str
    context_id: str
    action_instance_id: str
    action_uuid: str
    host_id: str
    control_id: str
    control: ControlSurface
    input_capability_ids: frozenset[str]
    raster_capability_id: str | None
    profile_id: str
    page_id: str
    settings_target: SettingsTargetRef | None
    context: ControlContext
    page_session_id: str | None = None
    role_id: str | None = None
    item_key: str | None = None
    handler: str | None = None


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
        device: DeviceDescriptor,
        hardware_ref: DeviceRef,
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
        self._settings_service = settings_service
        self.action_contexts = AsyncMap[str, ControlContext]()
        self._translator = EventTranslator(controller_id=controller_id)
        self._nav = NavigationService(config)
        self._dynamic_page_session: DynamicPageSession | None = None
        self._binding_leases: dict[str, BindingLease] = {}
        self._binding_by_context: dict[str, str] = {}
        self._active_binding_by_control: dict[str, str] = {}
        self._action_instances: dict[str, ActionInstanceMetadata] = {}
        self._action_instance_hosts: dict[str, str] = {}
        self._clock = clock or time.monotonic
        self._page_timeout_check_interval = page_timeout_check_interval
        self._nav_lock = anyio.Lock()
        self._start_soon(self._page_timeout_loop)

    async def _render_unavailable_to_control(self, control: ControlSurface) -> None:
        """Render a not-available overlay to an output-capable control."""
        if control.image_format is None or control.raster_capability_id is None:
            return
        model = RenderModel(overlay_type="unavailable")
        render_service = RenderService()
        output = DeviceOutput(
            self._command_service,
            self.config_id,
            control.id,
            control.raster_capability_id,
        )
        context_id = make_context_id()
        request = render_service.build_request(
            model,
            control.image_format,
            context_id=context_id,
            control_id=control.id,
        )
        await self._render_dispatcher.submit_request(
            control_id=control.id,
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
        await lease.context.on_binding_detached("detach")
        output = (
            DeviceOutput(
                self._command_service,
                self.config_id,
                lease.control_id,
                lease.raster_capability_id,
            )
            if lease.raster_capability_id is not None
            else None
        )
        await self._render_dispatcher.clear_control(
            lease.control_id,
            context_id=lease.context_id,
            binding_id=lease.binding_id,
            output=output,
            clear_output=clear_output,
        )
        return lease

    async def _revoke_active_bindings(self, *, clear_outputs: bool = True) -> None:
        for binding_id in list(self._binding_leases):
            await self._revoke_binding(binding_id, clear_output=clear_outputs)

    async def _clear_all_raster_controls(self) -> None:
        """Clear raster-capable controls before rendering a new page."""
        for control in raster_controls(self.device):
            await self._render_dispatcher.clear_control(
                control.control_id,
                output=DeviceOutput(
                    self._command_service,
                    self.config_id,
                    control.control_id,
                    control.capability_id,
                ),
            )

    def _build_settings_target_for_binding(
        self,
        *,
        action_instance_id: str,
        binding: ResolvedControlBinding,
        plugin_uuid: str | None = None,
    ) -> SettingsTargetRef:
        return SettingsTargetRef(
            scope="action_instance",
            controllerId=self._controller_id,
            configId=self.config_id,
            pluginId=plugin_uuid or "",
            actionId=binding.action_uuid,
            actionInstanceId=action_instance_id,
            stableId=binding.stable_id,
        )

    def _matched_capabilities(
        self,
        binding: ResolvedControlBinding,
    ) -> tuple[MatchedCapability, ...]:
        selected = {
            "input": binding.input_capability_ids,
            "output": binding.output_capability_ids,
            "state": binding.state_capability_ids,
            "config": binding.config_capability_ids,
            "diagnostic": binding.diagnostic_capability_ids,
        }
        matches: list[MatchedCapability] = []
        for direction, capability_ids in selected.items():
            for capability in binding.control.capabilities:
                if capability.capability_id not in capability_ids:
                    continue
                if direction != "diagnostic" and capability.direction != direction:
                    continue
                matches.append(
                    MatchedCapability(
                        requirementName=direction,
                        capability=CapabilityRef(
                            deviceRef=self.hardware_ref,
                            controlId=binding.control_id,
                            capabilityId=capability.capability_id,
                        ),
                        family=capability.family,
                        type=capability.capability_type,
                        direction=capability.direction,
                        eventTypes=capability.event_types,
                        commandTypes=capability.command_types,
                        provenance="native",
                    )
                )
        return tuple(matches)

    async def _ensure_action_instance(
        self,
        *,
        action_meta: Any,
        action_instance_id: str,
        context_id: str,
        settings: Mapping[str, Any],
    ) -> None:
        if action_instance_id in self._action_instances:
            return
        metadata = ActionInstanceMetadata(
            pluginId=action_meta.plugin_uuid,
            actionId=action_meta.uuid,
            actionInstanceId=action_instance_id,
            configId=self.config_id,
            contextId=context_id,
        )
        self._action_instances[action_instance_id] = metadata
        self._action_instance_hosts[action_instance_id] = action_meta.host_id
        if action_meta.host_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        msg = plugin_message(
            sender=controller_address(self._controller_id),
            recipient=host_address(action_meta.host_id),
            message_type=ACTION_INSTANCE_CREATED,
            body=ActionInstanceLifecycleBody(
                metadata=metadata,
                settings=dict(settings),
            ),
            subject=context_subject(
                context_id,
                config_id=self.config_id,
                action_instance_id=action_instance_id,
                action_uuid=action_meta.uuid,
            ),
        )
        await self._plugin_bus.publish(msg)

    async def _destroy_action_instance(
        self,
        action_instance_id: str,
        *,
        reason: str,
    ) -> None:
        metadata = self._action_instances.pop(action_instance_id, None)
        host_id = self._action_instance_hosts.pop(action_instance_id, None)
        if metadata is None or host_id is None or host_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        msg = plugin_message(
            sender=controller_address(self._controller_id),
            recipient=host_address(host_id),
            message_type=ACTION_INSTANCE_DESTROYED,
            body=ActionInstanceLifecycleBody(metadata=metadata, reason=reason),
            subject=context_subject(
                metadata.context_id or "",
                config_id=self.config_id,
                action_instance_id=metadata.action_instance_id,
                action_uuid=metadata.action_id,
            ),
        )
        await self._plugin_bus.publish(msg)

    async def _destroy_all_action_instances(self, *, reason: str) -> None:
        for action_instance_id in list(self._action_instances):
            await self._destroy_action_instance(action_instance_id, reason=reason)

    async def _try_resolve_binding(
        self,
        binding: ResolvedControlBinding,
        *,
        profile_id: str,
        page_id: str,
        action_instance_id: str,
        page_session_id: str | None = None,
        persist_settings: bool = True,
        role_id: str | None = None,
        item_key: str | None = None,
        handler: str | None = None,
    ) -> bool:
        """Resolve a binding: create ControlContext and announce the binding lease.
        Returns True if context was created, False if action not found (caller should render unavailable).
        """
        raster_capability_id = _selected_raster_capability_id(binding)
        control = _find_control_surface(
            self.device,
            binding.control_id,
            raster_capability_id=raster_capability_id,
        )
        if control is None:
            logger.warning(
                "Resolved binding control disappeared before bind: config=%s control=%s",
                self.config_id,
                binding.control_id,
            )
            return False
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
            self._build_settings_target_for_binding(
                action_instance_id=action_instance_id,
                binding=binding,
                plugin_uuid=action_meta.plugin_uuid or action_meta.host_id,
            )
            if persist_settings
            else None
        )
        initial_settings = dict(binding.settings)
        if self._settings_service is not None and settings_target is not None:
            try:
                snapshot = await self._settings_service.get(settings_target)
                initial_settings = dict(thaw_json(snapshot.settings))
            except KeyError:
                initial_settings = dict(binding.settings)
        builtin_action = None
        if action_meta.host_id == BUILTIN_ACTION_PROVIDER_ID and hasattr(
            self.manager, "get_builtin_action"
        ):
            builtin_action = self.manager.get_builtin_action(action_meta.uuid)
        binding_id = make_binding_id()
        context_id = make_context_id()
        await self._ensure_action_instance(
            action_meta=action_meta,
            action_instance_id=action_instance_id,
            context_id=context_id,
            settings=initial_settings,
        )
        binding_metadata = BindingMetadata(
            pluginId=action_meta.plugin_uuid,
            actionId=action_meta.uuid,
            actionInstanceId=action_instance_id,
            configId=self.config_id,
            contextId=context_id,
            bindingId=binding_id,
            pageSessionId=page_session_id,
            deviceRef=self.hardware_ref,
            controlRef=ControlRef(
                deviceRef=self.hardware_ref,
                controlId=control.id,
            ),
            roleId=role_id,
            itemKey=item_key,
            handler=handler,
            matchedCapabilities=self._matched_capabilities(binding),
        )
        ctx = ControlContext(
            controller_id=self._controller_id,
            device=self.device,
            config_id=self.config_id,
            command_service=self._command_service,
            host_id=action_meta.host_id,
            action_uuid=action_meta.uuid,
            control=control,
            settings=initial_settings,
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
            metadata=binding_metadata,
        )
        lease = BindingLease(
            binding_id=binding_id,
            context_id=context_id,
            action_instance_id=action_instance_id,
            action_uuid=action_meta.uuid,
            host_id=action_meta.host_id,
            control_id=control.id,
            control=control,
            input_capability_ids=binding.input_capability_ids,
            raster_capability_id=raster_capability_id,
            profile_id=profile_id,
            page_id=page_id,
            settings_target=settings_target,
            context=ctx,
            page_session_id=page_session_id,
            role_id=role_id,
            item_key=item_key,
            handler=handler,
        )
        self._binding_leases[binding_id] = lease
        self._binding_by_context[context_id] = binding_id
        self._active_binding_by_control[control.id] = binding_id
        await self.action_contexts.set(control.id, ctx)
        await ctx.on_binding_attached()
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

    def _page_session_metadata(
        self,
        session: DynamicPageSession,
    ) -> PageSessionMetadata:
        bindings = tuple(
            lease.context.metadata
            for lease in self._binding_leases.values()
            if lease.page_session_id == session.page_session_id
        )
        return PageSessionMetadata(
            actionInstanceId=session.action_instance_id,
            configId=self.config_id,
            pageId=session.page_id,
            pageSessionId=session.page_session_id,
            contextId=session.context_id,
            templateId=session.template_id,
            ownerBindingId=session.owner_binding_id,
            bindings=bindings,
        )

    async def _emit_page_opened(
        self,
        session: DynamicPageSession,
        *,
        causation_id: str | None = None,
    ) -> None:
        if session.owner_host_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        msg = plugin_message(
            sender=controller_address(self._controller_id),
            recipient=host_address(session.owner_host_id),
            message_type=PAGE_SESSION_OPENED,
            body=PageSessionLifecycleBody(
                pageSession=self._page_session_metadata(session)
            ),
            subject=context_subject(
                session.context_id,
                config_id=self.config_id,
                action_instance_id=session.action_instance_id,
                page_session_id=session.page_session_id,
                action_uuid=session.owner_action_uuid,
            ),
            causation_id=causation_id,
        )
        await self._plugin_bus.publish(msg)

    async def _emit_page_closed(
        self,
        session: DynamicPageSession,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> None:
        if session.owner_host_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        msg = plugin_message(
            sender=controller_address(self._controller_id),
            recipient=host_address(session.owner_host_id),
            message_type=PAGE_SESSION_CLOSED,
            body=PageSessionLifecycleBody(
                pageSession=self._page_session_metadata(session),
                reason=reason,
            ),
            subject=context_subject(
                session.context_id,
                config_id=self.config_id,
                action_instance_id=session.action_instance_id,
                page_session_id=session.page_session_id,
                action_uuid=session.owner_action_uuid,
            ),
            causation_id=causation_id,
        )
        await self._plugin_bus.publish(msg)

    async def _finalize_dynamic_page(
        self,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> None:
        session = self._dynamic_page_session
        if session is None:
            return
        await self._emit_page_closed(
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
            result = await validate_page_bindings(
                self._nav.resolve_static_bindings(arriving),
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
                            "Binding validation failed [%s]: %s (control=%s action=%s) %s",
                            err.code,
                            err.message,
                            err.control_ref,
                            err.action_uuid,
                            err.details,
                        )
                if transition.departing is not None:
                    self._nav.set_page(transition.departing)
                return False
            for err in result.errors:
                if err.code not in BLOCKING_ERROR_CODES:
                    logger.warning(
                        "Action unavailable (control will show 'not available'): %s (control=%s action=%s)",
                        err.message,
                        err.control_ref,
                        err.action_uuid,
                    )
        elif isinstance(arriving, DynamicPageCommand):
            if page_session is None:
                logger.error("Dynamic page validation missing page session")
                return False
            result = await validate_exact_control_bindings(
                list(arriving.bindings),
                self.device,
                self.manager.get_action,
                action_uuid=page_session.owner_action_uuid,
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
                            "Dynamic page binding validation failed [%s]: %s (control=%s action=%s) %s",
                            err.code,
                            err.message,
                            err.control_ref,
                            err.action_uuid,
                            err.details,
                        )
                if transition.departing is not None:
                    self._nav.set_page(transition.departing)
                return False
            for err in result.errors:
                if err.code not in BLOCKING_ERROR_CODES:
                    logger.warning(
                        "Action unavailable (control will show 'not available'): %s (control=%s action=%s)",
                        err.message,
                        err.control_ref,
                        err.action_uuid,
                    )

        if transition.departing is not None:
            await self._revoke_active_bindings()

        await self._clear_all_raster_controls()

        if isinstance(arriving, StaticPageRef):
            for binding in result.bindings:
                action_instance_id = derive_action_instance_id(
                    controller_id=self._controller_id,
                    config_id=self.config_id,
                    action_id=binding.action_uuid,
                    stable_id=binding.stable_id,
                    profile_id=arriving.profile_name,
                    page_id=str(arriving.page_index),
                    control_id=binding.control_id,
                )
                if not await self._try_resolve_binding(
                    binding,
                    profile_id=arriving.profile_name,
                    page_id=str(arriving.page_index),
                    action_instance_id=action_instance_id,
                ):
                    control = _find_control_surface(
                        self.device,
                        binding.control_id,
                        raster_capability_id=_selected_raster_capability_id(binding),
                    )
                    if control is not None:
                        await self._render_unavailable_to_control(control)
        elif isinstance(arriving, DynamicPageCommand):
            if page_session is None:
                logger.error("Dynamic page transition missing page session")
                return False
            for child, binding in zip(arriving.bindings, result.bindings, strict=True):
                if not await self._try_resolve_binding(
                    binding,
                    profile_id="_dynamic",
                    page_id=arriving.page_id,
                    action_instance_id=page_session.action_instance_id,
                    page_session_id=page_session.page_session_id,
                    persist_settings=False,
                    role_id=child.role_id,
                    item_key=child.item_key,
                    handler=child.handler,
                ):
                    control = _find_control_surface(
                        self.device,
                        binding.control_id,
                        raster_capability_id=_selected_raster_capability_id(binding),
                    )
                    if control is not None:
                        await self._render_unavailable_to_control(control)
        return True

    async def _set_page_locked(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageCommand | None = None,
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
        descriptor: DynamicPageCommand | None = None,
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
        descriptor: DynamicPageCommand,
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
            descriptor = DynamicPageCommand(
                pageId=page_id,
                templateId=descriptor.template_id,
                bindings=descriptor.bindings,
            )

            session = DynamicPageSession(
                page_id=page_id,
                page_session_id=make_page_session_id(),
                context_id=make_context_id(),
                action_instance_id=owner_lease.action_instance_id,
                template_id=descriptor.template_id,
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
                await self._emit_page_opened(session, causation_id=causation_id)

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
        descriptor: DynamicPageCommand,
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
        descriptor: DynamicPageCommand,
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
            replacement = DynamicPageCommand(
                pageId=page_id,
                templateId=descriptor.template_id,
                bindings=descriptor.bindings,
            )
            next_session = DynamicPageSession(
                page_id=page_id,
                page_session_id=make_page_session_id(),
                context_id=make_context_id(),
                action_instance_id=current.action_instance_id,
                template_id=descriptor.template_id,
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
                await self._emit_page_closed(
                    current,
                    reason="replaced",
                    causation_id=causation_id,
                )
                self._dynamic_page_session = next_session
                await self._emit_page_opened(next_session, causation_id=causation_id)

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
            await self._emit_page_closed(
                session,
                reason=reason,
                causation_id=causation_id,
            )

    async def clear_page(self, *, clear_outputs: bool = True):
        async with self._nav_lock:
            await self._finalize_dynamic_page(reason="clear")
            await self._revoke_active_bindings(clear_outputs=clear_outputs)
            await self._destroy_all_action_instances(reason="clear")
            if clear_outputs:
                await self._clear_all_raster_controls()

    async def on_descriptor_changed(self, descriptor: DeviceDescriptor) -> None:
        """Re-resolve the active page against a changed device descriptor."""

        async with self._nav_lock:
            self.device = descriptor
            current_page = self._nav.current_page
            if current_page is None:
                return
            ok = await self._execute_transition(
                PageTransition(departing=current_page, arriving=current_page),
                page_session=self._dynamic_page_session,
            )
            if not ok:
                await self._finalize_dynamic_page(reason="device_descriptor_changed")
                await self._revoke_active_bindings(clear_outputs=False)

    async def on_capability_state_changed(
        self,
        event: hw_messages.CapabilityStateChangedMessage,
    ) -> None:
        logger.info(
            "Observed capability state change config=%s control=%s capability=%s state=%s",
            self.config_id,
            event.control_id,
            event.capability_id,
            event.state_type,
        )

    async def on_command_rejected(
        self,
        event: hw_messages.CommandRejectedMessage,
    ) -> None:
        logger.warning(
            "Hardware command rejected config=%s control=%s capability=%s command=%s reason=%s message=%s",
            self.config_id,
            event.control_id,
            event.capability_id,
            event.command_type,
            event.reason,
            event.message,
        )

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
            if not any(
                other.action_instance_id == lease.action_instance_id
                for other in self._binding_leases.values()
            ):
                await self._destroy_action_instance(
                    lease.action_instance_id,
                    reason="action_unregistered",
                )
            await self._render_unavailable_to_control(lease.control)
        for ctx in to_reappear:
            await ctx.on_binding_attached()

        # Handle registered: try to resolve bindings that were previously unavailable
        current_page = self._nav.current_page
        if current_page is None:
            return

        if isinstance(current_page, StaticPageRef):
            result = await validate_page_bindings(
                self._nav.resolve_static_bindings(current_page),
                self.device,
                self.manager.get_action,
                profile_id=current_page.profile_name,
                page_id=str(current_page.page_index),
            )
            profile_id = current_page.profile_name
            page_id = str(current_page.page_index)
            page_session_id = None
            action_instance_id = None
            persist_settings = True
        else:
            session = self._dynamic_page_session
            if session is None:
                return
            result = await validate_exact_control_bindings(
                list(current_page.bindings),
                self.device,
                self.manager.get_action,
                action_uuid=session.owner_action_uuid,
                profile_id="_dynamic",
                page_id=current_page.page_id,
            )
            profile_id = "_dynamic"
            page_id = current_page.page_id
            page_session_id = session.page_session_id
            action_instance_id = session.action_instance_id
            persist_settings = False

        if result.has_blocking_errors:
            logger.warning(
                "Skipping action-change rebind for invalid page bindings: %s",
                format_validation_summary(result),
            )
            return

        logger.info(
            "Re-evaluating page bindings for config=%s page=%s after actions change +%s -%s",
            self.config_id,
            page_id,
            registered,
            unregistered,
        )

        child_bindings = (
            current_page.bindings
            if isinstance(current_page, DynamicPageCommand)
            else (None,) * len(result.bindings)
        )
        for child, binding in zip(child_bindings, result.bindings, strict=True):
            if await self.action_contexts.has_key(binding.control_id):
                continue  # Already has context
            resolved_action_instance_id = action_instance_id or derive_action_instance_id(
                controller_id=self._controller_id,
                config_id=self.config_id,
                action_id=binding.action_uuid,
                stable_id=binding.stable_id,
                profile_id=profile_id,
                page_id=page_id,
                control_id=binding.control_id,
            )
            await self._try_resolve_binding(
                binding,
                profile_id=profile_id,
                page_id=page_id,
                action_instance_id=resolved_action_instance_id,
                page_session_id=page_session_id,
                persist_settings=persist_settings,
                role_id=child.role_id if child is not None else None,
                item_key=child.item_key if child is not None else None,
                handler=child.handler if child is not None else None,
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
            if self._dynamic_page_session is not None:
                await self._finalize_dynamic_page(reason="config_change")
            await self._destroy_all_action_instances(reason="config_change")
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
        """Handle a canonical command message from a plugin host."""
        payload = plugin_body_dict(msg)
        msg_type = msg.message_type

        async def send_settings_response(snapshot_body: SettingsSnapshotBody) -> None:
            await self._plugin_bus.reply_to(
                msg,
                message_type=SETTINGS_SNAPSHOT,
                body=snapshot_body.to_dict(),
                subject=msg.subject,
            )

        if msg_type in {SETTINGS_REQUEST, SETTINGS_PATCH, SETTINGS_REPLACE}:
            sender_host_id = self._command_sender_host_id(msg)
            if sender_host_id is None:
                return
            target_data = payload.get("target")
            if not isinstance(target_data, dict):
                return
            target = SettingsTargetRef.model_validate(target_data)
            if target.config_id != self.config_id:
                return
            if target.scope == "plugin":
                if self._settings_service is None:
                    return
                if msg_type == SETTINGS_REQUEST:
                    snapshot = await self._settings_service.get(target)
                elif msg_type == SETTINGS_PATCH:
                    body = SettingsPatchBody.model_validate(payload)
                    snapshot = await self._settings_service.patch(
                        target,
                        body.settings,
                    )
                else:
                    body = SettingsReplaceBody.model_validate(payload)
                    snapshot = await self._settings_service.replace(
                        target,
                        body.settings,
                    )
                await send_settings_response(SettingsSnapshotBody.from_snapshot(snapshot))
                return

        context_id = subject_context_id(msg.subject) or ""
        if not context_id:
            return
        config_id = subject_config_id(msg.subject)
        if config_id != self.config_id:
            return
        authorization = await self._authorize_plugin_command(
            msg,
            context_id=context_id,
        )
        if authorization is None:
            return

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

        if msg_type == BINDING_OUTPUT:
            if authorization.binding is not None:
                body = BindingOutputBody.model_validate(payload)
                await self._handle_binding_output(authorization.binding, body)
            return

        page_session = authorization.page_session
        if page_session is not None:
            if msg_type in {SETTINGS_REQUEST, SETTINGS_PATCH, SETTINGS_REPLACE}:
                if self._settings_service is None or page_session.settings_target is None:
                    return
                target = SettingsTargetRef.model_validate(payload.get("target"))
                if target.key() != page_session.settings_target.key():
                    logger.warning("Ignoring settings command for mismatched page target")
                    return
                if msg_type == SETTINGS_REQUEST:
                    snapshot = await self._settings_service.get(target)
                elif msg_type == SETTINGS_PATCH:
                    body = SettingsPatchBody.model_validate(payload)
                    snapshot = await self._settings_service.patch(
                        target,
                        body.settings,
                    )
                else:
                    body = SettingsReplaceBody.model_validate(payload)
                    snapshot = await self._settings_service.replace(
                        target,
                        body.settings,
                    )
                await send_settings_response(SettingsSnapshotBody.from_snapshot(snapshot))
            return

        lease = authorization.binding
        if lease is None:
            return

        if msg_type in {SETTINGS_REQUEST, SETTINGS_PATCH, SETTINGS_REPLACE}:
            if self._settings_service is None or lease.settings_target is None:
                return
            target = SettingsTargetRef.model_validate(payload.get("target"))
            if target.key() != lease.settings_target.key():
                logger.warning("Ignoring settings command for mismatched binding target")
                return
            if msg_type == SETTINGS_REQUEST:
                snapshot = await self._settings_service.get(target)
            elif msg_type == SETTINGS_PATCH:
                body = SettingsPatchBody.model_validate(payload)
                snapshot = await self._settings_service.patch(target, body.settings)
            else:
                body = SettingsReplaceBody.model_validate(payload)
                snapshot = await self._settings_service.replace(target, body.settings)
            lease.context._store.settings = dict(thaw_json(snapshot.settings))
            await send_settings_response(SettingsSnapshotBody.from_snapshot(snapshot))
        elif msg_type == SET_PAGE:
            if lease.page_session_id is not None:
                logger.warning("Ignoring setPage from dynamic child binding")
                return
            await self.set_page(
                profile=payload.get("profile", "default"),
                page=payload.get("page", 0),
                causation_id=msg.message_id,
            )

    async def _handle_binding_output(
        self,
        lease: BindingLease,
        body: BindingOutputBody,
    ) -> None:
        if (
            body.binding.context_id != lease.context_id
            or body.binding.binding_id != lease.binding_id
            or body.binding.action_instance_id != lease.action_instance_id
        ):
            logger.warning(
                "Ignoring binding output with mismatched mirrored lease identity"
            )
            return
        if body.binding.output_generation != body.generation:
            logger.warning(
                "Ignoring binding output with mismatched generation for binding %s",
                lease.binding_id,
            )
            return
        if body.capability.control_id != lease.control_id:
            logger.warning(
                "Ignoring binding output for wrong control %s on binding %s",
                body.capability.control_id,
                lease.binding_id,
            )
            return
        if body.capability.capability_id != lease.raster_capability_id:
            logger.warning(
                "Ignoring unsupported binding output capability %s on binding %s",
                body.capability.capability_id,
                lease.binding_id,
            )
            return
        if body.command_type == "clear":
            await self._command_service.clear_raster(
                self.config_id,
                lease.control_id,
                body.capability.capability_id,
            )
            return
        if body.command_type != "set_frame":
            logger.warning(
                "Ignoring unsupported binding output command %s on binding %s",
                body.command_type,
                lease.binding_id,
            )
            return
        image = body.params.get("image")
        encoding = body.params.get("encoding")
        if not isinstance(image, str) or encoding != "jpeg":
            logger.warning(
                "Ignoring raster output without jpeg image payload on binding %s",
                lease.binding_id,
            )
            return
        try:
            frame = base64.b64decode(image, validate=True)
        except (ValueError, binascii.Error):
            logger.warning(
                "Ignoring raster output with invalid base64 image on binding %s",
                lease.binding_id,
            )
            return
        await self._command_service.set_raster_frame(
            self.config_id,
            lease.control_id,
            body.capability.capability_id,
            frame,
        )

    async def on_event(self, message: DeckrMessage):
        event = hw_messages.hardware_body_from_message(message)
        translated = self._translator.translate(event, self.config_id)
        if translated is None:
            return
        if self._dynamic_page_session is not None:
            self._record_page_activity()

        control_id = translated.control_id
        binding_id = self._active_binding_by_control.get(control_id)
        lease = self._binding_leases.get(binding_id) if binding_id is not None else None
        if lease is None:
            logger.info(
                "Ignoring input from unbound control config=%s control=%s capability=%s",
                self.config_id,
                control_id,
                translated.capability_id,
            )
            return
        if translated.capability_id not in lease.input_capability_ids:
            logger.info(
                "Ignoring input for unselected capability config=%s control=%s capability=%s",
                self.config_id,
                control_id,
                translated.capability_id,
            )
            return

        try:
            await lease.context.on_input(translated.plugin_event)
        except Exception as e:
            logger.error(
                "Error delivering input to action %s: %s",
                lease.action_uuid,
                e,
                exc_info=True,
            )
