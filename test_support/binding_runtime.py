from collections.abc import Callable, Iterable
from types import SimpleNamespace

import anyio
from deckr.actions.endpoints import (
    action_provider_address,
)
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ActionAvailabilityEntry,
    ActionDescriptor,
    context_subject,
)
from deckr.contracts.authority import ContractPointer
from deckr.hardware.descriptors import (
    DeviceDescriptor,
    DeviceRef,
)
from deckr.lanes import EndpointSession

from deckr.controller._actions import (
    ActionAvailabilityRecord,
    ActionIntentKey,
    ActionMetadata,
    BuiltinActionCatalog,
    ProviderActionKey,
    ProviderSessionKey,
)
from deckr.controller._device_session import DeviceSession
from deckr.controller._hardware import HardwareCommandService
from deckr.controller._render_workers import RenderBackend
from deckr.controller.config._data import DeviceConfig
from deckr.controller.settings import SettingsService
from test_support.action_catalog import _BindingActionServiceHarness, _requirements
from test_support.binding_constants import CONTROLLER_ADDR
from test_support.lanes import LaneHarness


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
        contract,
        causation_id=None,
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
        if contract is None or contract != self._action_service.current_contract(
            provider_session_key
        ):
            return False
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
    manager: DeviceSession,
) -> _BindingActionServiceHarness:
    return _ACTION_SERVICE_BY_BINDING_SERVICE_ID[id(manager)]


def _runtime_sender_for(
    manager: DeviceSession,
) -> _RecordingRuntimeSender:
    return _RUNTIME_SENDER_BY_BINDING_SERVICE_ID[id(manager)]


def _record_provider_runtime_contract(
    manager: DeviceSession,
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
    manager: BuiltinActionCatalog,
    actions_bus: EndpointSession,
    start_soon: Callable,
    render_backend: RenderBackend | None = None,
    settings_service: SettingsService | None = None,
    clock: Callable[[], float] | None = None,
    action_service: _BindingActionServiceHarness | None = None,
    page_timeout_check_interval: float = 0.25,
) -> DeviceSession:
    service_action = action_service or _BindingActionServiceHarness(
        manager=manager,
        clock=clock,
    )
    runtime_sender = _RecordingRuntimeSender(
        actions_bus=actions_bus,
        action_service=service_action,
    )
    service = DeviceSession(
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


def _seed_action_availability(
    manager: DeviceSession,
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
                    actionId=metadata.action_id,
                    status="available",
                    descriptor=ActionDescriptor(
                        actionId=metadata.action_id,
                        providerId=metadata.provider_id,
                        requirements=_requirements(),
                    ),
                ),
            ),
        )


def _ingest_provider_entries(
    manager: DeviceSession,
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
    manager: DeviceSession,
    *,
    provider_instance_id: str,
    provider_id: str,
    action_id: str,
    provider_session_id: str | None = None,
    reason: str | None = None,
    intent: ActionIntentKey | None = None,
) -> ProviderActionKey:
    return _action_service_for(manager).record_lifecycle_unavailable(
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        action_id=action_id,
        provider_session_id=provider_session_id,
        reason=reason,
        intent=intent,
    )


def _record_for_key(
    manager: DeviceSession,
    key: ProviderActionKey,
) -> ActionAvailabilityRecord | None:
    return _action_service_for(manager).record_for_key(key)
