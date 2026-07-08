from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.endpoints import action_provider_address
from deckr.actions.messages import (
    SETTINGS_PATCH,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import (
    ACTIONS_LANE,
    HARDWARE_MESSAGES_LANE,
    DeckrMessage,
    controller_address,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
)
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import CapabilityRef, DeviceDescriptor, DeviceRef

from deckr.controller._controller_service import ControllerService

CONTROLLER_ID = "controller-main"
CONFIG_ID = "config-a"


class _Stream:
    def __init__(self, messages) -> None:
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _Endpoint:
    def __init__(self, messages=()) -> None:
        self.session_id = "controller-session"
        self.send = AsyncMock()
        self._messages = list(messages)
        self.subscribed_lanes: list[str] = []

    @asynccontextmanager
    async def subscribe(self, lane: str):
        self.subscribed_lanes.append(lane)
        yield _Stream(self._messages)


def _service(
    *,
    endpoint: _Endpoint | None = None,
    availability_service=None,
) -> ControllerService:
    return ControllerService(
        endpoint=endpoint or _Endpoint(),
        beacon=MagicMock(),
        concord=MagicMock(),
        config_service=MagicMock(),
        settings_service=MagicMock(),
        controller_id=CONTROLLER_ID,
        action_availability_service=availability_service,
    )


def _action_message(
    message_type: str,
    *,
    subject=None,
    body=None,
) -> DeckrMessage:
    return DeckrMessage(
        lane=ACTIONS_LANE,
        messageType=message_type,
        sender=action_provider_address("provider-a"),
        senderSessionId="provider-session",
        recipient=endpoint_target(controller_address(CONTROLLER_ID)),
        subject=subject or entity_subject("other"),
        body=body or {},
    )


def _hardware_message(message_type: str, body) -> DeckrMessage:
    ref = body.device_ref
    capability_ref = CapabilityRef(
        deviceRef=ref,
        controlId=getattr(body, "control_id", None),
        capabilityId=body.capability_id,
    )
    return DeckrMessage(
        lane=HARDWARE_MESSAGES_LANE,
        messageType=message_type,
        sender=hardware_manager_address(ref.manager_id),
        senderSessionId="manager-session",
        recipient=endpoint_target(controller_address(CONTROLLER_ID)),
        subject=hw_messages.hardware_subject_for_capability(capability_ref),
        body=hw_messages.hardware_body_to_dict(body),
    )


def _device_ref() -> DeviceRef:
    return DeviceRef(managerId="manager-a", deviceId="device-a")


def _device() -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId="device-a",
        fingerprint="fingerprint:device-a",
        displayName="Device A",
    )


async def _register_route(service: ControllerService) -> None:
    service._device_registry.connect(
        config_id=CONFIG_ID,
        ref=_device_ref(),
        device=_device(),
        contract=ContractPointer(contractId="contract-a", generation=1),
        manager_session_id="manager-session",
    )


def _manager_context() -> MagicMock:
    ctx = MagicMock()
    ctx.handle_command = AsyncMock()
    ctx.on_action_availability_changed = AsyncMock()
    ctx.on_event = AsyncMock()
    ctx.on_capability_state_changed = AsyncMock()
    ctx.on_command_rejected = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_action_command_ignores_invalid_settings_body_and_missing_subject() -> None:
    service = _service()
    ctx = _manager_context()
    await service._controller_contexts.set(CONFIG_ID, ctx)

    await service._handle_action_command(
        _action_message(
            SETTINGS_PATCH,
            body={"target": {"not": "valid"}},
        )
    )
    await service._handle_action_command(_action_message("openPage"))

    ctx.handle_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_hardware_input_loop_ignores_message_with_no_device_ref(
    monkeypatch,
) -> None:
    body = hw_messages.ControlInputMessage(
        deviceRef=_device_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    endpoint = _Endpoint([_hardware_message(hw_messages.CONTROL_INPUT, body)])
    service = _service(endpoint=endpoint)
    ctx = _manager_context()
    await _register_route(service)
    await service._controller_contexts.set(CONFIG_ID, ctx)
    monkeypatch.setattr(
        hw_messages,
        "hardware_device_ref_from_message",
        lambda message: None,
    )

    await service._hardware_input_loop(stopping=anyio.Event())

    ctx.on_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_hardware_input_loop_ignores_message_without_route() -> None:
    body = hw_messages.ControlInputMessage(
        deviceRef=_device_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    endpoint = _Endpoint([_hardware_message(hw_messages.CONTROL_INPUT, body)])
    service = _service(endpoint=endpoint)

    await service._hardware_input_loop(stopping=anyio.Event())

    assert endpoint.subscribed_lanes == [HARDWARE_MESSAGES_LANE]


@pytest.mark.asyncio
async def test_hardware_input_loop_ignores_message_without_controller_context() -> None:
    body = hw_messages.ControlInputMessage(
        deviceRef=_device_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    endpoint = _Endpoint([_hardware_message(hw_messages.CONTROL_INPUT, body)])
    service = _service(endpoint=endpoint)
    await _register_route(service)

    await service._hardware_input_loop(stopping=anyio.Event())

    assert endpoint.subscribed_lanes == [HARDWARE_MESSAGES_LANE]


@pytest.mark.asyncio
async def test_hardware_input_loop_routes_input_state_and_rejection_messages() -> None:
    input_body = hw_messages.ControlInputMessage(
        deviceRef=_device_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    state_body = hw_messages.CapabilityStateChangedMessage(
        deviceRef=_device_ref(),
        controlId="key-1",
        capabilityId="input",
        value=True,
        stateType="pressed",
    )
    rejected_body = hw_messages.CommandRejectedMessage(
        deviceRef=_device_ref(),
        controlId="key-1",
        capabilityId="raster",
        commandType="set_frame",
        reason="stale",
    )
    messages = [
        _hardware_message(hw_messages.CONTROL_INPUT, input_body),
        _hardware_message(hw_messages.CAPABILITY_STATE_CHANGED, state_body),
        _hardware_message(hw_messages.COMMAND_REJECTED, rejected_body),
    ]
    endpoint = _Endpoint(messages)
    service = _service(endpoint=endpoint)
    ctx = _manager_context()
    await _register_route(service)
    await service._controller_contexts.set(CONFIG_ID, ctx)

    await service._hardware_input_loop(stopping=anyio.Event())

    ctx.on_event.assert_awaited_once_with(messages[0])
    state_event = ctx.on_capability_state_changed.await_args.args[0]
    rejected_event = ctx.on_command_rejected.await_args.args[0]
    assert state_event.capability_id == "input"
    assert state_event.value is True
    assert rejected_event.capability_id == "raster"
    assert rejected_event.command_type == "set_frame"
