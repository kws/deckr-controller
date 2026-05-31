import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

import anyio
from deckr.actions.endpoints import (
    RESERVED_BUILTIN_PROVIDER_IDS,
    action_provider_address,
    parse_action_provider_address,
)
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_INSTANCE_DESTROYED,
    BINDING_OUTPUT,
    BINDING_OVERLAY,
    BINDING_OVERLAY_CLEAR,
    CLOSE_PAGE,
    OPEN_PAGE,
    PAGE_SESSION_CLOSED,
    PAGE_SESSION_OPENED,
    REPLACE_PAGE,
    SETTINGS_PATCH,
    SETTINGS_REPLACE,
    SETTINGS_REQUEST,
    SETTINGS_SNAPSHOT,
    ActionInstanceLifecycleBody,
    ActionInstanceMetadata,
    BindingMetadata,
    BindingOutputBody,
    BindingOverlayBody,
    BindingOverlayClearBody,
    DynamicPageCommand,
    MatchedCapability,
    PageChildBindingDescriptor,
    PageSessionLifecycleBody,
    PageSessionMetadata,
    SettingsPatchBody,
    SettingsReplaceBody,
    SettingsSnapshot,
    SettingsTargetRef,
    action_body_dict,
    action_message,
    context_subject,
    make_binding_id,
    make_context_id,
    make_dynamic_page_id,
    make_page_session_id,
    subject_action_instance_id,
    subject_binding_id,
    subject_config_id,
    subject_context_id,
    subject_page_session_id,
    subject_provider_instance_id,
)
from deckr.contracts.messages import (
    DeckrMessage,
    controller_address,
)
from deckr.contracts.models import thaw_json
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import messages as hw_messages
from deckr.hardware.capabilities import (
    RasterBitmapClearParams,
    raster_bitmap_command_params,
)
from deckr.hardware.descriptors import (
    DECKR_OUTPUT_RASTER,
    CapabilityRef,
    ControlRef,
    DeviceDescriptor,
    DeviceRef,
)
from pydantic import ValidationError

