"""ControlBindingService tests. Uses mock devices (no VirtualDevice)."""

import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
import pytest_asyncio
from conftest import LaneHarness
from deckr.actions.endpoints import (
    BUILTIN_ACTION_PROVIDER_ID,
    RESERVED_BUILTIN_PROVIDER_IDS,
    action_provider_address,
)
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_INSTANCE_DESTROYED,
    ACTION_LIFECYCLE_REJECTED,
    BINDING_ATTACHED,
    BINDING_DETACHED,
    BINDING_OUTPUT,
    CAPABILITY_INPUT,
    CLOSE_PAGE,
    OPEN_PAGE,
    PAGE_SESSION_CLOSED,
    PAGE_SESSION_OPENED,
    REPLACE_PAGE,
    ActionAvailabilityEntry,
    ActionDescriptor,
    CapabilityInputBody,
    DynamicPageCommand,
    PageChildBindingDescriptor,
    PageChildBindingTarget,
    action_message,
    context_subject,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage, controller_address, endpoint_target
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_INPUT_ENCODER,
    DECKR_INPUT_TOUCH,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
    DeviceRef,
)
from deckr.lanes import EndpointSession
from invariant import Node, SubGraphNode, dump_graph_data_uri
from invariant.params import ref

from deckr.controller._action_interest import (
    ActionInterestSource,
    ActionInterestStrength,
)
from deckr.controller._actions import (
    PROVIDER_SESSION_INVALID_REASON,
    ActionAvailabilityRecord,
    ActionAvailabilityState,
    ActionIntentKey,
    ActionMetadata,
    ActionPlanningSnapshot,
    ActionProviderManager,
    ProviderActionKey,
    ProviderSessionKey,
    SettingsActionMetadata,
)
from deckr.controller._actions._availability import ActionAvailabilityCache
from deckr.controller._bindings import (
    ActionInstanceSnapshot,
    BindingLeaseSnapshot,
    ControlBindingService,
)
from deckr.controller._bindings import _service as binding_service_module
from deckr.controller._hardware import HardwareCommandService
from deckr.controller._render import RenderResult
from deckr.controller._render_dispatcher import RenderBackend
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from deckr.controller.settings import ConfigBackedSettingsService, SettingsService

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)
PROVIDER_INSTANCE_ID = "python"
PROVIDER_ID = "test.provider"
PROVIDER_ADDR = action_provider_address(PROVIDER_INSTANCE_ID)
PROVIDER_SESSION_ID = "action-provider-session"
UNSUPPORTED_ACTION_MESSAGE = "unsupportedActionMessage"


def _contract_pointer_for_provider_session(
    provider_session_id: str = PROVIDER_SESSION_ID,
    *,
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    provider_id: str = PROVIDER_ID,
) -> ContractPointer:
    return ContractPointer(
        contractId=f"provider-session:{provider_instance_id}:{provider_id}:{provider_session_id}",
        generation=1,
    )


