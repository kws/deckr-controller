import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
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
    ACTION_LIFECYCLE_REJECTED,
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
    ActionLifecycleRejectedBody,
    BindingMetadata,
    BindingOutputBody,
    BindingOverlayBody,
    BindingOverlayClearBody,
    DynamicPageCommand,
    MatchedCapability,
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
from deckr.lanes import EndpointSession
from pydantic import ValidationError

from deckr.controller._action_availability import (
    ActionAvailabilityCache,
    ActionAvailabilityPolicy,
    ProviderActionKey,
)
from deckr.controller._action_interest import (
    ActionInterestSnapshot,
    ActionInterestSource,
    ActionInterestTracker,
)
from deckr.controller._action_provider_sessions import (
    ProviderSessionKey,
    provider_session_key,
)
from deckr.controller._binding_planner import (
    ActionIntentKey,
    BindingPlanner,
    BindingPlanStatus,
    DynamicPageSession,
    PageFrame,
    PagePlan,
    PlannedBinding,
)
from deckr.controller._binding_resolution import ResolvedControlBinding
from deckr.controller._binding_validator import format_validation_summary
from deckr.controller._command_router import DeviceOutput
from deckr.controller._device_layout import (
    ControlSurface,
    control_surface_for_raster_capability,
    raster_controls,
)
from deckr.controller._endpoint_messages import send_with_endpoint_identity
from deckr.controller._event_translator import EventTranslator
from deckr.controller._hardware_service import HardwareCommandService
from deckr.controller._navigation_service import (
    NavigationService,
    PageStackEntry,
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
from deckr.controller.action_provider.events import ActionCatalogChangedEvent
from deckr.controller.action_provider.provider import (
    ActionMetadata,
    ActionProviderManager,
)
from deckr.controller.config._data import DeviceConfig, Profile
from deckr.controller.settings import SettingsService

logger = logging.getLogger(__name__)

DEFAULT_WIDGET_TIMEOUT_MS = 60_000
ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS = 1.0
BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS = 1.0
SETTINGS_SNAPSHOT_TIMEOUT_SECONDS = 1.0
DETACH_NOTIFY_TIMEOUT_SECONDS = 1.0
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
        logger.warning(
            "Ignoring invalid dynamic page descriptor payload", exc_info=True
        )
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


def _dedupe_action_intents(
    intents: Iterable[ActionIntentKey],
) -> tuple[ActionIntentKey, ...]:
    return tuple(dict.fromkeys(intents))


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


@dataclass(slots=True)
class HeldInputRecord:
    binding_id: str
    control_id: str
    capability_id: str
    context_id: str
    down_event: Any


def _qualified_action_id(provider_instance_id: str, action_uuid: str) -> str:
    return f"{provider_instance_id}::{action_uuid}"


def _provider_action_key_from_catalog_id(
    catalog_id: str,
) -> ProviderActionKey | None:
    provider_instance_id, separator, action_uuid = catalog_id.partition("::")
    if not separator or not provider_instance_id or not action_uuid:
        return None
    return ProviderActionKey(provider_instance_id, action_uuid)


def _lease_matches_action(lease: BindingLease, action_meta: ActionMetadata) -> bool:
    return (
        lease.action_uuid == action_meta.uuid
        and lease.provider_instance_id == action_meta.provider_instance_id
        and lease.provider_id == action_meta.provider_id
        and lease.provider_session_id == action_meta.provider_session_id
    )


def _lease_matches_action_ignoring_session(
    lease: BindingLease,
    action_meta: ActionMetadata,
) -> bool:
    return (
        lease.action_uuid == action_meta.uuid
        and lease.provider_instance_id == action_meta.provider_instance_id
        and lease.provider_id == action_meta.provider_id
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizedCommandTarget:
    sender_provider_instance_id: str
    context_id: str
    binding: BindingLease | None = None
    page_session: DynamicPageSession | None = None


def _binding_body_matches_lease(lease: BindingLease, binding: BindingMetadata) -> bool:
    return (
        binding.provider_instance_id == lease.provider_instance_id
        and binding.provider_id == lease.provider_id
        and binding.action_id == lease.action_uuid
        and binding.context_id == lease.context_id
        and binding.binding_id == lease.binding_id
        and binding.action_instance_id == lease.action_instance_id
    )


def _action_instance_matches_metadata(
    stored: ActionInstanceMetadata,
    metadata: ActionInstanceMetadata,
) -> bool:
    return (
        stored.provider_instance_id == metadata.provider_instance_id
        and stored.provider_id == metadata.provider_id
        and stored.action_id == metadata.action_id
        and stored.action_instance_id == metadata.action_instance_id
        and stored.config_id == metadata.config_id
        and stored.context_id == metadata.context_id
    )


def _action_instance_matches_action(
    stored: ActionInstanceMetadata,
    action_meta: ActionMetadata,
    *,
    config_id: str,
) -> bool:
    return (
        stored.provider_instance_id == action_meta.provider_instance_id
        and stored.provider_id == action_meta.provider_id
        and stored.action_id == action_meta.uuid
        and stored.config_id == config_id
    )


def _page_session_matches_metadata(
    session: DynamicPageSession,
    metadata: PageSessionMetadata,
) -> bool:
    return (
        metadata.provider_instance_id == session.owner_provider_instance_id
        and metadata.provider_id == session.owner_provider_id
        and metadata.action_instance_id == session.action_instance_id
        and metadata.page_id == session.page_id
        and metadata.page_session_id == session.page_session_id
        and metadata.context_id == session.context_id
        and metadata.owner_binding_id == session.owner_binding_id
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
        actions_bus: EndpointSession,
        start_soon: Callable,
        render_backend: RenderBackend | None = None,
        settings_service: SettingsService | None = None,
        config_stream: AsyncIterator[DeviceConfig | None] | None = None,
        on_config_removed: Callable[[str], Awaitable[None]] | None = None,
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
        self._clock = clock or time.monotonic
        self._render_dispatcher = RenderDispatcher(
            command_service=command_service,
            config_id=self.config_id,
            backend=self._render_backend,
            start_soon=start_soon,
        )
        self._settings_service = settings_service
        self._on_config_removed = on_config_removed
        self.action_contexts = AsyncMap[str, ControlContext]()
        self._translator = EventTranslator(controller_id=controller_id)
        self._nav = NavigationService(config)
        self._binding_planner = BindingPlanner(
            controller_id=controller_id,
            config_id=self.config_id,
        )
        self._dynamic_page_session: DynamicPageSession | None = None
        self._page_frames: list[PageFrame] = []
        self._current_plan: PagePlan | None = None
        self._planned_bindings_by_control: dict[str, PlannedBinding] = {}
        self._config_active = True
        self._action_availability = ActionAvailabilityCache(
            policy=ActionAvailabilityPolicy(
                fresh_ttl_seconds=None,
                stale_grace_seconds=None,
            ),
            clock=self._clock,
        )
        self._action_interest = ActionInterestTracker(clock=self._clock)
        self._binding_leases: dict[str, BindingLease] = {}
        self._binding_by_context: dict[str, str] = {}
        self._active_binding_by_control: dict[str, str] = {}
        self._held_input_bindings: dict[tuple[str, str], HeldInputRecord] = {}
        self._cancelled_input_keys: set[tuple[str, str]] = set()
        self._action_instances: dict[str, ActionInstanceMetadata] = {}
        self._action_instance_providers: dict[str, str] = {}
        self._action_instance_provider_sessions: dict[
            str,
            ProviderSessionKey | None,
        ] = {}
        self._page_timeout_check_interval = page_timeout_check_interval
        self._nav_lock = anyio.Lock()
        self._sync_action_interest()

    async def start(
        self,
        tg: anyio.abc.TaskGroup,
        stopping: anyio.Event,
    ) -> None:
        tg.start_soon(self._page_timeout_loop, stopping)

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

    def _describe_page_entry(self, entry: PageStackEntry | None) -> str:
        if entry is None:
            return "none"
        if isinstance(entry, StaticPageRef):
            return f"static:{entry.profile_name}:{entry.page_index}"
        return f"dynamic:{entry.page_id}"

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
        logger.info(
            "Revoking binding config=%s control=%s action=%s provider=%s "
            "binding=%s reason=%s clearOutput=%s notifyProvider=%s",
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
            reason,
            clear_output,
            notify_provider,
        )
        if clear_held_input:
            await self._cancel_held_inputs_for_binding(lease)
        self._binding_by_context.pop(lease.context_id, None)
        active_binding = self._active_binding_by_control.get(lease.control_id)
        if active_binding == binding_id:
            self._active_binding_by_control.pop(lease.control_id, None)
            await self.action_contexts.delete(lease.control_id)
        if lease.attached and notify_provider:
            with anyio.move_on_after(DETACH_NOTIFY_TIMEOUT_SECONDS) as scope:
                await lease.context.on_binding_detached(reason)
            if scope.cancel_called:
                logger.warning(
                    "Timed out notifying provider of binding detach config=%s "
                    "control=%s action=%s provider=%s binding=%s reason=%s",
                    self.config_id,
                    lease.control_id,
                    lease.action_uuid,
                    lease.provider_instance_id,
                    lease.binding_id,
                    reason,
                )
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

    async def _cancel_held_inputs_for_binding(self, lease: BindingLease) -> None:
        for key, held in tuple(self._held_input_bindings.items()):
            if held.binding_id != lease.binding_id:
                continue
            self._held_input_bindings.pop(key, None)
            self._cancelled_input_keys.add(key)
            if not lease.attached or held.context_id != lease.context_id:
                continue
            cancel_event = held.down_event.model_copy(
                update={"event_type": "cancel"}
            )
            try:
                await lease.context.on_input(cancel_event)
            except Exception:
                logger.exception(
                    "Error delivering cancel to action %s binding=%s",
                    lease.action_uuid,
                    lease.binding_id,
                )

    async def _cancel_all_held_inputs(self) -> None:
        for held in tuple(self._held_input_bindings.values()):
            lease = self._binding_leases.get(held.binding_id)
            if lease is not None:
                await self._cancel_held_inputs_for_binding(lease)
            else:
                key = (held.control_id, held.capability_id)
                self._held_input_bindings.pop(key, None)
                self._cancelled_input_keys.add(key)

    async def _revoke_active_bindings(
        self,
        *,
        clear_outputs: bool = True,
        reason: str = "active_bindings",
    ) -> None:
        await self._revoke_active_bindings_except(
            clear_outputs=clear_outputs,
            preserve_output_control_ids=frozenset(),
            reason=reason,
        )

    async def _revoke_active_bindings_except(
        self,
        *,
        clear_outputs: bool = True,
        preserve_output_control_ids: frozenset[str],
        reason: str = "active_bindings",
        clear_held_input: bool = True,
    ) -> None:
        for binding_id in list(self._binding_leases):
            lease = self._binding_leases.get(binding_id)
            clear_output = clear_outputs
            if lease is not None and lease.control_id in preserve_output_control_ids:
                clear_output = False
            await self._revoke_binding(
                binding_id,
                clear_output=clear_output,
                reason=reason,
                clear_held_input=clear_held_input,
            )

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

    def _sync_top_frame_state(self) -> None:
        frame = self._page_frames[-1] if self._page_frames else None
        self._current_plan = frame.committed_plan if frame is not None else None
        self._planned_bindings_by_control = (
            {
                planned.control_id: planned
                for planned in frame.committed_plan.bindings
            }
            if frame is not None
            else {}
        )
        dynamic_sessions = [
            page_frame.page_session
            for page_frame in self._page_frames
            if page_frame.page_session is not None
        ]
        self._dynamic_page_session = (
            dynamic_sessions[-1] if dynamic_sessions else None
        )
        self._sync_action_interest()

    def action_interest_snapshot(
        self,
        *,
        now: float | None = None,
    ) -> ActionInterestSnapshot:
        return self._action_interest.snapshot(now=now)

    def _sync_action_interest(self) -> None:
        now = self._clock()
        if self._config_active:
            self._action_interest.replace_strong_interests(
                ActionInterestSource.CONNECTED_CONFIG,
                self._configured_action_intents(),
                now=now,
            )
        else:
            self._action_interest.clear_source(
                ActionInterestSource.CONNECTED_CONFIG,
                now=now,
            )

        visible_source = self._visible_action_interest_source()
        visible_intents = self._current_plan_action_intents()
        if visible_source == ActionInterestSource.DYNAMIC_PAGE:
            self._action_interest.clear_source(
                ActionInterestSource.VISIBLE_BINDING,
                now=now,
            )
            self._action_interest.replace_strong_interests(
                ActionInterestSource.DYNAMIC_PAGE,
                visible_intents,
                now=now,
            )
            return

        self._action_interest.replace_strong_interests(
            ActionInterestSource.VISIBLE_BINDING,
            visible_intents,
            now=now,
        )
        self._action_interest.clear_source(
            ActionInterestSource.DYNAMIC_PAGE,
            now=now,
        )

    def _visible_action_interest_source(self) -> ActionInterestSource:
        if (
            self._current_plan is not None
            and self._current_plan.page_session is not None
        ):
            return ActionInterestSource.DYNAMIC_PAGE
        return ActionInterestSource.VISIBLE_BINDING

    def _configured_action_intents(self) -> tuple[ActionIntentKey, ...]:
        intents: list[ActionIntentKey] = []
        for profile in self.config.profiles:
            for page_index in range(len(profile.pages)):
                bindings = self._nav.resolve_static_bindings(
                    StaticPageRef(
                        profile_name=profile.name,
                        page_index=page_index,
                    )
                )
                intents.extend(self._binding_planner.static_action_intents(bindings))
        return _dedupe_action_intents(intents)

    def _current_plan_action_intents(self) -> tuple[ActionIntentKey, ...]:
        if self._current_plan is None:
            return ()
        return _dedupe_action_intents(
            self._binding_planner.resolved_action_intent_key(planned.binding)
            for planned in self._current_plan.bindings
        )

    async def _action_metadata_snapshot_for_plan(
        self,
        intents: tuple[ActionIntentKey, ...],
        *,
        refresh_actions: bool,
    ) -> dict[ActionIntentKey, ActionMetadata]:
        authoritative = self._action_availability.snapshot_for_intents(
            intents,
            now=self._clock(),
        )
        if not refresh_actions:
            return authoritative

        transitional = await self._transitional_beacon_candidate_metadata_for_plan(
            intents
        )
        return {**transitional, **authoritative}

    async def _transitional_beacon_candidate_metadata_for_plan(
        self,
        intents: tuple[ActionIntentKey, ...],
    ) -> dict[ActionIntentKey, ActionMetadata]:
        """Bridge Beacon catalog metadata into planning until direct availability exists."""
        metadata_by_intent: dict[ActionIntentKey, ActionMetadata] = {}
        for intent in intents:
            action_meta = await self.manager.get_action(
                intent.action_uuid,
                provider_instance_id=intent.provider_instance_id,
                provider_labels=dict(intent.provider_labels),
            )
            if action_meta is None:
                continue
            action_meta = self._action_metadata_with_current_session(action_meta)
            self._action_availability.record_candidate(
                action_meta,
                now=self._clock(),
                intent=intent,
            )
            metadata_by_intent[intent] = action_meta
        return metadata_by_intent

    def _log_static_page_plan_rejection(
        self,
        result_errors,
    ) -> None:
        logger.error(
            "Page transition rejected (capability validation): %s",
            format_validation_summary(list(result_errors)),
        )
        for err in result_errors:
            logger.error(
                "Binding validation failed [%s]: %s (control=%s action=%s) %s",
                err.code,
                err.message,
                err.control_ref,
                err.action_uuid,
                err.details,
            )

    def _log_dynamic_page_plan_rejection(
        self,
        result_errors,
    ) -> None:
        logger.error(
            "Dynamic page transition rejected (capability validation): %s",
            format_validation_summary(list(result_errors)),
        )
        for err in result_errors:
            logger.error(
                "Dynamic page binding validation failed [%s]: %s (control=%s action=%s) %s",
                err.code,
                err.message,
                err.control_ref,
                err.action_uuid,
                err.details,
            )

    async def _build_static_page_plan(
        self,
        entry: StaticPageRef,
        *,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> PagePlan | None:
        bindings = self._nav.resolve_static_bindings(entry)
        action_metadata = await self._action_metadata_snapshot_for_plan(
            self._binding_planner.static_action_intents(bindings),
            refresh_actions=refresh_actions,
        )
        result = self._binding_planner.build_static_page_plan(
            entry,
            bindings=bindings,
            device=self.device,
            action_metadata=action_metadata,
            retained_plan=retained_plan,
        )
        if result.plan is None:
            self._log_static_page_plan_rejection(result.validation_errors)
        return result.plan

    async def _build_dynamic_page_plan(
        self,
        entry: DynamicPageCommand,
        *,
        page_session: DynamicPageSession,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> PagePlan | None:
        action_metadata = await self._action_metadata_snapshot_for_plan(
            self._binding_planner.dynamic_action_intents(
                entry.bindings,
                owner_action_uuid=page_session.owner_action_uuid,
                owner_provider_instance_id=page_session.owner_provider_instance_id,
            ),
            refresh_actions=refresh_actions,
        )
        result = self._binding_planner.build_dynamic_page_plan(
            entry,
            device=self.device,
            page_session=page_session,
            action_metadata=action_metadata,
            retained_plan=retained_plan,
        )
        if result.plan is None:
            self._log_dynamic_page_plan_rejection(result.validation_errors)
        return result.plan

    async def _build_page_plan(
        self,
        entry: PageStackEntry,
        *,
        page_session: DynamicPageSession | None = None,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> PagePlan | None:
        if isinstance(entry, StaticPageRef):
            return await self._build_static_page_plan(
                entry,
                retained_plan=retained_plan,
                refresh_actions=refresh_actions,
            )
        if page_session is None:
            logger.error("Dynamic page planning missing page session")
            return None
        return await self._build_dynamic_page_plan(
            entry,
            page_session=page_session,
            retained_plan=retained_plan,
            refresh_actions=refresh_actions,
        )

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

    def _restamp_binding_route(
        self,
        lease: BindingLease,
        action_meta: ActionMetadata,
    ) -> None:
        action_meta = self._action_metadata_with_current_session(action_meta)
        lease.provider_session_id = action_meta.provider_session_id
        lease.provider_session_key = (
            None
            if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            else provider_session_key(action_meta)
        )
        lease.context.provider_session_id = lease.provider_session_id
        self._action_instance_provider_sessions[lease.action_instance_id] = (
            lease.provider_session_key
        )

    async def _activate_binding(
        self,
        lease: BindingLease,
    ) -> bool:
        if lease.attached:
            return True
        action_meta = ActionMetadata(
            uuid=lease.action_uuid,
            provider_instance_id=lease.provider_instance_id,
            provider_id=lease.provider_id,
            provider_session_id=lease.provider_session_id,
        )
        had_action_instance = lease.action_instance_id in self._action_instances
        logger.info(
            "Binding activation starting config=%s control=%s action=%s "
            "provider=%s binding=%s",
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
        )
        with anyio.move_on_after(ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS) as scope:
            await self._ensure_action_instance(
                action_meta=action_meta,
                action_instance_id=lease.action_instance_id,
                context_id=lease.context_id,
                settings=lease.context.settings,
            )
        if scope.cancel_called:
            if not had_action_instance:
                self._action_instances.pop(lease.action_instance_id, None)
                self._action_instance_providers.pop(lease.action_instance_id, None)
                self._action_instance_provider_sessions.pop(
                    lease.action_instance_id,
                    None,
                )
            logger.warning(
                "Binding activation timed out config=%s control=%s action=%s "
                "provider=%s binding=%s stage=actionInstanceCreated timeout=%ss",
                self.config_id,
                lease.control_id,
                lease.action_uuid,
                lease.provider_instance_id,
                lease.binding_id,
                ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS,
            )
            return False
        logger.info(
            "Binding activation action instance ready config=%s control=%s "
            "action=%s provider=%s binding=%s",
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
        )
        self._binding_by_context[lease.context_id] = lease.binding_id
        self._active_binding_by_control[lease.control_id] = lease.binding_id
        await self.action_contexts.set(lease.control_id, lease.context)
        lease.attached = True
        with anyio.move_on_after(BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS) as scope:
            await lease.context.on_binding_attached()
        if scope.cancel_called:
            logger.warning(
                "Binding activation timed out config=%s control=%s action=%s "
                "provider=%s binding=%s stage=bindingAttached timeout=%ss",
                self.config_id,
                lease.control_id,
                lease.action_uuid,
                lease.provider_instance_id,
                lease.binding_id,
                BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS,
            )
        else:
            logger.info(
                "Binding activation attached config=%s control=%s action=%s "
                "provider=%s binding=%s",
                self.config_id,
                lease.control_id,
                lease.action_uuid,
                lease.provider_instance_id,
                lease.binding_id,
            )
        return True

    async def _reconcile_binding_sessions(self) -> None:
        for lease in tuple(self._binding_leases.values()):
            if not lease.attached:
                await self._activate_binding(lease)

    async def _ensure_action_instance(
        self,
        *,
        action_meta: Any,
        action_instance_id: str,
        context_id: str,
        settings: Mapping[str, Any],
    ) -> None:
        existing = self._action_instances.get(action_instance_id)
        if existing is not None and _action_instance_matches_action(
            existing,
            action_meta,
            config_id=self.config_id,
        ):
            return
        if existing is not None:
            await self._destroy_action_instance(
                action_instance_id,
                reason="action_instance_retargeted",
            )
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
        provider_session_key_for_action = (
            None
            if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            else provider_session_key(action_meta)
        )
        self._action_instance_provider_sessions[action_instance_id] = (
            provider_session_key_for_action
        )
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        msg = action_message(
            sender=controller_address(self._controller_id),
            sender_session_id=self._actions_bus.session_id,
            recipient=action_provider_address(action_meta.provider_instance_id),
            recipient_session_id=(
                provider_session_key_for_action.provider_session_id
                if provider_session_key_for_action is not None
                else None
            ),
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
        await send_with_endpoint_identity(self._actions_bus, msg)

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
        provider_session_key_for_action = self._action_instance_provider_sessions.pop(
            action_instance_id,
            None,
        )
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
            recipient_session_id=(
                provider_session_key_for_action.provider_session_id
                if provider_session_key_for_action is not None
                else None
            ),
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
        await send_with_endpoint_identity(self._actions_bus, msg)

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
            if action_meta is not None:
                action_meta = self._action_metadata_with_current_session(action_meta)
                self._action_availability.record_candidate(
                    action_meta,
                    now=self._clock(),
                    intent=self._binding_planner.resolved_action_intent_key(binding),
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
            with anyio.move_on_after(SETTINGS_SNAPSHOT_TIMEOUT_SECONDS) as scope:
                try:
                    snapshot = await self._settings_service.get(settings_target)
                    initial_settings = dict(thaw_json(snapshot.settings))
                except KeyError:
                    initial_settings = dict(binding.settings)
            if scope.cancel_called:
                logger.warning(
                    "Binding settings snapshot timed out config=%s control=%s "
                    "action=%s provider=%s target=%s timeout=%ss",
                    self.config_id,
                    binding.control_id,
                    binding.action_uuid,
                    action_meta.provider_instance_id,
                    settings_target.key(),
                    SETTINGS_SNAPSHOT_TIMEOUT_SECONDS,
                )
        builtin_action = None
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID and hasattr(
            self.manager, "get_builtin_action"
        ):
            builtin_action = self.manager.get_builtin_action(action_meta.uuid)
        binding_id = make_binding_id()
        existing_action_instance = self._action_instances.get(action_instance_id)
        context_id = (
            existing_action_instance.context_id
            if existing_action_instance is not None
            and _action_instance_matches_action(
                existing_action_instance,
                action_meta,
                config_id=self.config_id,
            )
            else make_context_id()
        )
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
            provider_session_id=provider_session_id,
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
        attached = await self._activate_binding(lease)
        if attached:
            logger.info(
                "Binding resolved on profile=%s page=%s control=%s action=%s provider=%s binding=%s",
                profile_id,
                page_id,
                binding.control_id,
                binding.action_uuid,
                action_meta.provider_instance_id,
                binding_id,
            )
            return True
        logger.info(
            "Binding pending provider availability on profile=%s page=%s control=%s action=%s provider=%s binding=%s",
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

    async def _page_timeout_loop(self, stopping: anyio.Event) -> None:
        while not stopping.is_set():
            await anyio.sleep(self._page_timeout_check_interval)
            if stopping.is_set():
                return
            session = self._dynamic_page_session
            if session is None:
                continue
            if session.timeout_ms <= 0:
                continue
            elapsed_ms = int((self._clock() - session.last_activity) * 1000)
            if elapsed_ms >= session.timeout_ms:
                await self.close_page(context_id=session.context_id, reason="timeout")

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
            recipient_session_id=session.owner_provider_session_id,
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
        await send_with_endpoint_identity(self._actions_bus, msg)

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
            recipient_session_id=session.owner_provider_session_id,
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
        await send_with_endpoint_identity(self._actions_bus, msg)

    async def _finalize_dynamic_page(
        self,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> None:
        sessions = [
            frame.page_session
            for frame in reversed(self._page_frames)
            if frame.page_session is not None
        ]
        if not sessions:
            return
        for session in sessions:
            await self._emit_page_closed(
                session,
                reason,
                causation_id=causation_id,
            )
        self._page_frames = [
            frame for frame in self._page_frames if frame.page_session is None
        ]
        if self._page_frames:
            self._nav.set_page(self._page_frames[-1].entry)
        else:
            self._nav._current_page = None
        self._sync_top_frame_state()

    async def _execute_transition(
        self,
        transition: PageTransition,
        *,
        page_session: DynamicPageSession | None = None,
        preserve_rebound_outputs: bool = False,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> bool:
        plan = await self._build_page_plan(
            transition.arriving,
            page_session=page_session,
            retained_plan=retained_plan,
            refresh_actions=refresh_actions,
        )
        if plan is None:
            return False
        await self._commit_page_plan(
            plan,
            departing=transition.departing,
            preserve_rebound_outputs=preserve_rebound_outputs,
        )
        return True

    async def _commit_page_plan(
        self,
        plan: PagePlan,
        *,
        departing: PageStackEntry | None,
        preserve_rebound_outputs: bool = False,
    ) -> None:
        arriving = plan.entry
        logger.info(
            "Executing page transition config=%s departing=%s arriving=%s "
            "pageSession=%s preserveReboundOutputs=%s",
            self.config_id,
            self._describe_page_entry(departing),
            self._describe_page_entry(arriving),
            plan.page_session.page_session_id
            if plan.page_session is not None
            else None,
            preserve_rebound_outputs,
        )

        preserve_output_control_ids = (
            frozenset(planned.control_id for planned in plan.bindings)
            if preserve_rebound_outputs
            else frozenset()
        )

        if departing is not None:
            await self._revoke_active_bindings_except(
                preserve_output_control_ids=preserve_output_control_ids,
                reason=(
                    "page_transition:"
                    f"{self._describe_page_entry(departing)}->"
                    f"{self._describe_page_entry(arriving)}"
                ),
            )

        await self._clear_all_raster_controls(
            preserve_control_ids=preserve_output_control_ids,
        )

        self._current_plan = plan
        self._planned_bindings_by_control = {
            planned.control_id: planned for planned in plan.bindings
        }

        resolved_count = 0
        unavailable_count = 0
        for planned in plan.bindings:
            if await self._install_planned_binding(plan, planned):
                resolved_count += 1
            else:
                unavailable_count += 1
        logger.info(
            "Page bindings installed config=%s page=%s resolved=%s unavailable=%s",
            self.config_id,
            plan.page_id,
            resolved_count,
            unavailable_count,
        )

    async def _install_planned_binding(
        self,
        plan: PagePlan,
        planned: PlannedBinding,
    ) -> bool:
        if planned.status == BindingPlanStatus.UNAVAILABLE:
            control = _find_control_surface(
                self.device,
                planned.control_id,
                raster_capability_id=_selected_raster_capability_id(planned.binding),
            )
            if control is not None:
                await self._render_unavailable_to_control(control)
            return False
        ok = await self._try_resolve_binding(
            planned.binding,
            profile_id=plan.profile_id,
            page_id=plan.page_id,
            action_instance_id=planned.action_instance_id,
            page_session_id=planned.page_session_id,
            persist_settings=planned.persist_settings,
            item_key=planned.item_key,
            handler=planned.handler,
            action_meta=planned.action_meta,
        )
        if ok:
            return True
        control = _find_control_surface(
            self.device,
            planned.control_id,
            raster_capability_id=_selected_raster_capability_id(planned.binding),
        )
        if control is not None:
            await self._render_unavailable_to_control(control)
        return False

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
        dynamic_sessions_to_close = (
            [
                frame.page_session
                for frame in self._page_frames
                if frame.page_session is not None
            ]
            if close_dynamic
            else []
        )
        current_frame = self._page_frames[-1] if self._page_frames else None
        departing = current_frame.entry if current_frame is not None else None
        if descriptor is not None:
            entry: PageStackEntry = descriptor
        else:
            profile_name = profile or "default"
            page_index = page if page is not None else 0
            profile_obj = self._find_profile(profile_name)
            entry = StaticPageRef(profile_name=profile_obj.name, page_index=page_index)

        retained_plan = (
            current_frame.committed_plan
            if current_frame is not None and current_frame.entry == entry
            else None
        )
        plan = await self._build_page_plan(
            entry,
            page_session=page_session,
            retained_plan=retained_plan,
            refresh_actions=True,
        )
        if plan is None:
            return False

        preserve_rebound_outputs = isinstance(departing, DynamicPageCommand) and (
            isinstance(entry, StaticPageRef)
            or (page_session is not None and isinstance(entry, DynamicPageCommand))
        )
        await self._commit_page_plan(
            plan,
            departing=departing,
            preserve_rebound_outputs=preserve_rebound_outputs,
        )

        if isinstance(entry, StaticPageRef):
            self._page_frames = [PageFrame(entry, None, plan)]
        elif page_session is not None:
            next_frame = PageFrame(entry, page_session, plan)
            if (
                current_frame is not None
                and current_frame.page_session is page_session
            ):
                self._page_frames[-1] = next_frame
            else:
                self._page_frames.append(next_frame)
        self._nav.set_page(entry)
        self._sync_top_frame_state()

        for session in reversed(dynamic_sessions_to_close):
            if session is not None:
                await self._emit_page_closed(
                    session,
                    close_reason,
                    causation_id=causation_id,
                )
        return True

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
                logger.warning(
                    "open_page ignored: no active context for %s", context_id
                )
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
                logger.warning(
                    "open_page ignored: no active context for %s", context_id
                )
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
                logger.warning(
                    "replace_page ignored: no active page for %s", context_id
                )
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
            if (
                len(self._page_frames) < 2
                or self._page_frames[-1].page_session is not session
            ):
                logger.info("Dynamic page close ignored for non-top session")
                return
            departing_frame = self._page_frames[-1]
            restore_frame = self._page_frames[-2]
            restored_plan = await self._build_page_plan(
                restore_frame.entry,
                page_session=restore_frame.page_session,
                retained_plan=restore_frame.committed_plan,
                refresh_actions=False,
            )
            if restored_plan is None:
                logger.warning(
                    "Dynamic page close rejected because restore frame is invalid"
                )
                return
            await self._commit_page_plan(
                restored_plan,
                departing=departing_frame.entry,
                preserve_rebound_outputs=True,
            )
            self._page_frames.pop()
            self._page_frames[-1] = PageFrame(
                restore_frame.entry,
                restore_frame.page_session,
                restored_plan,
            )
            self._nav.set_page(restore_frame.entry)
            self._sync_top_frame_state()
            await self._emit_page_closed(
                session,
                reason=reason,
                causation_id=causation_id,
            )

    async def clear_page(self, *, clear_outputs: bool = True):
        async with self._nav_lock:
            await self._cancel_all_held_inputs()
            await self._finalize_dynamic_page(reason="clear")
            await self._revoke_active_bindings(
                clear_outputs=clear_outputs,
                reason="clear_page",
            )
            await self._destroy_all_action_instances(reason="clear")
            if clear_outputs:
                await self._clear_all_raster_controls()
            self._page_frames.clear()
            self._nav._current_page = None
            self._sync_top_frame_state()

    async def on_descriptor_changed(self, descriptor: DeviceDescriptor) -> None:
        """Re-resolve the active page against a changed device descriptor."""

        async with self._nav_lock:
            self.device = descriptor
            current_frame = self._page_frames[-1] if self._page_frames else None
            if current_frame is None:
                return
            ok = await self._execute_transition(
                PageTransition(
                    departing=current_frame.entry,
                    arriving=current_frame.entry,
                ),
                page_session=current_frame.page_session,
                preserve_rebound_outputs=True,
                retained_plan=current_frame.committed_plan,
                refresh_actions=False,
            )
            if not ok:
                await self._finalize_dynamic_page(reason="device_descriptor_changed")
                await self._revoke_active_bindings(
                    clear_outputs=False,
                    reason="device_descriptor_changed_failed",
                )
                return
            if self._page_frames and self._current_plan is not None:
                self._page_frames[-1] = PageFrame(
                    current_frame.entry,
                    current_frame.page_session,
                    self._current_plan,
                )
                self._sync_top_frame_state()

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

    async def on_action_catalog_changed(
        self,
        event: ActionCatalogChangedEvent,
    ) -> None:
        """Refresh the current page availability overlay after Beacon changes."""
        async with self._nav_lock:
            self._remove_catalog_candidates(event.catalog_removed)
            current_frame = self._page_frames[-1] if self._page_frames else None
            if current_frame is None:
                return

            refreshed_plan = await self._build_page_plan(
                current_frame.entry,
                page_session=current_frame.page_session,
                retained_plan=current_frame.committed_plan,
                refresh_actions=True,
            )
            if refreshed_plan is None:
                logger.warning(
                    "Skipping action-change rebind for invalid page bindings"
                )
                return

            logger.info(
                "Re-evaluating page bindings for config=%s page=%s after action catalog change +%s -%s ~%s successor=%s",
                self.config_id,
                refreshed_plan.page_id,
                event.catalog_added,
                event.catalog_removed,
                event.catalog_updated,
                event.provider_session_successions,
            )

            self._page_frames[-1] = PageFrame(
                current_frame.entry,
                current_frame.page_session,
                refreshed_plan,
            )
            self._sync_top_frame_state()

            for planned in refreshed_plan.bindings:
                lease = self._binding_lease_for_control(planned.control_id)
                if planned.status == BindingPlanStatus.UNAVAILABLE:
                    if lease is None:
                        control = _find_control_surface(
                            self.device,
                            planned.control_id,
                            raster_capability_id=_selected_raster_capability_id(
                                planned.binding
                            ),
                        )
                        if control is not None:
                            await self._render_unavailable_to_control(control)
                    continue

                if lease is None:
                    await self._install_planned_binding(refreshed_plan, planned)
                    continue

                if _lease_matches_action(lease, planned.action_meta):
                    await self._activate_binding(lease)
                    continue

                if _lease_matches_action_ignoring_session(
                    lease,
                    planned.action_meta,
                ):
                    self._restamp_binding_route(lease, planned.action_meta)
                    await self._activate_binding(lease)
                    continue

                await self._revoke_binding(
                    lease.binding_id,
                    clear_output=False,
                    notify_provider=False,
                    reason="action_catalog_changed",
                    clear_held_input=True,
                )
                await self._install_planned_binding(refreshed_plan, planned)

    def _remove_catalog_candidates(self, catalog_removed: Iterable[str]) -> None:
        keys = tuple(
            key
            for qualified in catalog_removed
            if (key := _provider_action_key_from_catalog_id(qualified)) is not None
        )
        self._action_availability.remove_candidates(keys)

    async def _config_listener(self) -> None:
        """Consume config stream and apply changes."""
        if self._config_stream is None:
            return
        async for config in self._config_stream:
            await self._on_config_changed(config)

    async def _on_config_changed(self, config: DeviceConfig | None) -> None:
        """Handle config update or removal."""
        if config is None:
            self._config_active = False
            await self.clear_page()
            if self._on_config_removed is not None:
                await self._on_config_removed(self.config_id)
            return
        if config == self.config:
            return
        async with self._nav_lock:
            await self._cancel_all_held_inputs()
            self._config_active = True
            self.config = config
            self._nav.update_config(config)
            await self._finalize_dynamic_page(reason="config_change")
            await self._destroy_all_action_instances(reason="config_change")
            profile = config.profiles[0]
            entry = StaticPageRef(profile_name=profile.name, page_index=0)
            plan = await self._build_page_plan(entry, refresh_actions=True)
            if plan is None:
                self._page_frames.clear()
                self._nav._current_page = None
                self._sync_top_frame_state()
                return
            departing = self._page_frames[-1].entry if self._page_frames else None
            await self._commit_page_plan(plan, departing=departing)
            self._page_frames = [PageFrame(entry, None, plan)]
            self._nav.set_page(entry)
            self._sync_top_frame_state()

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
        if not self.manager.provider_instance_provides_provider(
            sender_provider_instance_id,
            target.provider_id,
        ):
            logger.warning(
                "Ignoring provider settings command from %s for unadvertised provider %s",
                sender_provider_instance_id,
                target.provider_id,
            )
            return False
        if sender_session_id is None:
            logger.warning(
                "Ignoring provider settings command from %s without sender session",
                sender_provider_instance_id,
            )
            return False
        current_session_id = self.manager.provider_session_id(
            sender_provider_instance_id
        )
        if sender_session_id != current_session_id:
            logger.warning(
                "Ignoring provider settings command from %s with stale provider session %s",
                sender_provider_instance_id,
                sender_session_id,
            )
            return False
        return True

    def _action_instance_rejection_authorized(
        self,
        msg: DeckrMessage,
        *,
        sender_provider_instance_id: str,
        metadata: ActionInstanceMetadata,
        context_id: str,
    ) -> bool:
        if metadata.config_id != self.config_id or metadata.context_id != context_id:
            return False
        if metadata.provider_instance_id != sender_provider_instance_id:
            return False
        action_instance_id = subject_action_instance_id(msg.subject)
        if (
            action_instance_id is not None
            and action_instance_id != metadata.action_instance_id
        ):
            return False
        stored = self._action_instances.get(metadata.action_instance_id)
        if stored is None or not _action_instance_matches_metadata(stored, metadata):
            return False
        key = self._action_instance_provider_sessions.get(metadata.action_instance_id)
        return key is not None and msg.sender_session_id == key.provider_session_id

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

    async def _handle_action_lifecycle_rejected(
        self,
        msg: DeckrMessage,
        body: ActionLifecycleRejectedBody,
        *,
        context_id: str,
    ) -> None:
        if body.reason == "stale_lifecycle":
            logger.info(
                "Ignoring stale action lifecycle rejection config=%s target=%s",
                self.config_id,
                body.target_kind,
            )
            return

        sender_provider_instance_id = self._command_sender_provider_instance_id(msg)
        if sender_provider_instance_id is None:
            return

        if body.target_kind == "action_instance":
            metadata = body.action_instance
            if metadata is None:
                return
            if not self._action_instance_rejection_authorized(
                msg,
                sender_provider_instance_id=sender_provider_instance_id,
                metadata=metadata,
                context_id=context_id,
            ):
                logger.warning(
                    "Ignoring unauthorized action lifecycle rejection for action instance %s",
                    metadata.action_instance_id,
                )
                return
            await self._reject_action_instance(metadata, reason=body.reason)
            return

        authorization = await self._authorize_action_command(
            msg,
            context_id=context_id,
        )
        if authorization is None:
            return

        if body.target_kind == "binding":
            lease = authorization.binding
            metadata = body.binding
            if (
                lease is None
                or metadata is None
                or metadata.config_id != self.config_id
                or not _binding_body_matches_lease(lease, metadata)
            ):
                logger.warning(
                    "Ignoring action lifecycle rejection for mismatched binding"
                )
                return
            control = (
                _find_control_surface(
                    self.device,
                    lease.control_id,
                    raster_capability_id=lease.raster_capability_id,
                )
                if body.retryable
                else None
            )
            await self._revoke_binding(
                lease.binding_id,
                clear_output=not body.retryable,
                notify_provider=False,
                reason=body.reason,
                clear_held_input=True,
            )
            if control is not None:
                await self._render_unavailable_to_control(control)
            return

        if body.target_kind == "page_session":
            session = authorization.page_session
            metadata = body.page_session
            if (
                session is None
                or metadata is None
                or metadata.config_id != self.config_id
                or not _page_session_matches_metadata(session, metadata)
            ):
                logger.warning(
                    "Ignoring action lifecycle rejection for mismatched page session"
                )
                return
            await self._close_rejected_page_session(session, reason=body.reason)

    async def _reject_action_instance(
        self,
        metadata: ActionInstanceMetadata,
        *,
        reason: str,
    ) -> None:
        page_session = self._dynamic_page_session
        if (
            page_session is not None
            and page_session.action_instance_id == metadata.action_instance_id
        ):
            await self._close_rejected_page_session(page_session, reason=reason)

        for binding_id, lease in tuple(self._binding_leases.items()):
            if lease.action_instance_id == metadata.action_instance_id:
                await self._revoke_binding(
                    binding_id,
                    notify_provider=False,
                    reason=reason,
                    clear_held_input=True,
                )
        await self._destroy_action_instance(
            metadata.action_instance_id,
            reason=reason,
            notify_provider=False,
        )

    async def _close_rejected_page_session(
        self,
        session: DynamicPageSession,
        *,
        reason: str,
    ) -> None:
        await self.close_page(context_id=session.context_id, reason=reason)

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
        if msg_type == ACTION_LIFECYCLE_REJECTED:
            body = ActionLifecycleRejectedBody.model_validate(payload)
            await self._handle_action_lifecycle_rejected(
                msg,
                body,
                context_id=context_id,
            )
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
                if (
                    self._settings_service is None
                    or page_session.settings_target is None
                ):
                    return
                target = settings_target
                if target is None:
                    return
                if target.key() != page_session.settings_target.key():
                    logger.warning(
                        "Ignoring settings command for mismatched page target"
                    )
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
                logger.warning(
                    "Ignoring settings command for mismatched binding target"
                )
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
        key = (control_id, translated.capability_id)
        if translated.action_event.event_type == "up":
            held = self._held_input_bindings.pop(key, None)
            if held is not None:
                lease = self._binding_leases.get(held.binding_id)
                if lease is None:
                    self._cancelled_input_keys.add(key)
                    return
                await self._deliver_input_to_lease(lease, translated)
                return
            if key in self._cancelled_input_keys:
                self._cancelled_input_keys.remove(key)
                logger.info(
                    "Ignoring release after cancel config=%s control=%s capability=%s",
                    self.config_id,
                    control_id,
                    translated.capability_id,
                )
                return

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

        self._record_held_input_binding(translated, lease)
        await self._deliver_input_to_lease(lease, translated)

    async def _deliver_input_to_lease(self, lease: BindingLease, translated) -> None:
        if translated.capability_id not in lease.input_capability_ids:
            logger.info(
                "Ignoring input for unselected capability config=%s control=%s capability=%s",
                self.config_id,
                translated.control_id,
                translated.capability_id,
            )
            return
        try:
            await lease.context.on_input(translated.action_event)
        except Exception as e:
            logger.error(
                "Error delivering input to action %s: %s",
                lease.action_uuid,
                e,
                exc_info=True,
            )

    def _record_held_input_binding(self, translated, lease: BindingLease) -> None:
        if translated.action_event.event_type != "down":
            return
        self._held_input_bindings[(translated.control_id, translated.capability_id)] = (
            HeldInputRecord(
                binding_id=lease.binding_id,
                control_id=translated.control_id,
                capability_id=translated.capability_id,
                context_id=lease.context_id,
                down_event=translated.action_event,
            )
        )