from deckr.controller._action_provider_sessions import (
    ActionProviderSessionManager,
    ProviderSessionKey,
    ProviderSessionSnapshot,
    provider_session_key,
)
from deckr.controller._binding_resolution import ResolvedControlBinding
from deckr.controller._binding_validator import (
    BLOCKING_ERROR_CODES,
    format_validation_summary,
    validate_dynamic_page_bindings,
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
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.action_provider.context import ControlContext
from deckr.controller.action_provider.provider import (
    ActionMetadata,
    ActionProviderManager,
)
from deckr.controller.config._data import DeviceConfig, Profile
from deckr.controller.settings import (
    SettingsService,
    derive_action_instance_id,
)

logger = logging.getLogger(__name__)

DEFAULT_WIDGET_TIMEOUT_MS = 60_000
BINDING_SESSION_RECONCILE_SECONDS = 1.0
_ACTION_METADATA_UNSET: Any = object()
_SETTINGS_COMMAND_TYPES = frozenset(
    {
        SETTINGS_REQUEST,
        SETTINGS_PATCH,
        SETTINGS_REPLACE,
    }
)
_IMAGE_SOURCE_SCHEMES = ("data:", "http://", "https://")


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


def _binding_output_image_source(params: Mapping[str, Any]) -> str | None:
    image = params.get("image")
    if not isinstance(image, str) or not image:
        return None
    if image.startswith(_IMAGE_SOURCE_SCHEMES):
        return image
    encoding = params.get("encoding")
    if encoding in {"jpeg", "png"}:
        return f"data:image/{encoding};base64,{image}"
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
    owner_context_id: str
    owner_binding_id: str
    owner_control_id: str
    owner_action_uuid: str
    owner_provider_instance_id: str
    owner_provider_id: str
    owner_provider_session_id: str | None
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
    provider_instance_id: str
    provider_id: str
    provider_session_id: str | None
    provider_session_key: ProviderSessionKey | None
    attached: bool
    control_id: str
    control: ControlSurface
    input_capability_ids: frozenset[str]
    raster_capability_id: str | None
    profile_id: str
    page_id: str
    settings_target: SettingsTargetRef | None
    context: ControlContext
    page_session_id: str | None = None
    item_key: str | None = None
    handler: str | None = None


def _qualified_action_id(provider_instance_id: str, action_uuid: str) -> str:
    return f"{provider_instance_id}::{action_uuid}"


def _lease_matches_action(lease: BindingLease, action_meta: ActionMetadata) -> bool:
    return (
        lease.action_uuid == action_meta.uuid
        and lease.provider_instance_id == action_meta.provider_instance_id
        and lease.provider_id == action_meta.provider_id
        and lease.provider_session_id == action_meta.provider_session_id
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizedCommandTarget:
    sender_provider_instance_id: str
    context_id: str
    binding: BindingLease | None = None
    page_session: DynamicPageSession | None = None


def _binding_body_matches_lease(lease: BindingLease, binding: BindingMetadata) -> bool:
    return (
        binding.context_id == lease.context_id
        and binding.binding_id == lease.binding_id
        and binding.action_instance_id == lease.action_instance_id
    )


class DeviceManager:
    def __init__(
        self,
        *,
        controller_id: str,
        device: DeviceDescriptor,
        hardware_ref: DeviceRef,
        command_service: HardwareCommandService,
        config: DeviceConfig,
        manager: ActionProviderManager,
        actions_bus: Any,
        start_soon: Callable,
        render_backend: RenderBackend | None = None,
        settings_service: SettingsService | None = None,
        config_stream: AsyncIterator[DeviceConfig | None] | None = None,
        on_config_removed: Callable[[str], Awaitable[None]] | None = None,
        provider_sessions: ActionProviderSessionManager | None = None,
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
        self._actions_bus = actions_bus
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
        self._on_config_removed = on_config_removed
        self._provider_sessions = provider_sessions
        self.action_contexts = AsyncMap[str, ControlContext]()
        self._translator = EventTranslator(controller_id=controller_id)
        self._nav = NavigationService(config)
        self._dynamic_page_session: DynamicPageSession | None = None
        self._binding_leases: dict[str, BindingLease] = {}
        self._binding_by_context: dict[str, str] = {}
        self._active_binding_by_control: dict[str, str] = {}
        self._held_input_bindings: dict[tuple[str, str], str] = {}
        self._action_instances: dict[str, ActionInstanceMetadata] = {}
        self._action_instance_providers: dict[str, str] = {}
        self._action_instance_provider_sessions: dict[
            str,
            ProviderSessionKey | None,
        ] = {}
        self._clock = clock or time.monotonic
        self._page_timeout_check_interval = page_timeout_check_interval
        self._nav_lock = anyio.Lock()
        self._start_soon(self._page_timeout_loop)
        if self._provider_sessions is not None:
            self._start_soon(self._binding_session_loop)

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
        notify_provider: bool = True,
        reason: str = "detach",
        clear_held_input: bool = False,
    ) -> BindingLease | None:
        lease = self._binding_leases.pop(binding_id, None)
        if lease is None:
            return None
        if clear_held_input:
            self._clear_held_inputs_for_binding(binding_id)
        self._binding_by_context.pop(lease.context_id, None)
        active_binding = self._active_binding_by_control.get(lease.control_id)
        if active_binding == binding_id:
            self._active_binding_by_control.pop(lease.control_id, None)
            await self.action_contexts.delete(lease.control_id)
        if lease.attached and notify_provider:
            await lease.context.on_binding_detached(reason)
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

    def _clear_held_inputs_for_binding(self, binding_id: str) -> None:
        for key, held_binding_id in tuple(self._held_input_bindings.items()):
            if held_binding_id == binding_id:
                self._held_input_bindings.pop(key, None)

    async def _revoke_active_bindings(self, *, clear_outputs: bool = True) -> None:
        await self._revoke_active_bindings_except(
            clear_outputs=clear_outputs,
            preserve_output_control_ids=frozenset(),
        )

    async def _revoke_active_bindings_except(
        self,
        *,
        clear_outputs: bool = True,
        preserve_output_control_ids: frozenset[str],
    ) -> None:
        for binding_id in list(self._binding_leases):
            lease = self._binding_leases.get(binding_id)
            clear_output = clear_outputs
            if lease is not None and lease.control_id in preserve_output_control_ids:
                clear_output = False
            await self._revoke_binding(binding_id, clear_output=clear_output)

    async def _clear_all_raster_controls(
        self,
        *,
        preserve_control_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Clear raster-capable controls before rendering a new page."""
        for control in raster_controls(self.device):
            if control.control_id in preserve_control_ids:
                continue
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
        provider_instance_id: str,
        provider_id: str,
    ) -> SettingsTargetRef:
        return SettingsTargetRef(
            scope="action_instance",
            controllerId=self._controller_id,
            configId=self.config_id,
            providerInstanceId=provider_instance_id,
            providerId=provider_id,
            actionId=binding.action_uuid,
            actionInstanceId=action_instance_id,
            stableId=binding.stable_id,
        )

    def _dynamic_child_action_instance_id(
        self,
        *,
        page_session: DynamicPageSession,
        child: PageChildBindingDescriptor,
        binding: ResolvedControlBinding,
    ) -> str:
        if child.target.kind == "self":
            return page_session.action_instance_id

        provider_key = binding.provider_instance_id or ""
        if binding.provider_labels:
            provider_key = "|".join(
                (
                    provider_key,
                    *(
                        f"{key}={value}"
                        for key, value in sorted(binding.provider_labels.items())
                    ),
                )
            )
        target_key = child.target.instance_key or child.control_id
        stable_id = "\x1f".join(
            (
                "dynamic-page",
                page_session.page_session_id,
                provider_key,
                target_key,
            )
        )
        return derive_action_instance_id(
            controller_id=self._controller_id,
            config_id=self.config_id,
            action_id=binding.action_uuid,
            stable_id=stable_id,
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

    def _action_metadata_with_current_session(
        self,
        action_meta: ActionMetadata,
    ) -> ActionMetadata:
        registry_session = self.manager.provider_session_id(
            action_meta.provider_instance_id
        )
        if not isinstance(registry_session, str):
            registry_session = None
        provider_session_id = registry_session or action_meta.provider_session_id
        if provider_session_id == action_meta.provider_session_id:
            return action_meta
        return replace(action_meta, provider_session_id=provider_session_id)

    def _binding_lease_for_control(self, control_id: str) -> BindingLease | None:
        active_binding_id = self._active_binding_by_control.get(control_id)
        if active_binding_id is not None:
            lease = self._binding_leases.get(active_binding_id)
            if lease is not None:
                return lease
        for lease in self._binding_leases.values():
            if lease.control_id == control_id:
                return lease
        return None

    async def _revoke_binding_for_action_session_change(
        self,
        lease: BindingLease,
        *,
        action_meta: ActionMetadata,
    ) -> None:
        logger.info(
            "Rebinding control after action provider session changed "
            "config=%s control=%s action=%s provider=%s oldSession=%s newSession=%s",
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.provider_session_id,
            action_meta.provider_session_id,
        )
        old_session_key = lease.provider_session_key
        await self._revoke_binding(
            lease.binding_id,
            clear_output=False,
            notify_provider=False,
            reason="provider_session_changed",
            clear_held_input=True,
        )
        await self._destroy_action_instance(
            lease.action_instance_id,
            reason="provider_session_changed",
            notify_provider=False,
        )
        if self._provider_sessions is None or old_session_key is None:
            return
        retire = getattr(self._provider_sessions, "retire", None)
        if retire is not None:
            await retire(old_session_key, reason="provider_session_changed")

    async def _prepare_provider_sessions_for_actions(
        self,
        actions: list[ActionMetadata | None],
    ) -> tuple[
        list[ActionMetadata | None],
        dict[ProviderSessionKey, ProviderSessionSnapshot],
    ]:
        prepared_actions = [
            None if action is None else self._action_metadata_with_current_session(action)
            for action in actions
        ]
        if self._provider_sessions is None:
            return prepared_actions, {}
        snapshots = await self._provider_sessions.prepare_many(
            action
            for action in prepared_actions
            if action is not None
            and action.provider_instance_id != BUILTIN_ACTION_PROVIDER_ID
        )
        return prepared_actions, snapshots

    def _provider_session_snapshot_for_action(
        self,
        action_meta: ActionMetadata | None,
        snapshots: Mapping[ProviderSessionKey, ProviderSessionSnapshot],
    ) -> ProviderSessionSnapshot | None:
        if action_meta is None:
            return None
        key = provider_session_key(action_meta)
        return snapshots.get(key) if key is not None else None

    def _binding_session_ready(
        self,
        lease: BindingLease,
        *,
        snapshot: ProviderSessionSnapshot | None = None,
    ) -> bool:
        if (
            self._provider_sessions is None
            or lease.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
        ):
            return True
        key = lease.provider_session_key
        if key is None:
            return False
        if snapshot is not None and snapshot.key == key:
            return snapshot.ready
        return self._provider_sessions.cached_ready(key)

    async def _activate_binding(
        self,
        lease: BindingLease,
        *,
        provider_session_snapshot: ProviderSessionSnapshot | None = None,
    ) -> bool:
        if lease.attached:
            return True
        if not self._binding_session_ready(
            lease,
            snapshot=provider_session_snapshot,
        ):
            return False
        await self._ensure_action_instance(
            action_meta=ActionMetadata(
                uuid=lease.action_uuid,
                provider_instance_id=lease.provider_instance_id,
                provider_id=lease.provider_id,
                provider_session_id=lease.provider_session_id,
            ),
            action_instance_id=lease.action_instance_id,
            context_id=lease.context_id,
            settings=lease.context.settings,
        )
        self._binding_by_context[lease.context_id] = lease.binding_id
        self._active_binding_by_control[lease.control_id] = lease.binding_id
        await self.action_contexts.set(lease.control_id, lease.context)
        lease.attached = True
        await lease.context.on_binding_attached()
        return True

    async def _binding_session_loop(self) -> None:
        while True:
            await anyio.sleep(BINDING_SESSION_RECONCILE_SECONDS)
            await self._reconcile_binding_sessions()

    async def _reconcile_binding_sessions(self) -> None:
        leases = tuple(self._binding_leases.values())
        snapshots: dict[ProviderSessionKey, ProviderSessionSnapshot] = {}
        if self._provider_sessions is not None:
            keys = {
                lease.provider_session_key
                for lease in leases
                if lease.provider_session_key is not None
            }
            snapshots = await self._provider_sessions.refresh_many(keys)

        for lease in leases:
            snapshot = (
                snapshots.get(lease.provider_session_key)
                if lease.provider_session_key is not None
                else None
            )
            if snapshot is not None and snapshot.terminal:
                await self._cleanup_terminal_provider_session(snapshot)

        leases = tuple(self._binding_leases.values())
        for lease in leases:
            snapshot = (
                snapshots.get(lease.provider_session_key)
                if lease.provider_session_key is not None
                else None
            )
            valid = self._binding_session_ready(lease, snapshot=snapshot)
            if valid:
                await self._activate_binding(
                    lease,
                    provider_session_snapshot=snapshot,
                )
                continue
            if lease.attached or (snapshot is not None and snapshot.terminal):
                await self._revoke_binding(lease.binding_id)

    def _dynamic_page_owned_by_provider_session(
        self,
        session: DynamicPageSession,
        key: ProviderSessionKey,
    ) -> bool:
        return (
            session.owner_provider_instance_id == key.provider_instance_id
            and session.owner_provider_id == key.provider_id
            and session.owner_provider_session_id == key.provider_session_id
        )

    async def _cleanup_terminal_provider_session(
        self,
        snapshot: ProviderSessionSnapshot,
    ) -> None:
        key = snapshot.key
        reason = snapshot.reason or "provider_session_terminal"
        closed_page_session_id: str | None = None
        page_session = self._dynamic_page_session
        if page_session is not None and self._dynamic_page_owned_by_provider_session(
            page_session,
            key,
        ):
            closed_page_session_id = page_session.page_session_id
            self._dynamic_page_session = None
            if isinstance(self._nav.current_page, DynamicPageCommand):
                self._nav.set_page(
                    StaticPageRef(
                        profile_name=page_session.owner_profile,
                        page_index=page_session.owner_page,
                    )
                )

        binding_ids: list[str] = []
        for binding_id, lease in tuple(self._binding_leases.items()):
            if lease.provider_session_key == key:
                binding_ids.append(binding_id)
                continue
            if (
                closed_page_session_id is not None
                and lease.page_session_id == closed_page_session_id
            ):
                binding_ids.append(binding_id)

        for binding_id in binding_ids:
            lease = self._binding_leases.get(binding_id)
            notify_provider = lease is not None and lease.provider_session_key != key
            await self._revoke_binding(
                binding_id,
                notify_provider=notify_provider,
                reason=reason,
                clear_held_input=True,
            )

        for action_instance_id, owned_session_key in tuple(
            self._action_instance_provider_sessions.items()
        ):
            if owned_session_key == key:
                await self._destroy_action_instance(
                    action_instance_id,
                    reason=reason,
                    notify_provider=False,
                )

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
            providerInstanceId=action_meta.provider_instance_id,
            providerId=action_meta.provider_id,
            actionId=action_meta.uuid,
            actionInstanceId=action_instance_id,
            configId=self.config_id,
            contextId=context_id,
        )
        self._action_instances[action_instance_id] = metadata
        self._action_instance_providers[action_instance_id] = (
            action_meta.provider_instance_id
        )
        self._action_instance_provider_sessions[action_instance_id] = (
            None
            if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            else provider_session_key(action_meta)
        )
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        msg = action_message(
            sender=controller_address(self._controller_id),
            sender_session_id=self._actions_bus.session_id,
            recipient=action_provider_address(action_meta.provider_instance_id),
            message_type=ACTION_INSTANCE_CREATED,
            body=ActionInstanceLifecycleBody(
                metadata=metadata,
                settings=dict(settings),
            ),
            subject=context_subject(
                context_id,
                provider_instance_id=action_meta.provider_instance_id,
                provider_id=action_meta.provider_id,
                config_id=self.config_id,
                action_instance_id=action_instance_id,
            ),
        )
        await self._actions_bus.publish(msg)

    async def _destroy_action_instance(
        self,
        action_instance_id: str,
        *,
        reason: str,
        notify_provider: bool = True,
    ) -> None:
        metadata = self._action_instances.pop(action_instance_id, None)
        provider_instance_id = self._action_instance_providers.pop(
            action_instance_id,
            None,
        )
        self._action_instance_provider_sessions.pop(action_instance_id, None)
        if (
            metadata is None
            or provider_instance_id is None
            or provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            or not notify_provider
        ):
            return
        msg = action_message(
            sender=controller_address(self._controller_id),
            sender_session_id=self._actions_bus.session_id,
            recipient=action_provider_address(provider_instance_id),
            message_type=ACTION_INSTANCE_DESTROYED,
            body=ActionInstanceLifecycleBody(metadata=metadata, reason=reason),
            subject=context_subject(
                metadata.context_id or "",
                provider_instance_id=metadata.provider_instance_id,
                provider_id=metadata.provider_id,
                config_id=self.config_id,
                action_instance_id=metadata.action_instance_id,
            ),
        )
        await self._actions_bus.publish(msg)

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
        item_key: str | None = None,
        handler: str | None = None,
        action_meta: ActionMetadata | None = _ACTION_METADATA_UNSET,
        provider_session_snapshot: ProviderSessionSnapshot | None = None,
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
        if action_meta is _ACTION_METADATA_UNSET:
            action_meta = await self.manager.get_action(
                binding.action_uuid,
                provider_instance_id=binding.provider_instance_id,
                provider_labels=binding.provider_labels,
            )
        if action_meta is None:
            logger.info(
                "Binding unresolved on profile=%s page=%s control=%s action=%s",
                profile_id,
                page_id,
                binding.control_id,
                binding.action_uuid,
            )
            return False
        action_meta = self._action_metadata_with_current_session(action_meta)
        provider_session_id = action_meta.provider_session_id
        session_key = (
            None
            if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            else provider_session_key(action_meta)
        )
        if provider_session_snapshot is not None and provider_session_snapshot.terminal:
            return False
        settings_target = (
            self._build_settings_target_for_binding(
                action_instance_id=action_instance_id,
                binding=binding,
                provider_instance_id=action_meta.provider_instance_id,
                provider_id=action_meta.provider_id,
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
        if (
            action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            and hasattr(self.manager, "get_builtin_action")
        ):
            builtin_action = self.manager.get_builtin_action(action_meta.uuid)
        binding_id = make_binding_id()
        context_id = make_context_id()
        matched_capabilities = self._matched_capabilities(binding)
        binding_metadata = BindingMetadata(
            providerInstanceId=action_meta.provider_instance_id,
            providerId=action_meta.provider_id,
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
            itemKey=item_key,
            handler=handler,
            matchedCapabilities=matched_capabilities,
        )
        ctx = ControlContext(
            controller_id=self._controller_id,
            device=self.device,
            config_id=self.config_id,
            command_service=self._command_service,
            provider_instance_id=action_meta.provider_instance_id,
            provider_id=action_meta.provider_id,
            action_uuid=action_meta.uuid,
            control=control,
            settings=initial_settings,
            manager=self,
            actions_bus=self._actions_bus,
            start_soon=self._start_soon,
            render_dispatcher=self._render_dispatcher,
            settings_service=self._settings_service,
            context_settings_target=settings_target,
            profile_id=profile_id,
            page_id=page_id,
            builtin_action=builtin_action,
            metadata=binding_metadata,
        )
        lease = BindingLease(
            binding_id=binding_id,
            context_id=context_id,
            action_instance_id=action_instance_id,
            action_uuid=action_meta.uuid,
            provider_instance_id=action_meta.provider_instance_id,
            provider_id=action_meta.provider_id,
            provider_session_id=provider_session_id,
            provider_session_key=session_key,
            attached=False,
            control_id=control.id,
            control=control,
            input_capability_ids=binding.input_capability_ids,
            raster_capability_id=raster_capability_id,
            profile_id=profile_id,
            page_id=page_id,
            settings_target=settings_target,
            context=ctx,
            page_session_id=page_session_id,
            item_key=item_key,
            handler=handler,
        )
        self._binding_leases[binding_id] = lease
        attached = await self._activate_binding(
            lease,
            provider_session_snapshot=provider_session_snapshot,
        )
        if attached:
            logger.debug(
                "Binding resolved on profile=%s page=%s control=%s action=%s provider=%s binding=%s",
                profile_id,
                page_id,
                binding.control_id,
                binding.action_uuid,
                action_meta.provider_instance_id,
                binding_id,
            )
            return True
        logger.debug(
            "Binding pending provider session on profile=%s page=%s control=%s action=%s provider=%s binding=%s",
            profile_id,
            page_id,
            binding.control_id,
            binding.action_uuid,
            action_meta.provider_instance_id,
            binding_id,
        )
        return False

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
            if lease.attached and lease.page_session_id == session.page_session_id
        )
        return PageSessionMetadata(
            providerInstanceId=session.owner_provider_instance_id,
            providerId=session.owner_provider_id,
            actionInstanceId=session.action_instance_id,
            configId=self.config_id,
            pageId=session.page_id,
            pageSessionId=session.page_session_id,
            contextId=session.context_id,
            ownerBindingId=session.owner_binding_id,
            bindings=bindings,
        )

    async def _emit_page_opened(
        self,
        session: DynamicPageSession,
        *,
        causation_id: str | None = None,
    ) -> None:
        if session.owner_provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        msg = action_message(
            sender=controller_address(self._controller_id),
            sender_session_id=self._actions_bus.session_id,
            recipient=action_provider_address(session.owner_provider_instance_id),
            message_type=PAGE_SESSION_OPENED,
            body=PageSessionLifecycleBody(
                pageSession=self._page_session_metadata(session)
            ),
            subject=context_subject(
                session.context_id,
                provider_instance_id=session.owner_provider_instance_id,
                provider_id=session.owner_provider_id,
                config_id=self.config_id,
                action_instance_id=session.action_instance_id,
                page_session_id=session.page_session_id,
            ),
            causation_id=causation_id,
        )
        await self._actions_bus.publish(msg)

    async def _emit_page_closed(
        self,
        session: DynamicPageSession,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> None:
        if session.owner_provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        msg = action_message(
            sender=controller_address(self._controller_id),
            sender_session_id=self._actions_bus.session_id,
            recipient=action_provider_address(session.owner_provider_instance_id),
            message_type=PAGE_SESSION_CLOSED,
            body=PageSessionLifecycleBody(
                pageSession=self._page_session_metadata(session),
                reason=reason,
            ),
            subject=context_subject(
                session.context_id,
                provider_instance_id=session.owner_provider_instance_id,
                provider_id=session.owner_provider_id,
                config_id=self.config_id,
                action_instance_id=session.action_instance_id,
                page_session_id=session.page_session_id,
            ),
            causation_id=causation_id,
        )
        await self._actions_bus.publish(msg)

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
        preserve_rebound_outputs: bool = False,
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
            result = await validate_dynamic_page_bindings(
                list(arriving.bindings),
                self.device,
                self.manager.get_action,
                owner_action_uuid=page_session.owner_action_uuid,
                owner_provider_instance_id=page_session.owner_provider_instance_id,
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

        prepared_actions, provider_session_snapshots = (
            await self._prepare_provider_sessions_for_actions(result.actions)
        )

        preserve_output_control_ids = (
            frozenset(binding.control_id for binding in result.bindings)
            if preserve_rebound_outputs
            else frozenset()
        )

        if transition.departing is not None:
            await self._revoke_active_bindings_except(
                preserve_output_control_ids=preserve_output_control_ids,
            )

        await self._clear_all_raster_controls(
            preserve_control_ids=preserve_output_control_ids,
        )

        if isinstance(arriving, StaticPageRef):
            for binding, action_meta in zip(
                result.bindings,
                prepared_actions,
                strict=True,
            ):
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
                    action_meta=action_meta,
                    provider_session_snapshot=self._provider_session_snapshot_for_action(
                        action_meta,
                        provider_session_snapshots,
                    ),
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
            resolved_count = 0
            unavailable_count = 0

            async def resolve_dynamic_child(
                child: PageChildBindingDescriptor,
                binding: ResolvedControlBinding,
                action_meta: ActionMetadata | None,
            ) -> None:
                nonlocal resolved_count, unavailable_count
                if not await self._try_resolve_binding(
                    binding,
                    profile_id="_dynamic",
                    page_id=arriving.page_id,
                    action_instance_id=self._dynamic_child_action_instance_id(
                        page_session=page_session,
                        child=child,
                        binding=binding,
                    ),
                    page_session_id=page_session.page_session_id,
                    persist_settings=False,
                    item_key=child.item_key,
                    handler=child.handler,
                    action_meta=action_meta,
                    provider_session_snapshot=self._provider_session_snapshot_for_action(
                        action_meta,
                        provider_session_snapshots,
                    ),
                ):
                    unavailable_count += 1
                    control = _find_control_surface(
                        self.device,
                        binding.control_id,
                        raster_capability_id=_selected_raster_capability_id(binding),
                    )
                    if control is not None:
                        await self._render_unavailable_to_control(control)
                    return
                resolved_count += 1

            async with anyio.create_task_group() as task_group:
                for child, binding, action_meta in zip(
                    arriving.bindings,
                    result.bindings,
                    prepared_actions,
                    strict=True,
                ):
                    task_group.start_soon(
                        resolve_dynamic_child,
                        child,
                        binding,
                        action_meta,
                    )
            logger.info(
                "Dynamic page bindings resolved on page=%s resolved=%s unavailable=%s",
                arriving.page_id,
                resolved_count,
                unavailable_count,
            )
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
            preserve_rebound_outputs=(
                page_session is not None
                and isinstance(transition.departing, DynamicPageCommand)
                and isinstance(transition.arriving, DynamicPageCommand)
            ),
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
        """Open or claim the dynamic page context for the sending action context."""
        if not descriptor or not descriptor.bindings:
            return

        async with self._nav_lock:
            current = self._dynamic_page_session
            binding_id = self._binding_by_context.get(context_id)
            owner_lease = (
                self._binding_leases.get(binding_id) if binding_id is not None else None
            )
            if owner_lease is None and current is None:
                logger.warning("open_page ignored: no active context for %s", context_id)
                return

            if owner_lease is not None:
                try:
                    owner_page = int(owner_lease.page_id)
                except ValueError:
                    owner_page = current.owner_page if current is not None else 0
                if owner_lease.page_session_id is not None and current is not None:
                    owner_profile = current.owner_profile
                    owner_page = current.owner_page
                else:
                    owner_profile = owner_lease.profile_id
                timeout_ms = self._resolve_widget_timeout_ms(owner_profile, owner_page)
                owner_context_id = context_id
                owner_binding_id = owner_lease.binding_id
                owner_control_id = owner_lease.control_id
                owner_action_uuid = owner_lease.action_uuid
                owner_provider_instance_id = owner_lease.provider_instance_id
                owner_provider_id = owner_lease.provider_id
                owner_provider_session_id = owner_lease.provider_session_id
                action_instance_id = owner_lease.action_instance_id
                settings_target = owner_lease.settings_target
            elif current is not None and context_id == current.context_id:
                owner_profile = current.owner_profile
                owner_page = current.owner_page
                timeout_ms = current.timeout_ms
                owner_context_id = current.owner_context_id
                owner_binding_id = current.owner_binding_id
                owner_control_id = current.owner_control_id
                owner_action_uuid = current.owner_action_uuid
                owner_provider_instance_id = current.owner_provider_instance_id
                owner_provider_id = current.owner_provider_id
                owner_provider_session_id = current.owner_provider_session_id
                action_instance_id = current.action_instance_id
                settings_target = current.settings_target
            else:
                logger.warning("open_page ignored: no active context for %s", context_id)
                return

            page_id = descriptor.page_id or make_dynamic_page_id()
            descriptor = DynamicPageCommand(
                pageId=page_id,
                bindings=descriptor.bindings,
            )

            session = DynamicPageSession(
                page_id=page_id,
                page_session_id=make_page_session_id(),
                context_id=make_context_id(),
                action_instance_id=action_instance_id,
                owner_context_id=owner_context_id,
                owner_binding_id=owner_binding_id,
                owner_control_id=owner_control_id,
                owner_action_uuid=owner_action_uuid,
                owner_provider_instance_id=owner_provider_instance_id,
                owner_provider_id=owner_provider_id,
                owner_provider_session_id=owner_provider_session_id,
                owner_profile=owner_profile,
                owner_page=owner_page,
                timeout_ms=timeout_ms,
                last_activity=self._clock(),
                settings_target=settings_target,
            )

            ok = await self._set_page_locked(
                descriptor=descriptor,
                page_session=session,
                close_dynamic=False,
            )
            if ok:
                if current is not None:
                    reason = (
                        "replaced"
                        if current.owner_binding_id == session.owner_binding_id
                        else "dismissed"
                    )
                    await self._emit_page_closed(
                        current,
                        reason=reason,
                        causation_id=causation_id,
                    )
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
        if lease.provider_instance_id != session.owner_provider_instance_id:
            return None
        return session

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
            if descriptor.page_id != current.page_id:
                logger.warning(
                    "replace_page ignored: descriptor page %s does not match session page %s",
                    descriptor.page_id,
                    current.page_id,
                )
                return
            replacement = DynamicPageCommand(
                pageId=current.page_id,
                bindings=descriptor.bindings,
            )
            ok = await self._set_page_locked(
                descriptor=replacement,
                page_session=current,
                close_dynamic=False,
            )
            if ok:
                current.last_activity = self._clock()

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
            self._held_input_bindings.clear()
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

        registered/unregistered carry qualified provider-instance action IDs.
        """
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
            dynamic_page_session = None
            persist_settings = True
        else:
            session = self._dynamic_page_session
            if session is None:
                return
            result = await validate_dynamic_page_bindings(
                list(current_page.bindings),
                self.device,
                self.manager.get_action,
                owner_action_uuid=session.owner_action_uuid,
                owner_provider_instance_id=session.owner_provider_instance_id,
                profile_id="_dynamic",
                page_id=current_page.page_id,
            )
            profile_id = "_dynamic"
            page_id = current_page.page_id
            page_session_id = session.page_session_id
            action_instance_id = session.action_instance_id
            dynamic_page_session = session
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

        changed_action_ids = set(registered) & set(unregistered)
        current_actions = [
            None if action is None else self._action_metadata_with_current_session(action)
            for action in result.actions
        ]
        rebind_control_ids: set[str] = set()
        for binding, action_meta in zip(
            result.bindings,
            current_actions,
            strict=True,
        ):
            lease = self._binding_lease_for_control(binding.control_id)
            if lease is None:
                continue
            if (
                _qualified_action_id(lease.provider_instance_id, lease.action_uuid)
                not in changed_action_ids
            ):
                continue
            if action_meta is None or _lease_matches_action(lease, action_meta):
                continue
            rebind_control_ids.add(binding.control_id)

        leased_control_ids = {
            lease.control_id for lease in self._binding_leases.values()
        }
        actions_needing_lease = [
            action
            if binding.control_id in rebind_control_ids
            or binding.control_id not in leased_control_ids
            else None
            for binding, action in zip(result.bindings, current_actions, strict=True)
        ]
        prepared_actions, provider_session_snapshots = (
            await self._prepare_provider_sessions_for_actions(actions_needing_lease)
        )
        if any(not lease.attached for lease in self._binding_leases.values()):
            await self._reconcile_binding_sessions()

        child_bindings = (
            current_page.bindings
            if isinstance(current_page, DynamicPageCommand)
            else (None,) * len(result.bindings)
        )
        for child, binding, action_meta in zip(
            child_bindings,
            result.bindings,
            prepared_actions,
            strict=True,
        ):
            if binding.control_id in rebind_control_ids:
                lease = self._binding_lease_for_control(binding.control_id)
                if lease is not None and action_meta is not None:
                    await self._revoke_binding_for_action_session_change(
                        lease,
                        action_meta=action_meta,
                    )
            if await self.action_contexts.has_key(binding.control_id):
                continue  # Already has context
            if any(
                lease.control_id == binding.control_id
                for lease in self._binding_leases.values()
            ):
                continue
            if child is not None and dynamic_page_session is not None:
                resolved_action_instance_id = self._dynamic_child_action_instance_id(
                    page_session=dynamic_page_session,
                    child=child,
                    binding=binding,
                )
            else:
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
                item_key=child.item_key if child is not None else None,
                handler=child.handler if child is not None else None,
                action_meta=action_meta,
                provider_session_snapshot=self._provider_session_snapshot_for_action(
                    action_meta,
                    provider_session_snapshots,
                ),
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
            if self._on_config_removed is not None:
                await self._on_config_removed(self.config_id)
            return
        async with self._nav_lock:
            self._held_input_bindings.clear()
            self.config = config
            if self._dynamic_page_session is not None:
                await self._finalize_dynamic_page(reason="config_change")
            await self._destroy_all_action_instances(reason="config_change")
            transition = self._nav.update_config(config)
            await self._execute_transition(transition)

    def _command_sender_provider_instance_id(self, msg: DeckrMessage) -> str | None:
        provider_instance_id = parse_action_provider_address(msg.sender)
        if provider_instance_id is None:
            logger.warning(
                "Ignoring action command %s from non-provider sender %s",
                msg.message_type,
                msg.sender,
            )
            return None
        if provider_instance_id in RESERVED_BUILTIN_PROVIDER_IDS:
            logger.warning(
                "Ignoring action command %s from external provider using reserved id %s",
                msg.message_type,
                provider_instance_id,
            )
            return None
        return provider_instance_id

    async def _authorize_action_command(
        self,
        msg: DeckrMessage,
        *,
        context_id: str,
    ) -> AuthorizedCommandTarget | None:
        sender_provider_instance_id = self._command_sender_provider_instance_id(msg)
        if sender_provider_instance_id is None:
            return None

        action_instance_id = subject_action_instance_id(msg.subject)
        binding_id = subject_binding_id(msg.subject)
        page_session_id = subject_page_session_id(msg.subject)
        subject_provider_id = subject_provider_instance_id(msg.subject)
        if (
            subject_provider_id is not None
            and subject_provider_id != sender_provider_instance_id
        ):
            logger.warning(
                "Ignoring action command %s from %s with mismatched subject provider %s",
                msg.message_type,
                msg.sender,
                subject_provider_id,
            )
            return None

        if binding_id is not None:
            lease = self._binding_leases.get(binding_id)
            if lease is None or lease.context_id != context_id:
                logger.warning(
                    "Ignoring action command %s from %s for inactive binding %s",
                    msg.message_type,
                    msg.sender,
                    binding_id,
                )
                return None
            active_binding_id = self._active_binding_by_control.get(lease.control_id)
            if active_binding_id != binding_id:
                logger.warning(
                    "Ignoring action command %s for inactive control binding %s",
                    msg.message_type,
                    binding_id,
                )
                return None
            if not lease.attached:
                logger.warning(
                    "Ignoring action command %s for pending binding %s",
                    msg.message_type,
                    binding_id,
                )
                return None
            if sender_provider_instance_id != lease.provider_instance_id:
                logger.warning(
                    "Ignoring action command %s from provider %s for binding owned by provider %s",
                    msg.message_type,
                    sender_provider_instance_id,
                    lease.provider_instance_id,
                )
                return None
            if msg.sender_session_id != lease.provider_session_id:
                logger.warning(
                    "Ignoring action command %s from stale provider session %s",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if not self._binding_session_ready(lease):
                logger.warning(
                    "Ignoring action command %s for invalid provider session %s",
                    msg.message_type,
                    lease.provider_session_id,
                )
                return None
            if (
                action_instance_id is not None
                and action_instance_id != lease.action_instance_id
            ):
                logger.warning(
                    "Ignoring action command %s for mismatched action instance %s",
                    msg.message_type,
                    action_instance_id,
                )
                return None
            if page_session_id is not None and page_session_id != lease.page_session_id:
                logger.warning(
                    "Ignoring action command %s for mismatched page session %s",
                    msg.message_type,
                    page_session_id,
                )
                return None
            return AuthorizedCommandTarget(
                sender_provider_instance_id=sender_provider_instance_id,
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
                    "Ignoring action command %s for inactive page session %s",
                    msg.message_type,
                    page_session_id,
                )
                return None
            if sender_provider_instance_id != session.owner_provider_instance_id:
                logger.warning(
                    "Ignoring action command %s from provider %s for page owned by provider %s",
                    msg.message_type,
                    sender_provider_instance_id,
                    session.owner_provider_instance_id,
                )
                return None
            if msg.sender_session_id != session.owner_provider_session_id:
                logger.warning(
                    "Ignoring page action command %s from stale provider session %s",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if (
                self._provider_sessions is not None
                and session.owner_provider_instance_id != BUILTIN_ACTION_PROVIDER_ID
            ):
                if session.owner_provider_session_id is None:
                    logger.warning(
                        "Ignoring page action command %s without provider session",
                        msg.message_type,
                    )
                    return None
                owner_key = ProviderSessionKey(
                    session.owner_provider_instance_id,
                    session.owner_provider_id,
                    session.owner_provider_session_id,
                )
                if not self._provider_sessions.cached_ready(owner_key):
                    logger.warning(
                        "Ignoring page action command %s for invalid provider session %s",
                        msg.message_type,
                        session.owner_provider_session_id,
                    )
                    return None
            if (
                action_instance_id is not None
                and action_instance_id != session.action_instance_id
            ):
                logger.warning(
                    "Ignoring action command %s for mismatched page action instance %s",
                    msg.message_type,
                    action_instance_id,
                )
                return None
            return AuthorizedCommandTarget(
                sender_provider_instance_id=sender_provider_instance_id,
                context_id=context_id,
                page_session=session,
            )

        logger.warning(
            "Ignoring action command %s from %s without binding or page session subject",
            msg.message_type,
            msg.sender,
        )
        return None

    def _settings_target_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        msg_type: str,
        sender: object,
    ) -> SettingsTargetRef | None:
        target_data = payload.get("target")
        if not isinstance(target_data, Mapping):
            logger.warning(
                "Ignoring invalid settings command %s from %s without target object",
                msg_type,
                sender,
            )
            return None
        try:
            return SettingsTargetRef.model_validate(target_data)
        except (ValidationError, ValueError):
            logger.warning(
                "Ignoring invalid settings target for %s from %s",
                msg_type,
                sender,
                exc_info=True,
            )
            return None

    async def _provider_settings_authorized(
        self,
        *,
        sender_provider_instance_id: str,
        sender_session_id: str | None,
        target: SettingsTargetRef,
    ) -> bool:
        if target.provider_instance_id != sender_provider_instance_id:
            logger.warning(
                "Ignoring provider settings command from %s for provider instance %s",
                sender_provider_instance_id,
                target.provider_instance_id,
            )
            return False
        if self._provider_sessions is None:
            logger.warning(
                "Ignoring provider settings command from %s without provider session manager",
                sender_provider_instance_id,
            )
            return False
        if sender_session_id is None:
            logger.warning(
                "Ignoring provider settings command from %s without sender session",
                sender_provider_instance_id,
            )
            return False
        if not await self._provider_sessions.valid(
            provider_instance_id=sender_provider_instance_id,
            provider_id=target.provider_id,
            provider_session_id=sender_session_id,
        ):
            logger.warning(
                "Ignoring provider settings command from %s with invalid provider session %s",
                sender_provider_instance_id,
                sender_session_id,
            )
            return False
        return True

    async def _settings_snapshot_for_command(
        self,
        *,
        msg_type: str,
        target: SettingsTargetRef,
        payload: Mapping[str, Any],
    ) -> SettingsSnapshot | None:
        if self._settings_service is None:
            return None
        try:
            if msg_type == SETTINGS_REQUEST:
                snapshot = await self._settings_service.get(target)
            elif msg_type == SETTINGS_PATCH:
                body = SettingsPatchBody.model_validate(payload)
                snapshot = await self._settings_service.patch(target, body.settings)
            else:
                body = SettingsReplaceBody.model_validate(payload)
                snapshot = await self._settings_service.replace(target, body.settings)
        except (KeyError, ValidationError, ValueError):
            logger.warning(
                "Ignoring invalid settings command %s for target %s",
                msg_type,
                target.key(),
                exc_info=True,
            )
            return None
        return SettingsSnapshot.from_snapshot(snapshot)

    async def handle_command(self, msg: DeckrMessage) -> None:
        """Handle a canonical command message from an action provider."""
        try:
            payload = action_body_dict(msg)
        except (ValidationError, ValueError):
            logger.warning(
                "Ignoring invalid action command body %s from %s",
                msg.message_type,
                msg.sender,
                exc_info=True,
            )
            return
        msg_type = msg.message_type
        settings_target: SettingsTargetRef | None = None

        async def send_settings_response(snapshot_body: SettingsSnapshot) -> None:
            await self._actions_bus.reply_to(
                msg,
                message_type=SETTINGS_SNAPSHOT,
                body=snapshot_body.to_dict(),
                subject=msg.subject,
            )

        if msg_type in _SETTINGS_COMMAND_TYPES:
            sender_provider_instance_id = self._command_sender_provider_instance_id(msg)
            if sender_provider_instance_id is None:
                return
            settings_target = self._settings_target_from_payload(
                payload,
                msg_type=msg_type,
                sender=msg.sender,
            )
            if settings_target is None:
                return
            if settings_target.config_id != self.config_id:
                logger.warning(
                    "Ignoring settings command %s from %s for config %s on manager config %s",
                    msg_type,
                    msg.sender,
                    settings_target.config_id,
                    self.config_id,
                )
                return
            if settings_target.scope == "action_provider_instance":
                if not await self._provider_settings_authorized(
                    sender_provider_instance_id=sender_provider_instance_id,
                    sender_session_id=msg.sender_session_id,
                    target=settings_target,
                ):
                    return
                snapshot_body = await self._settings_snapshot_for_command(
                    msg_type=msg_type,
                    target=settings_target,
                    payload=payload,
                )
                if snapshot_body is not None:
                    await send_settings_response(snapshot_body)
                return

        context_id = subject_context_id(msg.subject) or ""
        if not context_id:
            return
        config_id = subject_config_id(msg.subject)
        if config_id != self.config_id:
            return
        authorization = await self._authorize_action_command(
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

        if msg_type == BINDING_OVERLAY:
            if authorization.binding is not None:
                body = BindingOverlayBody.model_validate(payload)
                await self._handle_binding_overlay(authorization.binding, body)
            return

        if msg_type == BINDING_OVERLAY_CLEAR:
            if authorization.binding is not None:
                body = BindingOverlayClearBody.model_validate(payload)
                await self._handle_binding_overlay_clear(authorization.binding, body)
            return

        page_session = authorization.page_session
        if page_session is not None:
            if msg_type in _SETTINGS_COMMAND_TYPES:
                if self._settings_service is None or page_session.settings_target is None:
                    return
                target = settings_target
                if target is None:
                    return
                if target.key() != page_session.settings_target.key():
                    logger.warning("Ignoring settings command for mismatched page target")
                    return
                snapshot_body = await self._settings_snapshot_for_command(
                    msg_type=msg_type,
                    target=target,
                    payload=payload,
                )
                if snapshot_body is not None:
                    await send_settings_response(snapshot_body)
            return

        lease = authorization.binding
        if lease is None:
            return

        if msg_type in _SETTINGS_COMMAND_TYPES:
            if self._settings_service is None or lease.settings_target is None:
                return
            target = settings_target
            if target is None:
                return
            if target.key() != lease.settings_target.key():
                logger.warning("Ignoring settings command for mismatched binding target")
                return
            snapshot_body = await self._settings_snapshot_for_command(
                msg_type=msg_type,
                target=target,
                payload=payload,
            )
            if snapshot_body is None:
                return
            lease.context._store.settings = dict(thaw_json(snapshot_body.settings))
            await send_settings_response(snapshot_body)
    async def _handle_binding_output(
        self,
        lease: BindingLease,
        body: BindingOutputBody,
    ) -> None:
        if not _binding_body_matches_lease(lease, body.binding):
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
            try:
                params = raster_bitmap_command_params(body.command_type, body.params)
            except (ValueError, ValidationError) as exc:
                logger.warning(
                    "Ignoring invalid raster output command %s on binding %s: %s",
                    body.command_type,
                    lease.binding_id,
                    exc,
                )
                return
            if not isinstance(params, RasterBitmapClearParams):
                return
            await lease.context.clear_raster(generation=body.generation)
            return
        if body.command_type != "set_frame":
            logger.warning(
                "Ignoring unsupported raster output command %s on binding %s",
                body.command_type,
                lease.binding_id,
            )
            return
        image_source = _binding_output_image_source(body.params)
        if image_source is None:
            logger.warning(
                "Ignoring raster output without a valid image source on binding %s",
                lease.binding_id,
            )
            return
        await lease.context.set_raster_image(image_source, generation=body.generation)

    async def _handle_binding_overlay(
        self,
        lease: BindingLease,
        body: BindingOverlayBody,
    ) -> None:
        if not _binding_body_matches_lease(lease, body.binding):
            logger.warning(
                "Ignoring binding overlay with mismatched mirrored lease identity"
            )
            return
        ok = await lease.context.show_overlay(
            template=body.template,
            title=body.title,
            params=dict(body.params),
            duration_seconds=body.duration_seconds,
            overlay_id=body.overlay_id,
            generation=body.generation,
            binding_output_generation=body.binding.output_generation,
        )
        if not ok:
            logger.info(
                "Ignoring stale binding overlay for binding %s generation=%s",
                lease.binding_id,
                body.generation,
            )

    async def _handle_binding_overlay_clear(
        self,
        lease: BindingLease,
        body: BindingOverlayClearBody,
    ) -> None:
        if not _binding_body_matches_lease(lease, body.binding):
            logger.warning(
                "Ignoring binding overlay clear with mismatched mirrored lease identity"
            )
            return
        ok = await lease.context.clear_overlay(
            overlay_id=body.overlay_id,
            generation=body.generation,
            binding_output_generation=body.binding.output_generation,
        )
        if not ok:
            logger.info(
                "Ignoring stale binding overlay clear for binding %s generation=%s",
                lease.binding_id,
                body.generation,
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
        if self._consume_release_for_rebound_control(translated, binding_id):
            logger.info(
                "Ignoring release for rebound control config=%s control=%s capability=%s",
                self.config_id,
                control_id,
                translated.capability_id,
            )
            return
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

        self._record_held_input_binding(translated, binding_id)
        try:
            await lease.context.on_input(translated.action_event)
        except Exception as e:
            logger.error(
                "Error delivering input to action %s: %s",
                lease.action_uuid,
                e,
                exc_info=True,
            )

    def _record_held_input_binding(self, translated, binding_id: str) -> None:
        if translated.action_event.event_type != "down":
            return
        self._held_input_bindings[
            (translated.control_id, translated.capability_id)
        ] = binding_id

    def _consume_release_for_rebound_control(
        self,
        translated,
        binding_id: str | None,
    ) -> bool:
        if translated.action_event.event_type != "up":
            return False
        key = (translated.control_id, translated.capability_id)
        down_binding_id = self._held_input_bindings.pop(key, None)
        return down_binding_id is not None and down_binding_id != binding_id