class _BindingActionServiceHarness:
    def __init__(
        self,
        *,
        manager: ActionProviderManager,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.manager = manager
        self._clock = clock or time.monotonic
        self._cache = ActionAvailabilityCache(clock=self._clock)
        self._contracts: dict[ProviderSessionKey, ContractPointer] = {}
        self._interest_by_config: dict[str, object] = {}
        self.sent_runtime_messages: list[SimpleNamespace] = []

    def planning_snapshot(
        self,
        intents: Iterable[ActionIntentKey],
        *,
        existing_provider_keys: Iterable[ProviderActionKey] = (),
        now: float | None = None,
    ) -> ActionPlanningSnapshot:
        return self._cache.planning_snapshot(
            intents,
            stale_provider_keys=existing_provider_keys,
            ready_provider_session_keys=frozenset(self._contracts),
            now=self._now(now),
        )

    def settings_action_metadata(
        self,
        action_uuid: str,
        *,
        provider_instance_id: str | None = None,
        provider_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
        now: float | None = None,
    ) -> SettingsActionMetadata:
        return self._cache.settings_metadata_for_intent(
            ActionIntentKey(
                action_uuid=action_uuid,
                provider_instance_id=provider_instance_id,
                provider_labels=tuple(sorted((provider_labels or {}).items())),
            ),
            provider_id=provider_id,
            now=self._now(now),
        )

    def current_contract(
        self,
        provider_session_key: ProviderSessionKey | None,
    ) -> ContractPointer | None:
        if provider_session_key is None:
            return None
        return self._contracts.get(provider_session_key)

    async def send_runtime_message(
        self,
        provider_session_key: ProviderSessionKey,
        message_type: str,
        body: Any,
    ) -> bool:
        contract = self.current_contract(provider_session_key)
        if contract is None:
            return False
        self.sent_runtime_messages.append(
            SimpleNamespace(
                provider_session_key=provider_session_key,
                message_type=message_type,
                body=body,
                contract=contract,
            )
        )
        return True

    async def ensure_local_builtin_availability(
        self,
        intents: Iterable[ActionIntentKey],
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        for intent in intents:
            if intent.provider_instance_id is not None and (
                intent.provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS
            ):
                continue
            if intent.provider_labels:
                continue
            action = await self.manager.get_action(
                intent.action_uuid,
                provider_instance_id=intent.provider_instance_id,
                provider_labels=dict(intent.provider_labels),
            )
            if action is None:
                continue
            if action.provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS:
                continue
            record = self._cache.record_available(
                action,
                now=self._clock(),
                intent=intent,
            )
            changed.add(record.key)
        return frozenset(changed)

    def record_lifecycle_unavailable(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        action_uuid: str,
        provider_session_id: str | None = None,
        reason: str | None = None,
        intent: ActionIntentKey | None = None,
        now: float | None = None,
    ) -> ProviderActionKey:
        key = ProviderActionKey(provider_instance_id, action_uuid)
        existing = self._cache.record_for(key)
        metadata = existing.metadata if existing is not None else None
        if metadata is None:
            metadata = ActionMetadata(
                uuid=action_uuid,
                provider_instance_id=provider_instance_id,
                provider_id=provider_id,
                provider_session_id=provider_session_id,
            )
        self._cache.record_unavailable(
            key,
            metadata=metadata,
            reason=reason,
            now=self._now(now),
            intent=intent,
        )
        return key

    def record_for_key(
        self,
        key: ProviderActionKey,
    ) -> ActionAvailabilityRecord | None:
        return self._cache.record_for(key)

    def record_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        now: float | None = None,
    ) -> ActionAvailabilityRecord | None:
        return self._cache.record_for_intent(intent, now=self._now(now))

    def state_for_key(
        self,
        key: ProviderActionKey,
        *,
        now: float | None = None,
    ) -> ActionAvailabilityState | None:
        return self._cache.state_for(key, now=self._now(now))

    def provider_lifecycle_recovery_required(self, key: ProviderActionKey) -> bool:
        return self._cache.provider_lifecycle_recovery_required(key)

    def consume_provider_lifecycle_recovery(self, key: ProviderActionKey) -> bool:
        return self._cache.consume_provider_lifecycle_recovery(key)

    def update_config_interest(self, config_id: str, snapshot: object) -> None:
        self._interest_by_config[config_id] = snapshot

    def clear_config_interest(self, config_id: str) -> None:
        self._interest_by_config.pop(config_id, None)

    def has_config_interest(self, config_id: str) -> bool:
        return config_id in self._interest_by_config

    def record_runtime_contract(
        self,
        metadata: ActionMetadata,
        *,
        contract: ContractPointer | None = None,
    ) -> None:
        if (
            metadata.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            or metadata.provider_session_id is None
        ):
            return
        pointer = contract or _contract_pointer_for_provider_session(
            metadata.provider_session_id,
            provider_instance_id=metadata.provider_instance_id,
            provider_id=metadata.provider_id,
        )
        self._contracts[
            ProviderSessionKey(
                metadata.provider_instance_id,
                metadata.provider_id,
                metadata.provider_session_id,
            )
        ] = pointer

    def ingest_provider_entries(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
        entries: Iterable[ActionAvailabilityEntry],
        now: float | None = None,
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        record_now = self._now(now)
        for entry in entries:
            key = ProviderActionKey(provider_instance_id, entry.action_id)
            metadata = self._metadata_for_entry(
                key=key,
                provider_id=provider_id,
                provider_session_id=provider_session_id,
                provider_labels=provider_labels,
                entry=entry,
            )
            if entry.status == "available":
                if metadata is None:
                    continue
                existing = self._cache.record_for(key)
                requires_recovery = (
                    existing is not None
                    and existing.state == ActionAvailabilityState.UNAVAILABLE
                    and existing.reason == PROVIDER_SESSION_INVALID_REASON
                )
                self._cache.record_available(
                    metadata,
                    now=record_now,
                    requires_provider_lifecycle_recovery=requires_recovery,
                )
            elif entry.status == "unavailable":
                self._cache.record_unavailable(
                    key,
                    metadata=metadata,
                    reason=entry.reason,
                    now=record_now,
                )
            else:
                self._cache.record_probing(
                    key,
                    metadata=metadata,
                    reason=entry.reason,
                    now=record_now,
                )
            changed.add(key)
        return frozenset(changed)

    def _metadata_for_entry(
        self,
        *,
        key: ProviderActionKey,
        provider_id: str,
        provider_session_id: str | None,
        provider_labels: Mapping[str, str] | None,
        entry: ActionAvailabilityEntry,
    ) -> ActionMetadata | None:
        descriptor = entry.descriptor
        if descriptor is None and entry.status == "available":
            return None
        return ActionMetadata(
            uuid=entry.action_id,
            provider_instance_id=key.provider_instance_id,
            provider_id=provider_id,
            name=descriptor.name if descriptor is not None else None,
            provider_session_id=provider_session_id,
            provider_labels=provider_labels,
            settings_schema=(
                descriptor.settings_schema if descriptor is not None else None
            ),
            provider_settings_schema=(
                descriptor.provider_settings_schema
                if descriptor is not None
                else None
            ),
        )

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now


class _RecordingRuntimeSender:
    def __init__(
        self,
        *,
        actions_bus: EndpointSession,
        action_service: _BindingActionServiceHarness,
    ) -> None:
        self._actions_bus = actions_bus
        self._action_service = action_service
        self.messages: list[SimpleNamespace] = []
        self.block_action_instance_creates_for: set[str] = set()
        self.raise_for_message_types: dict[str, Exception] = {}

    async def send_action_runtime_message(
        self,
        *,
        provider_session_key: ProviderSessionKey | None,
        message_type: str,
        body,
    ) -> bool:
        if provider_session_key is None:
            return False
        target = (
            getattr(body, "metadata", None)
            or getattr(body, "binding", None)
            or getattr(body, "page_session", None)
        )
        assert target is not None
        contract = self._action_service.current_contract(provider_session_key)
        self.messages.append(
            SimpleNamespace(
                message_type=message_type,
                recipient_session_id=provider_session_key.provider_session_id,
                contract=contract,
                body=body,
            )
        )
        if (
            message_type == ACTION_INSTANCE_CREATED
            and getattr(target, "action_id", None)
            in self.block_action_instance_creates_for
        ):
            await anyio.sleep_forever()
        exc = self.raise_for_message_types.get(message_type)
        if exc is not None:
            raise exc
        await self._actions_bus.send(
            lane="actions",
            recipient=action_provider_address(
                provider_session_key.provider_instance_id
            ),
            recipient_session_id=provider_session_key.provider_session_id,
            subject=context_subject(
                target.context_id,
                provider_instance_id=target.provider_instance_id,
                provider_id=target.provider_id,
                config_id=target.config_id,
                action_instance_id=getattr(target, "action_instance_id", None),
                binding_id=getattr(target, "binding_id", None),
                page_session_id=getattr(target, "page_session_id", None),
            ),
            message_type=message_type,
            body=body.to_dict(),
            contract=contract,
        )
        return True


_ACTION_SERVICE_BY_BINDING_SERVICE_ID: dict[int, _BindingActionServiceHarness] = {}
_RUNTIME_SENDER_BY_BINDING_SERVICE_ID: dict[int, _RecordingRuntimeSender] = {}


def _action_service_for(
    manager: ControlBindingService,
) -> _BindingActionServiceHarness:
    return _ACTION_SERVICE_BY_BINDING_SERVICE_ID[id(manager)]


def _runtime_sender_for(
    manager: ControlBindingService,
) -> _RecordingRuntimeSender:
    return _RUNTIME_SENDER_BY_BINDING_SERVICE_ID[id(manager)]


def _record_provider_runtime_contract(
    manager: ControlBindingService,
    metadata: ActionMetadata,
    *,
    contract: ContractPointer | None = None,
) -> None:
    _action_service_for(manager).record_runtime_contract(metadata, contract=contract)


def _actions_bus() -> LaneHarness:
    return LaneHarness("actions", default_endpoint=CONTROLLER_ADDR)


def _actions_session(action_bus: LaneHarness) -> EndpointSession:
    return action_bus.endpoint(CONTROLLER_ADDR).session


def _make_binding_service(
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
    clock: Callable[[], float] | None = None,
    action_service: _BindingActionServiceHarness | None = None,
    page_timeout_check_interval: float = 0.25,
) -> ControlBindingService:
    service_action = action_service or _BindingActionServiceHarness(
        manager=manager,
        clock=clock,
    )
    runtime_sender = _RecordingRuntimeSender(
        actions_bus=actions_bus,
        action_service=service_action,
    )
    service = ControlBindingService(
        controller_id=controller_id,
        device=device,
        hardware_ref=hardware_ref,
        command_service=command_service,
        config=config,
        manager=manager,
        actions_bus=actions_bus,
        start_soon=start_soon,
        render_backend=render_backend,
        settings_service=settings_service,
        clock=clock,
        action_service=service_action,
        runtime_sender=runtime_sender,
        page_timeout_check_interval=page_timeout_check_interval,
    )
    _ACTION_SERVICE_BY_BINDING_SERVICE_ID[id(service)] = service_action
    _RUNTIME_SENDER_BY_BINDING_SERVICE_ID[id(service)] = runtime_sender
    return service


def _binding_lease_for_control(
    manager: ControlBindingService,
    control_id: str,
) -> BindingLeaseSnapshot | None:
    snapshot = manager.snapshot()
    context = snapshot.active_contexts.get(control_id)
    if context is not None:
        return snapshot.binding_leases.get(context.binding_id)
    output_owner = snapshot.output_owners.get(control_id)
    if output_owner is not None:
        return snapshot.binding_leases.get(output_owner)
    return next(
        (
            lease
            for lease in snapshot.binding_leases.values()
            if lease.control_id == control_id
        ),
        None,
    )


def _action_instance_body(metadata: ActionInstanceSnapshot) -> dict:
    return {
        "providerInstanceId": metadata.provider_instance_id,
        "providerId": metadata.provider_id,
        "actionId": metadata.action_id,
        "actionInstanceId": metadata.action_instance_id,
        "configId": metadata.config_id,
        "contextId": metadata.context_id,
    }


def _action_command(
    message_type: str,
    payload: dict | None = None,
    *,
    config_id: str = "test-device",
    context_id: str,
    action_instance_id: str,
    binding_id: str,
    page_session_id: str | None = None,
    contract: ContractPointer | None = None,
) -> DeckrMessage:
    return action_message(
        sender=PROVIDER_ADDR,
        sender_session_id=PROVIDER_SESSION_ID,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload or {},
        subject=context_subject(
            context_id,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            config_id=config_id,
            action_instance_id=action_instance_id,
            binding_id=binding_id,
            page_session_id=page_session_id,
        ),
        contract=contract or _contract_pointer_for_provider_session(),
    )


def _metadata(
    uuid: str,
    *,
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    provider_id: str = PROVIDER_ID,
    provider_session_id: str | None = PROVIDER_SESSION_ID,
) -> ActionMetadata:
    return ActionMetadata(
        uuid=uuid,
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        provider_session_id=provider_session_id,
    )


def _builtin_metadata(uuid: str) -> ActionMetadata:
    return _metadata(
        uuid,
        provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
        provider_id=BUILTIN_ACTION_PROVIDER_ID,
        provider_session_id=None,
    )


def _availability_entry(action_uuid: str) -> ActionAvailabilityEntry:
    return ActionAvailabilityEntry(
        actionId=action_uuid,
        status="available",
        descriptor=ActionDescriptor(
            actionId=action_uuid,
            providerId=PROVIDER_ID,
        ),
    )


def _seed_action_availability(
    manager: ControlBindingService,
    *metadatas: ActionMetadata,
) -> None:
    for metadata in metadatas:
        _record_provider_runtime_contract(manager, metadata)
        _action_service_for(manager).ingest_provider_entries(
            provider_instance_id=metadata.provider_instance_id,
            provider_id=metadata.provider_id,
            provider_session_id=metadata.provider_session_id,
            entries=(
                ActionAvailabilityEntry(
                    actionId=metadata.uuid,
                    status="available",
                    descriptor=ActionDescriptor(
                        actionId=metadata.uuid,
                        providerId=metadata.provider_id,
                    ),
                ),
            ),
        )


def _ingest_provider_entries(
    manager: ControlBindingService,
    *,
    provider_instance_id: str,
    provider_id: str,
    provider_session_id: str | None,
    entries: Iterable[ActionAvailabilityEntry],
) -> frozenset[ProviderActionKey]:
    return _action_service_for(manager).ingest_provider_entries(
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        provider_session_id=provider_session_id,
        entries=entries,
    )


def _record_lifecycle_unavailable(
    manager: ControlBindingService,
    *,
    provider_instance_id: str,
    provider_id: str,
    action_uuid: str,
    provider_session_id: str | None = None,
    reason: str | None = None,
    intent: ActionIntentKey | None = None,
) -> ProviderActionKey:
    return _action_service_for(manager).record_lifecycle_unavailable(
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        action_uuid=action_uuid,
        provider_session_id=provider_session_id,
        reason=reason,
        intent=intent,
    )


def _record_for_key(
    manager: ControlBindingService,
    key: ProviderActionKey,
) -> ActionAvailabilityRecord | None:
    return _action_service_for(manager).record_for_key(key)


async def _action_command_for_active_binding(
    manager: ControlBindingService,
    message_type: str,
    payload: dict | None = None,
    *,
    control_id: str = "0,0",
    config_id: str = "test-device",
) -> DeckrMessage:
    ctx = manager.context_for_control(control_id)
    assert ctx is not None
    return _action_command(
        message_type,
        payload,
        config_id=config_id,
        context_id=ctx.id,
        action_instance_id=ctx.action_instance_id,
        binding_id=ctx.binding_id,
        page_session_id=ctx.page_session_id,
    )


def _action_instance_command(
    message_type: str,
    payload: dict,
    *,
    context_id: str,
    action_instance_id: str,
    config_id: str = "test-device",
    sender_session_id: str = PROVIDER_SESSION_ID,
    contract: ContractPointer | None = None,
) -> DeckrMessage:
    return action_message(
        sender=PROVIDER_ADDR,
        sender_session_id=sender_session_id,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload,
        subject=context_subject(
            context_id,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            config_id=config_id,
            action_instance_id=action_instance_id,
        ),
        contract=contract
        or _contract_pointer_for_provider_session(sender_session_id),
    )


def _page_session_command(
    message_type: str,
    payload: dict,
    *,
    session_id: str,
    context_id: str,
    action_instance_id: str,
    config_id: str = "test-device",
    sender_session_id: str = PROVIDER_SESSION_ID,
    contract: ContractPointer | None = None,
) -> DeckrMessage:
    return action_message(
        sender=PROVIDER_ADDR,
        sender_session_id=sender_session_id,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload,
        subject=context_subject(
            context_id,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            config_id=config_id,
            action_instance_id=action_instance_id,
            page_session_id=session_id,
        ),
        contract=contract
        or _contract_pointer_for_provider_session(sender_session_id),
    )


def _make_control(
    control_id: str,
    row: int = 0,
    col: int = 0,
    kind: str = "key",
    events: list[str] | None = None,
    has_display: bool = True,
    raster_width: int = 72,
    raster_height: int = 72,
    raster_rotation: int | None = None,
) -> ControlDescriptor:
    if events is None:
        events = ["momentary", "press"]
    input_capabilities = []
    if "momentary" in events:
        input_capabilities.append(
            CapabilityDescriptor(
                capabilityId="button.momentary",
                family=DECKR_INPUT_BUTTON,
                type="momentary",
                direction="input",
                access=("emits",),
                eventTypes=("down", "up"),
            )
        )
    if "press" in events:
        input_capabilities.append(
            CapabilityDescriptor.model_validate(
                {
                    "capabilityId": "button.press",
                    "family": DECKR_INPUT_BUTTON,
                    "type": "activation",
                    "direction": "input",
                    "access": ["emits"],
                    "eventTypes": ["press"],
                }
            )
        )
    if "rotate" in events:
        input_capabilities.append(
            CapabilityDescriptor(
                capabilityId="encoder.relative",
                family=DECKR_INPUT_ENCODER,
                type="relative",
                direction="input",
                access=("emits",),
                eventTypes=("rotate",),
            )
        )
    if "tap" in events or "swipe" in events:
        event_types = []
        if "tap" in events:
            event_types.append("tap")
        if "swipe" in events:
            event_types.append("swipe")
        input_capabilities.append(
            CapabilityDescriptor(
                capabilityId="touch.gesture",
                family=DECKR_INPUT_TOUCH,
                type="gesture",
                direction="input",
                access=("emits",),
                eventTypes=tuple(event_types),
            )
        )
    output_capabilities = []
    if has_display:
        raster_constraints = [
            {
                "type": "fixed",
                "subject": "width",
                "value": raster_width,
                "unit": "pixel",
            },
            {
                "type": "fixed",
                "subject": "height",
                "value": raster_height,
                "unit": "pixel",
            },
        ]
        if raster_rotation is not None:
            raster_constraints.append(
                {
                    "type": "fixed",
                    "subject": "rotation",
                    "value": raster_rotation,
                    "unit": "degree",
                }
            )
        output_capabilities.append(
            CapabilityDescriptor.model_validate(
                {
                    "capabilityId": "raster.bitmap",
                    "family": DECKR_OUTPUT_RASTER,
                    "type": "bitmap",
                    "direction": "output",
                    "access": ["settable"],
                    "commandTypes": ["set_frame", "clear"],
                    "constraints": raster_constraints,
                }
            )
        )
    return ControlDescriptor(
        controlId=control_id,
        kind=kind,
        geometry=ControlGeometry(x=col, y=row, width=1, height=1, unit="grid"),
        inputCapabilities=tuple(input_capabilities),
        outputCapabilities=tuple(output_capabilities),
    )


def _make_mock_device(
    device_id: str = "test-device",
    controls: list[ControlDescriptor] | None = None,
) -> DeviceDescriptor:
    """Create device metadata for controller tests."""
    if controls is None:
        controls = [_make_control("0,0"), _make_control("1,0")]
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test Device",
        fingerprint=f"fingerprint:{device_id}",
        controls=tuple(controls),
    )


def _make_mars_msd_two_device() -> DeviceDescriptor:
    key_kwargs = {
        "kind": "key",
        "events": ["momentary"],
        "raster_width": 64,
        "raster_height": 64,
        "raster_rotation": 270,
    }
    controls = [
        _make_control("0,0", row=0, col=0, **key_kwargs),
        _make_control("1,0", row=0, col=1, **key_kwargs),
        _make_control("2,0", row=0, col=2, **key_kwargs),
        _make_control("0,1", row=1, col=0, **key_kwargs),
        _make_control("1,1", row=1, col=1, **key_kwargs),
        _make_control("2,1", row=1, col=2, **key_kwargs),
        _make_control(
            "3,0",
            row=0,
            col=3,
            kind="dial",
            events=["rotate", "momentary"],
            has_display=False,
        ),
        _make_control(
            "3,1",
            row=1,
            col=3,
            kind="dial",
            events=["rotate", "momentary"],
            has_display=False,
        ),
        _make_control(
            "4,1",
            row=1,
            col=4,
            kind="dial",
            events=["rotate", "momentary"],
            has_display=False,
        ),
        _make_control("B1", row=2, col=0, kind="button", events=["momentary"], has_display=False),
        _make_control("B2", row=2, col=1, kind="button", events=["momentary"], has_display=False),
        _make_control("B3", row=2, col=2, kind="button", events=["momentary"], has_display=False),
    ]
    return DeviceDescriptor(
        deviceId="mars-gaming-msd-two",
        displayName="Mars Gaming MSD-TWO",
        fingerprint="0B00:1001:0300D0785616",
        manufacturer="MiraBox",
        model="MSD-TWO",
        controls=tuple(controls),
    )


def _hardware_ref(device: DeviceDescriptor) -> DeviceRef:
    return DeviceRef(
        managerId="manager-main",
        deviceId=device.device_id,
    )


def _hardware_input(
    control_id: str,
    event_type: str,
    *,
    capability_id: str = "button.momentary",
    sequence: int | None = None,
    device_id: str = "test-device",
) -> DeckrMessage:
    return hw_messages.control_input_message(
        manager_id="manager-main",
        sender_session_id="manager-session",
        device_id=device_id,
        control_id=control_id,
        capability_id=capability_id,
        event_type=event_type,
        sequence=sequence,
    )


class FakeHardwareCommandService:
    def __init__(self):
        self.set_raster_frame = AsyncMock()
        self.clear_raster = AsyncMock()


def _solid_key_graph() -> SubGraphNode:
    """Minimal graph: solid dark gray background (canvas size from context)."""
    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (51, 51, 51, 255),  # #333333
            },
            deps=["canvas"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="bg"
    )


def _solid_key_image() -> str:
    graph = _solid_key_graph()
    return dump_graph_data_uri(graph.graph, output=graph.output)


def _dynamic_page(page_id: str, *control_ids: str) -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=tuple(
            PageChildBindingDescriptor(
                controlId=control_id,
                target=PageChildBindingTarget(kind="self"),
                itemKey=f"item-{ix}",
            )
            for ix, control_id in enumerate(control_ids)
        ),
    )


def _dynamic_page_with_action_child(
    page_id: str,
    control_id: str,
    *,
    action_id: str,
    provider_instance_id: str,
) -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=(
            PageChildBindingDescriptor(
                controlId=control_id,
                target=PageChildBindingTarget(
                    kind="action",
                    actionId=action_id,
                    providerInstanceId=provider_instance_id,
                    instanceKey=control_id,
                ),
            ),
        ),
    )


def _dynamic_page_with_action_children(
    page_id: str,
    *children: tuple[str, str],
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
) -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=tuple(
            PageChildBindingDescriptor(
                controlId=control_id,
                target=PageChildBindingTarget(
                    kind="action",
                    actionId=action_id,
                    providerInstanceId=provider_instance_id,
                    instanceKey=control_id,
                ),
            )
            for control_id, action_id in children
        ),
    )


class ControlledFrameBackend:
    """Backend used by tests to control completion order without blocking commands."""

    def __init__(self):
        self.calls: list[int] = []
        self.requests: list = []
        self._events: dict[int, anyio.Event] = {}

    async def render(self, request) -> RenderResult:
        self.calls.append(request.generation)
        self.requests.append(request)
        event = self._events.setdefault(request.generation, anyio.Event())
        await event.wait()
        return RenderResult(
            context_id=request.context_id,
            binding_id=request.binding_id,
            control_id=request.control_id,
            generation=request.generation,
            frame=f"frame-{request.generation}".encode(),
        )

    def release(self, generation: int) -> None:
        self._events.setdefault(generation, anyio.Event()).set()

    async def aclose(self) -> None:
        return


async def _wait_for_render_request(
    render_backend: ControlledFrameBackend,
    *,
    after: int = 0,
):
    with anyio.fail_after(5.0):
        while len(render_backend.requests) <= after:
            await anyio.sleep(0.01)
    return render_backend.requests[-1]


class SetRasterImageOnAppearAction:
    """Minimal action that sets a graph-backed raster image on will_appear."""

    uuid: str = "test.virtual.setops"

    async def on_will_appear(self, event, context):
        await context.set_raster_image(_solid_key_image())

    async def on_will_disappear(self, event, context):
        pass


class NoopAction:
    uuid: str = "test.virtual.noop"

    async def on_will_appear(self, event, context):
        pass

    async def on_will_disappear(self, event, context):
        pass


class CapturingBuiltinAction:
    def __init__(self, uuid: str):
        self.uuid = uuid
        self.contexts = []

    async def on_bind(self, context) -> None:
        self.contexts.append(context)

    async def on_unbind(self, context, reason: str) -> None:
        del context, reason

    async def on_input(self, context, event) -> None:
        del context, event


class MemoryConfigService:
    def __init__(self, config: DeviceConfig) -> None:
        self.config = config

    async def match_device(
        self,
        *,
        fingerprint: str,
        labels,
    ) -> DeviceConfig | None:
        del labels
        return self.config if self.config.match.fingerprint == fingerprint else None

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        return self.config if self.config.id == config_id else None

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        self.config = config
        return config

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        del config_id
        return self._subscribe()

    async def _subscribe(self) -> AsyncIterator[DeviceConfig | None]:
        yield self.config


async def _next_action_message(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 1.0,
) -> DeckrMessage:
    with anyio.fail_after(timeout):
        return await anext(stream)


async def _assert_no_action_message(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 0.1,
) -> None:
    with anyio.move_on_after(timeout) as scope:
        await anext(stream)
    assert scope.cancel_called


async def _drain_action_messages(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 0.05,
) -> None:
    while True:
        with anyio.move_on_after(timeout) as scope:
            await anext(stream)
        if scope.cancel_called:
            return


async def _collect_action_messages(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 0.05,
) -> list[DeckrMessage]:
    messages: list[DeckrMessage] = []
    while True:
        with anyio.move_on_after(timeout) as scope:
            message = await anext(stream)
        if scope.cancel_called:
            return messages
        messages.append(message)


async def _next_capability_input(
    stream: AsyncIterator[DeckrMessage],
) -> CapabilityInputBody:
    msg = await _next_action_message(stream)
    assert msg.message_type == CAPABILITY_INPUT
    return CapabilityInputBody.model_validate(msg.body)


@pytest.mark.asyncio
async def test_device_manager_tracks_visible_and_dynamic_page_action_interest():
    device = _make_mock_device()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action="action.owner",
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async def get_action(address: str, **kwargs) -> ActionMetadata:
        del kwargs
        return _metadata(address)

    registry = MagicMock()
    registry.get_action = get_action

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(_actions_bus()),
            start_soon=tg.start_soon,
            clock=lambda: 20.0,
        )
        _seed_action_availability(
            manager,
            _metadata("action.owner"),
            _metadata("action.child", provider_instance_id=PROVIDER_INSTANCE_ID),
        )
        await manager.set_page(profile="default", page=0)
        static_snapshot = manager.action_interest_snapshot(now=20.0)
        assert any(
            record.source == ActionInterestSource.VISIBLE_BINDING
            and record.strength == ActionInterestStrength.STRONG
            and record.intent.action_uuid == "action.owner"
            for record in static_snapshot.records
        )

        owner = manager.context_for_control("0,0")
        assert owner is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id="action.child",
                provider_instance_id=PROVIDER_INSTANCE_ID,
            ),
            context_id=owner.id,
        )

        dynamic_snapshot = manager.action_interest_snapshot(now=20.0)
        assert any(
            record.source == ActionInterestSource.DYNAMIC_PAGE
            and record.strength == ActionInterestStrength.STRONG
            and record.intent.action_uuid == "action.child"
            for record in dynamic_snapshot.records
        )
        assert any(
            record.source == ActionInterestSource.VISIBLE_BINDING
            and record.strength == ActionInterestStrength.WARM
            and record.intent.action_uuid == "action.owner"
            for record in dynamic_snapshot.records
        )
        tg.cancel_scope.cancel()


def _settings_reload_config() -> DeviceConfig:
    return DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                id="root-control",
                                selector={"control_id": "0,0"},
                                action=SetRasterImageOnAppearAction.uuid,
                                settings={"label": "old"},
                            )
                        ]
                    )
                ],
            )
        ],
    )


def _settings_reload_selector_only_config() -> DeviceConfig:
    return DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={
                                    "geometry": {
                                        "x": 0,
                                        "y": 0,
                                        "width": 1,
                                        "height": 1,
                                        "unit": "grid",
                                    }
                                },
                                action=SetRasterImageOnAppearAction.uuid,
                                settings={"label": "old"},
                            )
                        ]
                    )
                ],
            )
        ],
    )



@pytest.mark.asyncio
async def test_unsupported_action_message_for_active_binding_is_ignored():
    config_service = MemoryConfigService(_settings_reload_config())
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config_service.config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            settings_service=ConfigBackedSettingsService(
                controller_id=CONTROLLER_ID,
                config_service=config_service,
            ),
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            await manager.set_page(profile="default", page=0)
            old_ctx = manager.context_for_control("0,0")
            assert old_ctx is not None
            assert old_ctx.settings == {"label": "old"}
            assert old_ctx.settings_target is not None
            await _drain_action_messages(stream)

            await manager.handle_provider_command(
                DeckrMessage(
                    lane="actions",
                    messageType=UNSUPPORTED_ACTION_MESSAGE,
                    sender=PROVIDER_ADDR,
                    senderSessionId=PROVIDER_SESSION_ID,
                    recipient=endpoint_target(CONTROLLER_ADDR),
                    subject=context_subject(
                        old_ctx.id,
                        provider_instance_id=PROVIDER_INSTANCE_ID,
                        provider_id=PROVIDER_ID,
                        config_id="test-device",
                        action_instance_id=old_ctx.action_instance_id,
                        binding_id=old_ctx.binding_id,
                    ),
                    body={
                        "target": old_ctx.settings_target.to_dict(),
                        "settings": {"label": "new", "extra": True},
                    },
                    contract=_contract_pointer_for_provider_session(),
                )
            )

            new_ctx = manager.context_for_control("0,0")
            assert new_ctx is old_ctx
            assert new_ctx.settings == {"label": "old"}
            assert config_service.config.profiles[0].pages[0].controls[0].settings == {
                "label": "old"
            }
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest_asyncio.fixture
def device_config_set_raster_image():
    """Config: one profile, one page, one control with SetRasterImageOnAppearAction."""
    return DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=SetRasterImageOnAppearAction.uuid,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_missing_action_renders_missing_unavailable_fallback():
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=None)
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=ACTION_X_UUID,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )

        await manager.set_page(profile="default", page=0)
        request = await _wait_for_render_request(render_backend)
        assert request.source is not None
        assert request.source.command_type == "controller_fallback"
        assert request.source.content_kind == "overlay:unavailable_missing"
        assert request.source.availability_cause == "missing"
        assert request.source.availability_reason is None
        render_backend.release(request.generation)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_service_unavailable_renders_service_fallback():
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=None)
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=ACTION_X_UUID,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _record_lifecycle_unavailable(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            action_uuid=ACTION_X_UUID,
            provider_session_id=PROVIDER_SESSION_ID,
            reason="sonos_service_unavailable",
        )

        await manager.set_page(profile="default", page=0)
        request = await _wait_for_render_request(render_backend)
        assert request.source is not None
        assert request.source.content_kind == "overlay:unavailable_service"
        assert request.source.availability_cause == "service"
        assert request.source.availability_state == "unavailable"
        assert request.source.availability_source == "service_view"
        assert request.source.availability_reason == "sonos_service_unavailable"
        render_backend.release(request.generation)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_provider_session_invalidation_renders_session_fallback(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        lease = _binding_lease_for_control(manager, "0,0")
        assert lease is not None
        key = _record_lifecycle_unavailable(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            provider_session_id=PROVIDER_SESSION_ID,
            action_uuid=SetRasterImageOnAppearAction.uuid,
            reason=PROVIDER_SESSION_INVALID_REASON,
            intent=lease.planned_intent,
        )

        await manager.on_action_availability_changed(frozenset({key}))
        request = await _wait_for_render_request(render_backend)
        assert request.source is not None
        assert request.source.content_kind == "overlay:unavailable_session"
        assert request.source.availability_cause == "session"
        assert request.source.availability_reason == PROVIDER_SESSION_INVALID_REASON
        render_backend.release(request.generation)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_nonterminal_lifecycle_rejection_renders_rejected_fallback(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = manager.context_for_control("0,0")
        assert ctx is not None

        msg = await _action_command_for_active_binding(
            manager,
            ACTION_LIFECYCLE_REJECTED,
            {
                "targetKind": "binding",
                "binding": ctx.metadata.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "reason": "provider_not_ready",
            },
        )
        await manager.handle_provider_command(msg)

        request = await _wait_for_render_request(render_backend)
        assert request.source is not None
        assert request.source.content_kind == "overlay:unavailable_rejected"
        assert request.source.availability_cause == "rejected"
        assert request.source.availability_reason == "provider_not_ready"
        render_backend.release(request.generation)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_binding_overlay_and_clear_source_reaches_render_request(
    device_config_set_raster_image, persistence_tmp_dir
):
    from deckr.actions.messages import BINDING_OVERLAY, BINDING_OVERLAY_CLEAR

    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

    async def wait_for_render_count(count: int) -> None:
        with anyio.fail_after(5.0):
            while len(render_backend.calls) < count:
                await anyio.sleep(0.01)

    async def release_and_wait_for_frame(count: int) -> None:
        render_backend.release(render_backend.calls[-1])
        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count < count:
                await anyio.sleep(0.01)

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = manager.context_for_control("0,0")
        assert ctx is not None
        binding = ctx.metadata.model_copy(update={"output_generation": 1})

        base_msg = await _action_command_for_active_binding(
            manager,
            BINDING_OUTPUT,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "capability": {
                    "deviceRef": {
                        "managerId": "manager-main",
                        "deviceId": "test-device",
                    },
                    "controlId": "0,0",
                    "capabilityId": "raster.bitmap",
                },
                "commandType": "set_frame",
                "params": {"image": _solid_key_image()},
                "generation": 1,
            },
        )
        await manager.handle_provider_command(base_msg)
        await wait_for_render_count(1)
        base_request = render_backend.requests[-1]
        await release_and_wait_for_frame(1)

        overlay_msg = await _action_command_for_active_binding(
            manager,
            BINDING_OVERLAY,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "template": "ok",
                "title": "OK",
                "durationSeconds": 30.0,
                "overlayId": "save-feedback",
                "generation": 1,
            },
        )
        await manager.handle_provider_command(overlay_msg)
        await wait_for_render_count(2)
        overlay_request = render_backend.requests[-1]
        await release_and_wait_for_frame(2)

        clear_msg = await _action_command_for_active_binding(
            manager,
            BINDING_OVERLAY_CLEAR,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "overlayId": "save-feedback",
                "generation": 2,
            },
        )
        await manager.handle_provider_command(clear_msg)
        await wait_for_render_count(3)
        clear_request = render_backend.requests[-1]
        await release_and_wait_for_frame(3)
        tg.cancel_scope.cancel()

    assert base_request.source is not None
    assert base_request.source.command_type == "set_frame"
    assert base_request.source.content_kind == "invariant_graph"
    assert base_request.source.overlay_generation is None

    assert overlay_request.source is not None
    assert overlay_request.source.command_type == BINDING_OVERLAY
    assert overlay_request.source.content_kind == "overlay:ok"
    assert overlay_request.source.overlay_generation == 1
    assert overlay_request.source.binding_output_generation == 1
    assert overlay_request.source.action_message_id == overlay_msg.message_id

    assert clear_request.source is not None
    assert clear_request.source.command_type == BINDING_OVERLAY_CLEAR
    assert clear_request.source.content_kind == "invariant_graph"
    assert clear_request.source.overlay_generation == 2
    assert clear_request.source.binding_output_generation == 1
    assert clear_request.source.action_message_id == clear_msg.message_id


@pytest.mark.asyncio
async def test_binding_without_live_provider_session_remains_pending(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(
            SetRasterImageOnAppearAction.uuid,
            provider_session_id=None,
        )
    )
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(
                    SetRasterImageOnAppearAction.uuid,
                    provider_session_id=None,
                ),
            )
            await manager.set_page(profile="default", page=0)
            ctx = manager.context_for_control("0,0")
            assert ctx is None
            assert manager.snapshot().binding_leases == {}
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_binding_activation_timeout_does_not_block_static_page_bind_loop(
    monkeypatch,
):
    blocked_action = "test.virtual.blocked"
    device = _make_mock_device()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=blocked_action,
                                settings={},
                            ),
                            Control(
                                selector={"control_id": "1,0"},
                                action=NoopAction.uuid,
                                settings={},
                            ),
                        ]
                    )
                ],
            )
        ],
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, **_: _metadata(uuid),
    )
    action_bus = _actions_bus()

    monkeypatch.setattr(
        binding_service_module,
        "ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS",
        0.01,
    )
    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(blocked_action),
        _metadata(NoopAction.uuid),
    )
    runtime_sender = _runtime_sender_for(manager)
    assert isinstance(runtime_sender, _RecordingRuntimeSender)
    runtime_sender.block_action_instance_creates_for.add(blocked_action)

    with anyio.fail_after(1):
        await manager.set_page(profile="default", page=0)

    blocked_lease = _binding_lease_for_control(manager, "0,0")
    resolved_lease = _binding_lease_for_control(manager, "1,0")
    assert blocked_lease is not None
    assert not blocked_lease.attached
    assert manager.context_for_control("0,0") is None
    assert blocked_lease.action_instance_id not in manager.snapshot().action_instances
    assert resolved_lease is not None
    assert resolved_lease.attached
    assert manager.context_for_control("1,0") is not None


@pytest.mark.asyncio
async def test_settings_snapshot_timeout_does_not_block_static_page_bind_loop(
    monkeypatch,
):
    class HangingSettingsService:
        async def get(self, target):
            await anyio.sleep_forever()

    device = _make_mock_device()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=ACTION_X_UUID,
                                settings={"configured": "first"},
                            ),
                            Control(
                                selector={"control_id": "1,0"},
                                action=NoopAction.uuid,
                                settings={"configured": "second"},
                            ),
                        ]
                    )
                ],
            )
        ],
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, **_: _metadata(uuid),
    )
    action_bus = _actions_bus()
    monkeypatch.setattr(
        binding_service_module,
        "SETTINGS_SERVICE_TIMEOUT_SECONDS",
        0.01,
    )
    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
        settings_service=HangingSettingsService(),
    )
    _seed_action_availability(
        manager,
        _metadata(ACTION_X_UUID),
        _metadata(NoopAction.uuid),
    )

    with anyio.fail_after(1):
        await manager.set_page(profile="default", page=0)

    first = manager.context_for_control("0,0")
    second = manager.context_for_control("1,0")
    assert first is not None
    assert first.settings == {"configured": "first"}
    assert second is not None
    assert second.settings == {"configured": "second"}


@pytest.mark.asyncio
async def test_static_binding_lifecycle_messages_carry_settings_only_on_attach():
    device = _make_mock_device()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=ACTION_X_UUID,
                                settings={"configured": "first"},
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, **_: _metadata(uuid),
    )
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(manager, _metadata(ACTION_X_UUID))
            await manager.set_page(profile="default", page=0)

            messages = await _collect_action_messages(stream)

        tg.cancel_scope.cancel()

    created = [
        message
        for message in messages
        if message.message_type == ACTION_INSTANCE_CREATED
    ]
    attached = [
        message for message in messages if message.message_type == BINDING_ATTACHED
    ]
    assert len(created) == 1
    assert len(attached) == 1
    assert "settings" not in created[0].body
    assert "internal" not in created[0].body
    assert attached[0].body["settings"] == {"configured": "first"}
    assert attached[0].body["internal"] == {}


@pytest.mark.asyncio
async def test_service_view_pending_preserves_attached_binding(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            ctx = manager.context_for_control("0,0")
            lease = _binding_lease_for_control(manager, "0,0")
            assert ctx is not None
            assert lease is not None
            assert lease.attached
            await _drain_action_messages(stream)
            manager._render_pending_to_control = AsyncMock()

            changed = _ingest_provider_entries(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
                provider_id=PROVIDER_ID,
                provider_session_id=PROVIDER_SESSION_ID,
                entries=(
                    ActionAvailabilityEntry(
                        actionId=SetRasterImageOnAppearAction.uuid,
                        status="probing",
                        reason="successor_lease_negotiating",
                    ),
                ),
            )
            assert changed == frozenset(
                {
                    ProviderActionKey(
                        PROVIDER_INSTANCE_ID,
                        SetRasterImageOnAppearAction.uuid,
                    )
                }
            )
            await manager.on_action_availability_changed(changed)

            assert manager.context_for_control("0,0") is ctx
            preserved = _binding_lease_for_control(manager, "0,0")
            assert preserved is not None
            assert preserved.binding_id == lease.binding_id
            assert lease.attached
            manager._render_pending_to_control.assert_not_awaited()
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_availability_recovers_same_session_after_invalidated(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        runtime_sender = _runtime_sender_for(manager)
        assert isinstance(runtime_sender, _RecordingRuntimeSender)
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            ctx = manager.context_for_control("0,0")
            lease = _binding_lease_for_control(manager, "0,0")
            assert ctx is not None
            assert lease is not None
            assert lease.provider_session_id == PROVIDER_SESSION_ID
            await _drain_action_messages(stream)
            runtime_sender.messages.clear()

            key = _record_lifecycle_unavailable(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
                provider_id=PROVIDER_ID,
                provider_session_id=PROVIDER_SESSION_ID,
                action_uuid=SetRasterImageOnAppearAction.uuid,
                reason=PROVIDER_SESSION_INVALID_REASON,
                intent=lease.planned_intent,
            )
            changed = _ingest_provider_entries(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
                provider_id=PROVIDER_ID,
                provider_session_id=PROVIDER_SESSION_ID,
                entries=[_availability_entry(SetRasterImageOnAppearAction.uuid)],
            )
            record = _record_for_key(manager, key)
            assert record is not None
            assert record.requires_provider_lifecycle_recovery

            await manager.on_action_availability_changed(changed)

            replacement_ctx = manager.context_for_control("0,0")
            replacement_lease = _binding_lease_for_control(manager, "0,0")
            assert replacement_ctx is not None
            assert replacement_ctx is not ctx
            assert replacement_lease is not None
            assert replacement_lease.binding_id != lease.binding_id
            assert replacement_lease.provider_session_id == PROVIDER_SESSION_ID
            assert replacement_lease.attached
            assert not _action_service_for(manager).provider_lifecycle_recovery_required(key)

            await _collect_action_messages(stream)
            messages = runtime_sender.messages
            created = [
                msg
                for msg in messages
                if msg.message_type == ACTION_INSTANCE_CREATED
            ]
            destroyed = [
                msg
                for msg in messages
                if msg.message_type == ACTION_INSTANCE_DESTROYED
            ]
            attached = [
                msg for msg in messages if msg.message_type == BINDING_ATTACHED
            ]
            detached = [
                msg for msg in messages if msg.message_type == BINDING_DETACHED
            ]
            assert len(created) == 1
            assert len(destroyed) == 1
            assert len(attached) == 1
            assert len(detached) == 1
            assert destroyed[0].recipient_session_id == PROVIDER_SESSION_ID
            assert detached[0].recipient_session_id == PROVIDER_SESSION_ID
            assert created[0].recipient_session_id == PROVIDER_SESSION_ID
            assert attached[0].recipient_session_id == PROVIDER_SESSION_ID

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_provider_session_contract_change_replaces_matching_binding(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    metadata = _metadata(SetRasterImageOnAppearAction.uuid)
    registry.get_action = AsyncMock(return_value=metadata)
    action_bus = _actions_bus()
    first_contract = ContractPointer(contractId="provider-session-contract-1", generation=1)
    next_contract = ContractPointer(contractId="provider-session-contract-2", generation=1)
    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        runtime_sender = _runtime_sender_for(manager)
        assert isinstance(runtime_sender, _RecordingRuntimeSender)
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                metadata,
            )
            _record_provider_runtime_contract(manager, metadata, contract=first_contract)
            await manager.set_page(profile="default", page=0)
            ctx = manager.context_for_control("0,0")
            lease = _binding_lease_for_control(manager, "0,0")
            assert ctx is not None
            assert lease is not None
            assert lease.contract == first_contract
            await _drain_action_messages(stream)
            runtime_sender.messages.clear()

            _record_provider_runtime_contract(manager, metadata, contract=next_contract)
            await manager.on_action_availability_changed(
                {
                    ProviderActionKey(
                        PROVIDER_INSTANCE_ID,
                        SetRasterImageOnAppearAction.uuid,
                    )
                }
            )

            replacement_ctx = manager.context_for_control("0,0")
            replacement_lease = _binding_lease_for_control(manager, "0,0")
            assert replacement_ctx is not None
            assert replacement_ctx is not ctx
            assert replacement_lease is not None
            assert replacement_lease.binding_id != lease.binding_id
            assert replacement_lease.provider_session_id == PROVIDER_SESSION_ID
            assert replacement_lease.contract == next_contract
            assert replacement_lease.attached

            await _collect_action_messages(stream)
            messages = runtime_sender.messages
            created = [
                msg
                for msg in messages
                if msg.message_type == ACTION_INSTANCE_CREATED
            ]
            attached = [
                msg for msg in messages if msg.message_type == BINDING_ATTACHED
            ]
            assert len(created) == 1
            assert len(attached) == 1
            assert created[0].contract == next_contract
            assert attached[0].contract == next_contract

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_attached_binding_survives_nonterminal_provider_session_unavailable(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()
    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(_actions_bus()),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = manager.context_for_control("0,0")
    assert ctx is not None
    lease = _binding_lease_for_control(manager, "0,0")
    assert lease is not None
    assert lease.attached
    command_service.clear_raster.reset_mock()

    await manager.on_action_availability_changed(
        {ProviderActionKey(PROVIDER_INSTANCE_ID, SetRasterImageOnAppearAction.uuid)}
    )

    assert manager.context_for_control("0,0") is ctx
    preserved = _binding_lease_for_control(manager, "0,0")
    assert preserved is not None
    assert preserved.binding_id == lease.binding_id
    assert lease.attached
    command_service.clear_raster.assert_not_awaited()

    command = await _action_command_for_active_binding(
        manager,
        BINDING_OUTPUT,
        {
            "binding": ctx.metadata.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            "capability": {
                "deviceRef": {
                    "managerId": "manager-main",
                    "deviceId": "test-device",
                },
                "controlId": "0,0",
                "capabilityId": "raster.bitmap",
            },
            "commandType": "clear",
            "generation": ctx.metadata.output_generation,
        },
    )
    await manager.handle_provider_command(
        command.model_copy(update={"sender_session_id": "stale"})
    )
    command_service.clear_raster.assert_not_awaited()

    await manager.handle_provider_command(command)

    command_service.clear_raster.assert_awaited_once_with(
        "test-device",
        "0,0",
        "raster.bitmap",
    )


@pytest.mark.asyncio
async def test_dynamic_page_owner_moves_to_successor_provider_session(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    action_bus = _actions_bus()
    successor_session_id = "new-provider-session"

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            owner_ctx = manager.context_for_control("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page("dynamic-page", "1,0"),
                context_id=owner_ctx.id,
            )
            session = manager.active_page_session()
            assert session is not None
            child_ctx = manager.context_for_control("1,0")
            assert child_ctx is not None
            child_lease = _binding_lease_for_control(manager, "1,0")
            assert child_lease is not None
            action_instance = manager.snapshot().action_instances[session.action_instance_id]
            assert session.owner_provider_session_id == PROVIDER_SESSION_ID
            await _drain_action_messages(stream)

            changed = _ingest_provider_entries(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
                provider_id=PROVIDER_ID,
                provider_session_id=successor_session_id,
                entries=[_availability_entry(SetRasterImageOnAppearAction.uuid)],
            )
            _record_provider_runtime_contract(
                manager,
                _metadata(
                    SetRasterImageOnAppearAction.uuid,
                    provider_session_id=successor_session_id,
                ),
            )
            await manager.on_action_availability_changed(changed)

            replacement_child_ctx = manager.context_for_control("1,0")
            assert replacement_child_ctx is not None
            assert replacement_child_ctx is not child_ctx
            assert replacement_child_ctx.provider_session_id == successor_session_id
            replacement_lease = _binding_lease_for_control(manager, "1,0")
            assert replacement_lease is not None
            assert replacement_lease.binding_id != child_lease.binding_id
            assert replacement_lease.provider_session_id == successor_session_id
            assert manager.active_page_session() is session
            assert session.owner_provider_session_id == successor_session_id
            assert (
                manager.snapshot().action_instances[session.action_instance_id]
                == action_instance
            )
            action_session = manager.snapshot().provider_session_keys[
                session.action_instance_id
            ]
            assert action_session is not None
            assert action_session.provider_session_id == successor_session_id

            await manager.handle_provider_command(
                _page_session_command(
                    CLOSE_PAGE,
                    {},
                    session_id=session.page_session_id,
                    context_id=session.context_id,
                    action_instance_id=session.action_instance_id,
                    sender_session_id=successor_session_id,
                    contract=_contract_pointer_for_provider_session(
                        successor_session_id
                    ),
                )
            )

            assert manager.active_page_session() is None
            restored_ctx = manager.context_for_control("0,0")
            assert restored_ctx is not None
            assert restored_ctx.provider_session_id == successor_session_id

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_dynamic_page_restores_cached_static_plan_without_metadata_lookup(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        session = manager.active_page_session()
        assert session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx

        registry.get_action.return_value = None
        registry.get_action.reset_mock()
        await manager.close_page(context_id=session.context_id)

        registry.get_action.assert_not_awaited()
        restored_ctx = manager.context_for_control("0,0")
        assert restored_ctx is not None
        assert restored_ctx.provider_session_id == PROVIDER_SESSION_ID
        assert manager.active_page_session() is None

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_dynamic_page_replace_preserves_rebound_control_outputs(
    device_config_set_raster_image, persistence_tmp_dir
):
    """Dynamic page replacement should not blank controls that are rebound immediately."""
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        registry.get_action.reset_mock()
        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "0,0", "1,0"),
            context_id=owner_ctx.id,
        )
        assert registry.get_action.await_count == 0
        session = manager.active_page_session()
        assert session is not None

        command_service.clear_raster.reset_mock()
        await manager.replace_page(
            descriptor=_dynamic_page(session.page_id, "0,0", "1,0"),
            context_id=session.context_id,
        )

        command_service.clear_raster.assert_not_awaited()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_action_availability_refresh_repaints_existing_binding_output(
    device_config_set_raster_image, persistence_tmp_dir, caplog
):
    """Existing leases must repaint cached output during availability reconciliation."""
    caplog.set_level(logging.DEBUG, logger="deckr.controller._bindings._service")
    caplog.set_level(logging.DEBUG, logger="deckr.controller._command_router")
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = manager.context_for_control("0,0")
        assert ctx is not None

        binding = ctx.metadata.model_copy(update={"output_generation": 1})
        msg = await _action_command_for_active_binding(
            manager,
            BINDING_OUTPUT,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "capability": {
                    "deviceRef": {
                        "managerId": "manager-main",
                        "deviceId": "test-device",
                    },
                    "controlId": "0,0",
                    "capabilityId": "raster.bitmap",
                },
                "commandType": "set_frame",
                "params": {"image": "ZnJhbWU=", "encoding": "jpeg"},
                "generation": 1,
            },
        )
        await manager.handle_provider_command(msg)

        with anyio.fail_after(5.0):
            while not render_backend.calls:
                await anyio.sleep(0.01)
        render_backend.release(render_backend.calls[-1])
        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count == 0:
                await anyio.sleep(0.01)

        baseline_render_calls = len(render_backend.calls)
        baseline_output_calls = command_service.set_raster_frame.call_count
        caplog.clear()

        await manager.on_action_availability_changed()

        with anyio.fail_after(5.0):
            while len(render_backend.calls) <= baseline_render_calls:
                await anyio.sleep(0.01)
        render_backend.release(render_backend.calls[-1])
        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count <= baseline_output_calls:
                await anyio.sleep(0.01)
        assert "Action availability page refresh decision" in caplog.text
        assert "changed_keys=0 affected=True" in caplog.text
        assert "Refreshing cached binding output" in caplog.text
        assert "content_kind=" in caplog.text
        assert "Command router render enqueue" in caplog.text
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_mars_same_control_dynamic_page_rebinds_opener_slot(persistence_tmp_dir):
    device = _make_mars_msd_two_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    config = DeviceConfig(
        id="mars-gaming-msd-two",
        name="Mars Gaming MSD-TWO",
        match={"fingerprint": device.fingerprint},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "1,0"},
                                action=SetRasterImageOnAppearAction.uuid,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            await _drain_action_messages(stream)
            owner_ctx = manager.context_for_control("1,0")
            assert owner_ctx is not None

            await manager.handle_hardware_input(
                _hardware_input("1,0", "down", sequence=1, device_id=device.device_id)
            )
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == owner_ctx.binding_id

            await manager.open_page(
                descriptor=_dynamic_page("album-page", "1,0", "2,0"),
                context_id=owner_ctx.id,
            )
            child_ctx = manager.context_for_control("1,0")
            assert child_ctx is not None
            assert child_ctx.binding_id != owner_ctx.binding_id
            body = await _next_capability_input(stream)
            assert body.event.event_type == "cancel"
            assert body.binding.binding_id == owner_ctx.binding_id
            await _drain_action_messages(stream)

            await manager.handle_hardware_input(
                _hardware_input("1,0", "up", sequence=2, device_id=device.device_id)
            )
            await _assert_no_action_message(stream)

            await manager.handle_hardware_input(
                _hardware_input("1,0", "down", sequence=3, device_id=device.device_id)
            )
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == child_ctx.binding_id

            await manager.handle_hardware_input(
                _hardware_input("1,0", "up", sequence=4, device_id=device.device_id)
            )
            body = await _next_capability_input(stream)
            assert body.event.event_type == "up"
            assert body.binding.binding_id == child_ctx.binding_id
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_held_input_cancelled_when_config_is_removed(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            await _drain_action_messages(stream)
            ctx = manager.context_for_control("0,0")
            assert ctx is not None

            await manager.handle_hardware_input(_hardware_input("0,0", "down", sequence=1))
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == ctx.binding_id

            await manager.on_config_changed(None)

            body = await _next_capability_input(stream)
            assert body.event.event_type == "cancel"
            assert body.binding.binding_id == ctx.binding_id
            await _drain_action_messages(stream)
            assert not manager.config_active
            assert manager.context_for_control("0,0") is None
            assert manager.snapshot().binding_leases == {}
            assert not _action_service_for(manager).has_config_interest(manager.config_id)

            await manager.handle_hardware_input(_hardware_input("0,0", "up", sequence=2))
            await _assert_no_action_message(stream)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_dynamic_page_restores_when_close_notification_fails(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        runtime_sender = _runtime_sender_for(manager)
        assert isinstance(runtime_sender, _RecordingRuntimeSender)
        runtime_sender.raise_for_message_types[PAGE_SESSION_CLOSED] = RuntimeError(
            "provider offline"
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        session = manager.active_page_session()
        assert session is not None

        await manager.close_page(context_id=session.context_id)

        assert manager.active_page_session() is None
        assert manager.context_for_control("0,0") is not None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_dynamic_page_destroys_external_action_child(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
                _metadata("test.virtual.child"),
            )
            await manager.set_page(profile="default", page=0)
            owner_ctx = manager.context_for_control("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page_with_action_child(
                    "dynamic-page",
                    "1,0",
                    action_id="test.virtual.child",
                    provider_instance_id=PROVIDER_INSTANCE_ID,
                ),
                context_id=owner_ctx.id,
            )
            session = manager.active_page_session()
            assert session is not None
            child_lease = _binding_lease_for_control(manager, "1,0")
            assert child_lease is not None
            child_action_instance_id = child_lease.action_instance_id
            assert child_action_instance_id in manager.snapshot().action_instances
            await _drain_action_messages(stream)

            await manager.close_page(context_id=session.context_id)

            messages = await _collect_action_messages(stream)
            destroyed = [
                message
                for message in messages
                if message.message_type == ACTION_INSTANCE_DESTROYED
                and message.body["metadata"]["actionInstanceId"]
                == child_action_instance_id
            ]
            assert child_action_instance_id not in manager.snapshot().action_instances
            assert len(destroyed) == 1
            assert destroyed[0].body["reason"] == "close"
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_replace_dynamic_page_remounts_all_external_action_children(
    device_config_set_raster_image,
):
    device = _make_mock_device(
        controls=[
            _make_control("0,0"),
            _make_control("1,0"),
            _make_control("2,0"),
        ]
    )
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
                _metadata("test.virtual.keep"),
                _metadata("test.virtual.drop"),
            )
            await manager.set_page(profile="default", page=0)
            owner_ctx = manager.context_for_control("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page_with_action_children(
                    "dynamic-page",
                    ("1,0", "test.virtual.keep"),
                    ("2,0", "test.virtual.drop"),
                ),
                context_id=owner_ctx.id,
            )
            session = manager.active_page_session()
            assert session is not None
            keep_lease = _binding_lease_for_control(manager, "1,0")
            drop_lease = _binding_lease_for_control(manager, "2,0")
            assert keep_lease is not None
            assert drop_lease is not None
            keep_action_instance_id = keep_lease.action_instance_id
            drop_action_instance_id = drop_lease.action_instance_id
            await _drain_action_messages(stream)

            await manager.replace_page(
                descriptor=_dynamic_page_with_action_children(
                    session.page_id,
                    ("1,0", "test.virtual.keep"),
                ),
                context_id=session.context_id,
            )

            replacement_keep = _binding_lease_for_control(manager, "1,0")
            assert replacement_keep is not None
            assert replacement_keep.action_instance_id != keep_action_instance_id
            assert replacement_keep.action_instance_id in manager.snapshot().action_instances
            assert keep_action_instance_id not in manager.snapshot().action_instances
            assert drop_action_instance_id not in manager.snapshot().action_instances

            messages = await _collect_action_messages(stream)
            destroyed_ids = [
                message.body["metadata"]["actionInstanceId"]
                for message in messages
                if message.message_type == ACTION_INSTANCE_DESTROYED
            ]
            assert set(destroyed_ids) == {
                keep_action_instance_id,
                drop_action_instance_id,
            }
            destroyed_reasons = {
                message.body["metadata"]["actionInstanceId"]: message.body["reason"]
                for message in messages
                if message.message_type == ACTION_INSTANCE_DESTROYED
            }
            assert destroyed_reasons == {
                keep_action_instance_id: "page_child_removed",
                drop_action_instance_id: "page_child_removed",
            }
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_dynamic_page_external_child_lifecycle_attach_carries_settings(
    device_config_set_raster_image,
):
    child_action_id = "test.virtual.child"
    device = _make_mock_device(controls=[_make_control("0,0"), _make_control("1,0")])
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
                _metadata(child_action_id),
            )
            await manager.set_page(profile="default", page=0)
            owner_ctx = manager.context_for_control("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=DynamicPageCommand(
                    pageId="dynamic-page",
                    bindings=(
                        PageChildBindingDescriptor(
                            controlId="1,0",
                            target=PageChildBindingTarget(
                                kind="action",
                                actionId=child_action_id,
                                providerInstanceId=PROVIDER_INSTANCE_ID,
                                instanceKey="child",
                            ),
                            settings={"zoneName": "Bedroom"},
                        ),
                    ),
                ),
                context_id=owner_ctx.id,
            )

            messages = await _collect_action_messages(stream)

        tg.cancel_scope.cancel()

    created = [
        message
        for message in messages
        if message.message_type == ACTION_INSTANCE_CREATED
        and message.body["metadata"]["actionId"] == child_action_id
    ]
    attached = [
        message
        for message in messages
        if message.message_type == BINDING_ATTACHED
        and message.body["binding"]["actionId"] == child_action_id
    ]
    assert len(created) == 1
    assert len(attached) == 1
    assert "settings" not in created[0].body
    assert attached[0].body["settings"] == {"zoneName": "Bedroom"}
    assert attached[0].body["internal"] == {}


@pytest.mark.asyncio
async def test_external_action_child_owner_survives_successor_dynamic_page(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
                _metadata("test.virtual.child"),
            )
            await manager.set_page(profile="default", page=0)
            owner_ctx = manager.context_for_control("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page_with_action_child(
                    "first-page",
                    "1,0",
                    action_id="test.virtual.child",
                    provider_instance_id=PROVIDER_INSTANCE_ID,
                ),
                context_id=owner_ctx.id,
            )
            first_session = manager.active_page_session()
            assert first_session is not None
            child_ctx = manager.context_for_control("1,0")
            assert child_ctx is not None
            child_action_instance_id = child_ctx.action_instance_id
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page("successor-page", "0,0"),
                context_id=child_ctx.id,
            )

            successor_session = manager.active_page_session()
            assert successor_session is not None
            assert successor_session.page_id == "successor-page"
            assert successor_session.page_session_id != first_session.page_session_id
            assert successor_session.action_instance_id == child_action_instance_id
            assert child_action_instance_id in manager.snapshot().action_instances

            messages = await _collect_action_messages(stream)
            closed_sessions = [
                message
                for message in messages
                if message.message_type == PAGE_SESSION_CLOSED
                and message.body["pageSession"]["pageSessionId"]
                == first_session.page_session_id
            ]
            destroyed_ids = [
                message.body["metadata"]["actionInstanceId"]
                for message in messages
                if message.message_type == ACTION_INSTANCE_DESTROYED
            ]
            assert len(closed_sessions) == 1
            assert closed_sessions[0].body["reason"] == "open_page"
            assert child_action_instance_id not in destroyed_ids
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_open_page_from_dynamic_child_dismisses_previous_owner(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
            _metadata(
                "test.virtual.child",
                provider_instance_id="python-child",
            ),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "first-page",
                "1,0",
                action_id="test.virtual.child",
                provider_instance_id="python-child",
            ),
            context_id=owner_ctx.id,
        )
        first_session = manager.active_page_session()
        assert first_session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("second-page", "0,0"),
            context_id=child_ctx.id,
        )

        second_session = manager.active_page_session()
        assert second_session is not None
        assert second_session.page_id == "second-page"
        assert second_session.page_session_id != first_session.page_session_id
        assert second_session.owner_binding_id == child_ctx.binding_id
        assert second_session.owner_provider_instance_id == "python-child"

        await manager.close_page(context_id=second_session.context_id)

        assert manager.active_page_session() is None
        restored_owner_ctx = manager.context_for_control("0,0")
        assert restored_owner_ctx is not None
        assert restored_owner_ctx.page_session_id is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_stale_opener_binding_cannot_open_page_after_transition(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        stale_open = _action_command(
            OPEN_PAGE,
            {
                "descriptor": _dynamic_page("stale-page", "0,0").model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                )
            },
            context_id=owner_ctx.id,
            action_instance_id=owner_ctx.action_instance_id,
            binding_id=owner_ctx.binding_id,
        )

        await manager.open_page(
            descriptor=_dynamic_page("active-page", "1,0"),
            context_id=owner_ctx.id,
        )
        session = manager.active_page_session()
        assert session is not None
        assert session.page_id == "active-page"
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await manager.handle_provider_command(stale_open)

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_replace_page_from_non_owner_is_noop(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
            _metadata(
                "test.virtual.child",
                provider_instance_id="python-child",
            ),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id="test.virtual.child",
                provider_instance_id="python-child",
            ),
            context_id=owner_ctx.id,
        )
        session = manager.active_page_session()
        assert session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await manager.replace_page(
            descriptor=_dynamic_page(session.page_id, "0,0"),
            context_id=child_ctx.id,
        )

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_page_from_non_owner_is_noop(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
            _metadata(
                "test.virtual.child",
                provider_instance_id="python-child",
            ),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id="test.virtual.child",
                provider_instance_id="python-child",
            ),
            context_id=owner_ctx.id,
        )
        session = manager.active_page_session()
        assert session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await manager.close_page(context_id=child_ctx.id)

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_child_binding_commands_cannot_close_or_replace_page_session(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        session = manager.active_page_session()
        assert session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        child_replace = await _action_command_for_active_binding(
            manager,
            REPLACE_PAGE,
            {
                "descriptor": _dynamic_page(session.page_id, "0,0").model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                )
            },
            control_id="1,0",
        )
        await manager.handle_provider_command(child_replace)

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None

        child_close = await _action_command_for_active_binding(
            manager,
            CLOSE_PAGE,
            control_id="1,0",
        )
        await manager.handle_provider_command(child_close)

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None

        await manager.handle_provider_command(
            _page_session_command(
                CLOSE_PAGE,
                {},
                session_id=session.page_session_id,
                context_id=session.context_id,
                action_instance_id=session.action_instance_id,
            )
        )

        assert manager.active_page_session() is None
        assert manager.context_for_control("0,0") is not None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_builtin_context_replaces_and_closes_opened_dynamic_page():
    owner_action = CapturingBuiltinAction("test.builtin.owner")
    child_action = CapturingBuiltinAction("test.builtin.child")
    builtin_actions = {
        owner_action.uuid: owner_action,
        child_action.uuid: child_action,
    }
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()

    async def get_action(uuid, provider_instance_id=None, **kwargs):
        del kwargs
        if provider_instance_id not in {None, BUILTIN_ACTION_PROVIDER_ID}:
            return None
        if uuid in builtin_actions:
            return _builtin_metadata(uuid)
        return None

    registry.get_action = get_action
    registry.get_builtin_action.side_effect = builtin_actions.get
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=owner_action.uuid,
                                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _builtin_metadata(owner_action.uuid),
            _builtin_metadata(child_action.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_context = owner_action.contexts[-1]

        await owner_context.open_page(
            _dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id=child_action.uuid,
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
            )
        )
        session = manager.active_page_session()
        assert session is not None
        assert manager.context_for_control("1,0") is not None

        await owner_context.replace_page(
            _dynamic_page_with_action_child(
                session.page_id,
                "0,0",
                action_id=child_action.uuid,
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
            )
        )

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is None
        assert manager.context_for_control("0,0") is not None

        await owner_context.close_page()

        assert manager.active_page_session() is None
        restored = manager.context_for_control("0,0")
        assert restored is not None
        assert restored.action_uuid == owner_action.uuid
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_builtin_child_context_cannot_control_parent_page_session():
    owner_action = CapturingBuiltinAction("test.builtin.owner")
    child_action = CapturingBuiltinAction("test.builtin.child")
    builtin_actions = {
        owner_action.uuid: owner_action,
        child_action.uuid: child_action,
    }
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()

    async def get_action(uuid, provider_instance_id=None, **kwargs):
        del kwargs
        if provider_instance_id not in {None, BUILTIN_ACTION_PROVIDER_ID}:
            return None
        if uuid in builtin_actions:
            return _builtin_metadata(uuid)
        return None

    registry.get_action = get_action
    registry.get_builtin_action.side_effect = builtin_actions.get
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=owner_action.uuid,
                                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _builtin_metadata(owner_action.uuid),
            _builtin_metadata(child_action.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_context = owner_action.contexts[-1]
        await owner_context.open_page(
            _dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id=child_action.uuid,
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
            )
        )
        session = manager.active_page_session()
        assert session is not None
        child_context = child_action.contexts[-1]
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await child_context.replace_page(
            _dynamic_page_with_action_child(
                session.page_id,
                "0,0",
                action_id=child_action.uuid,
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
            )
        )

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None

        await child_context.close_page()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_dynamic_page_replace_from_owner_during_nonterminal_unavailable(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    owner_ctx = manager.context_for_control("0,0")
    assert owner_ctx is not None
    await manager.open_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
        context_id=owner_ctx.id,
    )
    session = manager.active_page_session()
    assert session is not None
    child_ctx = manager.context_for_control("1,0")
    assert child_ctx is not None

    await manager.on_action_availability_changed(
        {ProviderActionKey(PROVIDER_INSTANCE_ID, SetRasterImageOnAppearAction.uuid)}
    )

    replacement = _dynamic_page(session.page_id, "0,0")
    await manager.handle_provider_command(
        _page_session_command(
            REPLACE_PAGE,
            {
                "descriptor": replacement.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                )
            },
            session_id=session.page_session_id,
            context_id=session.context_id,
            action_instance_id=session.action_instance_id,
        )
    )

    assert manager.active_page_session() is session
    assert manager.context_for_control("1,0") is None
    assert manager.context_for_control("0,0") is not None


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_binding_detaches_only_that_binding(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()

    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = manager.context_for_control("0,0")
    assert ctx is not None

    msg = await _action_command_for_active_binding(
        manager,
        ACTION_LIFECYCLE_REJECTED,
        {
            "targetKind": "binding",
            "binding": ctx.metadata.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            "reason": "invalid_settings",
        },
    )
    await manager.handle_provider_command(msg)

    assert manager.context_for_control("0,0") is None
    assert manager.snapshot().binding_leases == {}
    assert ctx.action_instance_id in manager.snapshot().action_instances


@pytest.mark.asyncio
async def test_terminal_action_lifecycle_rejected_action_instance_destroys_affected_bindings(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = manager.context_for_control("0,0")
    assert ctx is not None
    metadata = manager.snapshot().action_instances[ctx.action_instance_id]

    await manager.handle_provider_command(
        _action_instance_command(
            ACTION_LIFECYCLE_REJECTED,
            {
                "targetKind": "action_instance",
                "actionInstance": _action_instance_body(metadata),
                "reason": "invalid_settings",
            },
            context_id=metadata.context_id,
            action_instance_id=metadata.action_instance_id,
        )
    )

    assert manager.context_for_control("0,0") is None
    assert manager.snapshot().binding_leases == {}
    assert ctx.action_instance_id not in manager.snapshot().action_instances
    assert ctx.action_instance_id not in manager.snapshot().provider_session_keys


@pytest.mark.asyncio
async def test_action_instance_rejection_from_owner_during_nonterminal_unavailable_replans(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = manager.context_for_control("0,0")
    assert ctx is not None
    metadata = manager.snapshot().action_instances[ctx.action_instance_id]

    await manager.on_action_availability_changed(
        {ProviderActionKey(PROVIDER_INSTANCE_ID, SetRasterImageOnAppearAction.uuid)}
    )

    payload = {
        "targetKind": "action_instance",
        "actionInstance": _action_instance_body(metadata),
        "reason": "action_not_available",
    }
    await manager.handle_provider_command(
        _action_instance_command(
            ACTION_LIFECYCLE_REJECTED,
            payload,
            context_id=metadata.context_id,
            action_instance_id=metadata.action_instance_id,
            sender_session_id="stale",
        )
    )

    assert manager.context_for_control("0,0") is ctx
    assert ctx.action_instance_id in manager.snapshot().action_instances

    await manager.handle_provider_command(
        _action_instance_command(
            ACTION_LIFECYCLE_REJECTED,
            payload,
            context_id=metadata.context_id,
            action_instance_id=metadata.action_instance_id,
        )
    )

    assert manager.context_for_control("0,0") is None
    assert manager.snapshot().binding_leases == {}
    assert ctx.action_instance_id in manager.snapshot().action_instances


@pytest.mark.asyncio
async def test_delayed_unavailable_status_does_not_write_after_binding_restored(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = manager.context_for_control("0,0")
        assert ctx is not None

        msg = await _action_command_for_active_binding(
            manager,
            ACTION_LIFECYCLE_REJECTED,
            {
                "targetKind": "binding",
                "binding": ctx.metadata.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "reason": "invalid_settings",
                "retryable": True,
            },
        )
        await manager.handle_provider_command(msg)

        assert manager.context_for_control("0,0") is None
        with anyio.fail_after(5.0):
            while not render_backend.calls:
                await anyio.sleep(0.01)
        unavailable_render = render_backend.calls[-1]
        assert render_backend.requests[-1].binding_id is None

        changed = _ingest_provider_entries(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            provider_session_id=PROVIDER_SESSION_ID,
            entries=[_availability_entry(SetRasterImageOnAppearAction.uuid)],
        )
        await manager.on_action_availability_changed(changed)

        restored_ctx = manager.context_for_control("0,0")
        assert restored_ctx is not None
        assert restored_ctx.binding_id != ctx.binding_id

        baseline_calls = command_service.set_raster_frame.call_count
        render_backend.release(unavailable_render)
        with anyio.move_on_after(0.2):
            while command_service.set_raster_frame.call_count == baseline_calls:
                await anyio.sleep(0.01)

        assert command_service.set_raster_frame.call_count == baseline_calls
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_page_session_resource_unavailable_replans(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        session = manager.active_page_session()
        assert session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None
        runtime_sender = _runtime_sender_for(manager)
        assert isinstance(runtime_sender, _RecordingRuntimeSender)
        opened = [
            msg
            for msg in runtime_sender.messages
            if msg.message_type == PAGE_SESSION_OPENED
        ]
        assert opened

        await manager.handle_provider_command(
            _page_session_command(
                ACTION_LIFECYCLE_REJECTED,
                {
                    "targetKind": "page_session",
                    "pageSession": opened[-1].body.page_session.model_dump(
                        by_alias=True,
                        exclude_none=True,
                        mode="json",
                    ),
                    "reason": "resource_unavailable",
                },
                session_id=session.page_session_id,
                context_id=session.context_id,
                action_instance_id=session.action_instance_id,
            )
        )

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is None
        assert all(
            lease.page_session_id != session.page_session_id
            for lease in manager.snapshot().binding_leases.values()
        )
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_stale_lifecycle_rejection_from_current_session_recovers_binding(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            ctx = manager.context_for_control("0,0")
            lease = _binding_lease_for_control(manager, "0,0")
            assert ctx is not None
            assert lease is not None
            await _drain_action_messages(stream)

            msg = await _action_command_for_active_binding(
                manager,
                ACTION_LIFECYCLE_REJECTED,
                {
                    "targetKind": "binding",
                    "binding": ctx.metadata.model_dump(
                        by_alias=True,
                        exclude_none=True,
                        mode="json",
                    ),
                    "reason": "stale_lifecycle",
                },
            )
            await manager.handle_provider_command(msg)

            messages = await _collect_action_messages(stream)
            created = [
                event
                for event in messages
                if event.message_type == ACTION_INSTANCE_CREATED
            ]
            attached = [
                event for event in messages if event.message_type == BINDING_ATTACHED
            ]
            assert len(created) == 1
            assert len(attached) == 1
            assert created[0].recipient_session_id == PROVIDER_SESSION_ID
            assert attached[0].recipient_session_id == PROVIDER_SESSION_ID
            recovered = _binding_lease_for_control(manager, "0,0")
            assert recovered is not None
            assert recovered.stale_lifecycle_recoveries == 1

            await manager.handle_provider_command(msg)
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_from_stale_provider_session_is_ignored(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = manager.context_for_control("0,0")
    assert ctx is not None

    msg = await _action_command_for_active_binding(
        manager,
        ACTION_LIFECYCLE_REJECTED,
        {
            "targetKind": "binding",
            "binding": ctx.metadata.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            "reason": "stale_lifecycle",
        },
    )
    await manager.handle_provider_command(msg.model_copy(update={"sender_session_id": "stale"}))

    assert manager.context_for_control("0,0") is ctx
    assert manager.snapshot().binding_leases


@pytest.mark.asyncio
async def test_set_raster_image_last_write_wins_same_control(
    device_config_set_raster_image, persistence_tmp_dir
):
    """bindingOutput rejects stale or mismatched output generations."""
    from deckr.actions.messages import BINDING_OUTPUT

    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = manager.context_for_control("0,0")
        assert ctx is not None
        binding = ctx.metadata.model_copy(update={"output_generation": 1})
        msg = await _action_command_for_active_binding(
            manager,
            BINDING_OUTPUT,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "capability": {
                    "deviceRef": {
                        "managerId": "manager-main",
                        "deviceId": "test-device",
                    },
                    "controlId": "0,0",
                    "capabilityId": "raster.bitmap",
                },
                "commandType": "set_frame",
                "params": {"image": "ZnJhbWU=", "encoding": "jpeg"},
                "generation": 2,
            },
        )

        await manager.handle_provider_command(msg)

        command_service.set_raster_frame.assert_not_awaited()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_settings_isolated_by_control_same_action(persistence_tmp_dir):
    """Same action on different controls keeps separate settings."""
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=_metadata(NoopAction.uuid))
    action_bus = _actions_bus()

    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=NoopAction.uuid,
                                settings={"control_marker": "A"},
                            ),
                            Control(
                                selector={"control_id": "1,0"},
                                action=NoopAction.uuid,
                                settings={"control_marker": "B"},
                            ),
                        ]
                    )
                ],
            )
        ],
    )
    config_service = MemoryConfigService(config)
    settings_service = ConfigBackedSettingsService(
        controller_id=CONTROLLER_ID,
        config_service=config_service,
    )

    async with anyio.create_task_group():

        def start_soon(*args, **kwargs):
            pass

        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=start_soon,
            settings_service=settings_service,
        )
        _seed_action_availability(manager, _metadata(NoopAction.uuid))
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)
        control_a = manager.context_for_control("0,0")
        control_b = manager.context_for_control("1,0")
        assert control_a is not None
        assert control_b is not None
        settings_a = control_a.controller_context.settings
        settings_b = control_b.controller_context.settings
        assert settings_a.control_marker == "A"
        assert settings_b.control_marker == "B"


@pytest.mark.asyncio
async def test_config_reload_replaces_read_only_settings_snapshot(persistence_tmp_dir):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=_metadata(NoopAction.uuid))
    action_bus = _actions_bus()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=NoopAction.uuid,
                                settings={
                                    "label": "from-config",
                                    "nested": {"role": {"page": "root"}},
                                },
                            )
                        ]
                    )
                ],
            )
        ],
    )
    reloaded_config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=NoopAction.uuid,
                                settings={"label": "from-reload"},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group():

        def start_soon(*args, **kwargs):
            pass

        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=start_soon,
        )
        _seed_action_availability(manager, _metadata(NoopAction.uuid))
        await manager.set_page(profile="default", page=0)
        ctx = manager.context_for_control("0,0")
        assert ctx is not None
        settings = ctx.controller_context.settings
        assert settings.label == "from-config"
        assert settings.nested == {"role": {"page": "root"}}

        await manager.on_config_changed(reloaded_config)
        reloaded_ctx = manager.context_for_control("0,0")
        assert reloaded_ctx is not None
        reloaded_settings = reloaded_ctx.controller_context.settings
        assert vars(reloaded_settings) == {"label": "from-reload"}


class ConfigurableActionRegistry:
    """Registry that can add/remove actions for testing availability replans.

    Uses qualified IDs (provider_instance_id::action_uuid) internally to match ActionRegistry.
    """

    def __init__(self):
        self._actions: dict[str, ActionMetadata] = {}

    def _qualified_id(self, provider_instance_id: str, action_uuid: str) -> str:
        return f"{provider_instance_id}::{action_uuid}"

    async def get_action(
        self,
        address: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: dict[str, str] | None = None,
    ) -> ActionMetadata | None:
        del provider_labels
        if "::" in address:
            return self._actions.get(address)
        for key, meta in self._actions.items():
            if (
                provider_instance_id is not None
                and meta.provider_instance_id != provider_instance_id
            ):
                continue
            if key.endswith(f"::{address}"):
                return meta
        return None

    def add_action(self, action_uuid: str, meta: ActionMetadata) -> None:
        qualified = self._qualified_id(meta.provider_instance_id, action_uuid)
        self._actions[qualified] = meta

    def remove_action(self, action_uuid: str, provider_instance_id: str) -> None:
        qualified = self._qualified_id(provider_instance_id, action_uuid)
        self._actions.pop(qualified, None)

    def get_builtin_action(self, uuid: str):
        return None


ACTION_X_UUID = "test.action.x"


@pytest.mark.asyncio
async def test_service_view_availability_resolves_unavailable_control(
    persistence_tmp_dir,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableActionRegistry()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=ACTION_X_UUID,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group():

        def start_soon(*args, **kwargs):
            pass

        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=start_soon,
        )
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)

        ctx_before = manager.context_for_control("0,0")
        assert ctx_before is None

        changed = _ingest_provider_entries(
                manager,
                provider_instance_id="test-provider",
            provider_id="test",
            provider_session_id=PROVIDER_SESSION_ID,
            entries=[_availability_entry(ACTION_X_UUID)],
        )
        _record_provider_runtime_contract(
            manager,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
            ),
        )
        await manager.on_action_availability_changed(changed)

        ctx_after = manager.context_for_control("0,0")
        assert ctx_after is not None
        assert ctx_after.action_uuid == ACTION_X_UUID


@pytest.mark.asyncio
async def test_unrelated_availability_change_does_not_rebuild_current_page(
    persistence_tmp_dir,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableActionRegistry()
    registry.add_action(
        ACTION_X_UUID,
        _metadata(
            ACTION_X_UUID,
            provider_instance_id="test-provider",
            provider_id="test",
        ),
    )
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=ACTION_X_UUID,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        command_service = FakeHardwareCommandService()
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
            ),
        )
        await manager.set_page(profile="default", page=0)
        ctx_before = manager.context_for_control("0,0")
        assert ctx_before is not None

        command_service.clear_raster.reset_mock()

        await manager.on_action_availability_changed(
            {ProviderActionKey("other-provider", "test.action.unrelated")}
        )

        command_service.clear_raster.assert_not_awaited()
        assert manager.context_for_control("0,0") is ctx_before
        tg.cancel_scope.cancel()
