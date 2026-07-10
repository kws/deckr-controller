from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import (
    HARDWARE_MESSAGES_LANE,
    DeckrMessage,
    controller_address,
    endpoint_target,
    hardware_manager_address,
)
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    DECKR_DEVICE_POWER,
    CapabilityDescriptor,
    CapabilityRef,
    DeviceDescriptor,
    DeviceRef,
)

from deckr.controller._hardware import (
    ControllerHardwareService,
    HardwareCommandService,
)
from deckr.controller._hardware._routes import DeviceRouteRegistry


def _endpoint():
    return type("Endpoint", (), {"send": AsyncMock()})()


class _Stream:
    def __init__(self, messages) -> None:
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _InputEndpoint:
    def __init__(self, messages=()) -> None:
        self.session_id = "controller-session"
        self.send = AsyncMock()
        self._messages = list(messages)
        self.subscribed_lanes: list[str] = []

    @asynccontextmanager
    async def subscribe(self, lane: str):
        self.subscribed_lanes.append(lane)
        yield _Stream(self._messages)


class _Callbacks:
    def __init__(self) -> None:
        self.control_input = AsyncMock()
        self.capability_state_changed = AsyncMock()
        self.command_rejected = AsyncMock()
        self.connected = AsyncMock()
        self.disconnected = AsyncMock()
        self.descriptor_changed = AsyncMock()

    async def on_hardware_connected(self, live, *, initial_config) -> None:
        await self.connected(live, initial_config=initial_config)

    async def on_hardware_disconnected(self, config_id: str, *, reason: str) -> None:
        await self.disconnected(config_id, reason=reason)

    async def on_hardware_descriptor_changed(self, config_id: str, device) -> None:
        await self.descriptor_changed(config_id, device)

    async def on_hardware_control_input(self, live, message: DeckrMessage) -> None:
        await self.control_input(live, message)

    async def on_hardware_capability_state_changed(self, live, event) -> None:
        await self.capability_state_changed(live, event)

    async def on_hardware_command_rejected(self, live, event) -> None:
        await self.command_rejected(live, event)


def _ref() -> DeviceRef:
    return DeviceRef(managerId="manager-a", deviceId="device-a")


def _contract() -> ContractPointer:
    return ContractPointer(contractId="contract-a", generation=1)


_DEFAULT_CONTRACT = _contract()


def _device(*, power: bool = False) -> DeviceDescriptor:
    capabilities = ()
    if power:
        capabilities = (
            CapabilityDescriptor(
                capabilityId="device-power",
                family=DECKR_DEVICE_POWER,
                type="screen",
                direction="command",
                access=("invokable",),
                commandTypes=("sleep", "wake"),
            ),
        )
    return DeviceDescriptor(
        deviceId="device-a",
        fingerprint="fingerprint:device-a",
        displayName="Device A",
        capabilities=capabilities,
    )


def _registry(*, power: bool = False) -> DeviceRouteRegistry:
    registry = DeviceRouteRegistry()
    registry.connect(
        config_id="config-a",
        ref=_ref(),
        device=_device(power=power),
        contract=_contract(),
        manager_session_id="manager-session",
    )
    return registry


def _service(
    endpoint=None,
    *,
    power: bool = False,
    registry: DeviceRouteRegistry | None = None,
) -> HardwareCommandService:
    endpoint = endpoint or _endpoint()
    registry = registry or _registry(power=power)
    return HardwareCommandService(
        endpoint,
        route_lookup=registry.get,
    )


def _sent_body(endpoint) -> dict:
    return endpoint.send.await_args.kwargs["body"]


def _hardware_message(
    message_type: str,
    body,
    *,
    sender: str | None = None,
    sender_session_id: str = "manager-session",
    contract: ContractPointer | None = _DEFAULT_CONTRACT,
) -> DeckrMessage:
    ref = body.device_ref
    capability_ref = CapabilityRef(
        deviceRef=ref,
        controlId=getattr(body, "control_id", None),
        capabilityId=body.capability_id,
    )
    return DeckrMessage(
        lane=HARDWARE_MESSAGES_LANE,
        messageType=message_type,
        sender=sender or hardware_manager_address(ref.manager_id),
        senderSessionId=sender_session_id,
        recipient=endpoint_target(controller_address("controller-main")),
        subject=hw_messages.hardware_subject_for_capability(capability_ref),
        body=hw_messages.hardware_body_to_dict(body),
        contract=contract,
    )


def _controller_hardware(
    endpoint: _InputEndpoint,
    callbacks: _Callbacks,
) -> ControllerHardwareService:
    return ControllerHardwareService(
        endpoint=endpoint,
        beacon=MagicMock(),
        concord=MagicMock(),
        config_service=MagicMock(),
        callbacks=callbacks,
        controller_id="controller-main",
        controller_session_id="controller-session",
    )


def _connect_route(service: ControllerHardwareService) -> None:
    service._routes.connect(
        config_id="config-a",
        ref=_ref(),
        device=_device(),
        contract=_contract(),
        manager_session_id="manager-session",
    )


@pytest.mark.asyncio
async def test_set_raster_frame_sends_base64_hardware_command() -> None:
    endpoint = _endpoint()
    service = _service(endpoint)
    image = b"jpeg-bytes"

    await service.set_raster_frame("config-a", "key-1", "raster", image)

    endpoint.send.assert_awaited_once()
    sent = endpoint.send.await_args.kwargs
    assert sent["lane"] == HARDWARE_MESSAGES_LANE
    assert sent["recipient"] == hardware_manager_address("manager-a")
    assert sent["recipient_session_id"] == "manager-session"
    assert sent["message_type"] == hw_messages.CONTROL_COMMAND
    assert sent["contract"] == _contract()
    assert sent["subject"] == hw_messages.hardware_subject_for_capability(
        CapabilityRef(deviceRef=_ref(), controlId="key-1", capabilityId="raster")
    )
    assert _sent_body(endpoint)["commandType"] == "set_frame"
    assert _sent_body(endpoint)["params"]["image"] == base64.b64encode(image).decode(
        "ascii"
    )
    assert _sent_body(endpoint)["params"]["encoding"] == "jpeg"


@pytest.mark.asyncio
async def test_clear_raster_sends_clear_command() -> None:
    endpoint = _endpoint()
    service = _service(endpoint)

    await service.clear_raster("config-a", "key-1", "raster")

    endpoint.send.assert_awaited_once()
    assert _sent_body(endpoint)["commandType"] == "clear"
    assert _sent_body(endpoint)["params"] == {}


@pytest.mark.asyncio
async def test_sleep_and_wake_send_power_commands_when_advertised() -> None:
    endpoint = _endpoint()
    service = _service(endpoint, power=True)

    await service.sleep_device("config-a")
    await service.wake_device("config-a")

    assert endpoint.send.await_count == 2
    assert [
        call.kwargs["body"]["commandType"]
        for call in endpoint.send.await_args_list
    ] == ["sleep", "wake"]


@pytest.mark.asyncio
async def test_sleep_and_wake_drop_without_power_capability() -> None:
    endpoint = _endpoint()
    service = _service(endpoint, power=False)

    await service.sleep_device("config-a")
    await service.wake_device("config-a")

    endpoint.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_commands_for_unregistered_config_drop_without_send() -> None:
    endpoint = _endpoint()
    service = _service(endpoint, power=True)

    await service.set_raster_frame("missing", "key-1", "raster", b"jpeg")
    await service.clear_raster("missing", "key-1", "raster")
    await service.sleep_device("missing")
    await service.wake_device("missing")

    endpoint.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_control_command_requires_device_ref() -> None:
    endpoint = _endpoint()
    registry = _registry()
    service = _service(endpoint, registry=registry)
    live = registry.get("config-a")
    assert live is not None

    with pytest.raises(ValueError, match="deviceRef"):
        await service._send_control_command(
            live=live,
            ref=CapabilityRef(capabilityId="raster"),
            command_type="set_frame",
            params={},
        )


@pytest.mark.asyncio
async def test_hardware_input_loop_ignores_message_with_no_device_ref(
    monkeypatch,
) -> None:
    body = hw_messages.ControlInputMessage(
        deviceRef=_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    endpoint = _InputEndpoint([_hardware_message(hw_messages.CONTROL_INPUT, body)])
    callbacks = _Callbacks()
    service = _controller_hardware(endpoint, callbacks)
    _connect_route(service)
    monkeypatch.setattr(
        hw_messages,
        "hardware_device_ref_from_message",
        lambda message: None,
    )

    await service._input_loop(stopping=anyio.Event())

    callbacks.control_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_hardware_input_loop_ignores_message_without_route() -> None:
    body = hw_messages.ControlInputMessage(
        deviceRef=_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    endpoint = _InputEndpoint([_hardware_message(hw_messages.CONTROL_INPUT, body)])
    callbacks = _Callbacks()
    service = _controller_hardware(endpoint, callbacks)

    await service._input_loop(stopping=anyio.Event())

    assert endpoint.subscribed_lanes == [HARDWARE_MESSAGES_LANE]
    callbacks.control_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_hardware_input_loop_routes_input_state_and_rejection_messages() -> None:
    input_body = hw_messages.ControlInputMessage(
        deviceRef=_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    state_body = hw_messages.CapabilityStateChangedMessage(
        deviceRef=_ref(),
        controlId="key-1",
        capabilityId="input",
        value=True,
        stateType="pressed",
    )
    rejected_body = hw_messages.CommandRejectedMessage(
        deviceRef=_ref(),
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
    endpoint = _InputEndpoint(messages)
    callbacks = _Callbacks()
    service = _controller_hardware(endpoint, callbacks)
    _connect_route(service)

    await service._input_loop(stopping=anyio.Event())

    callbacks.control_input.assert_awaited_once()
    state_event = callbacks.capability_state_changed.await_args.args[1]
    rejected_event = callbacks.command_rejected.await_args.args[1]
    assert state_event.capability_id == "input"
    assert state_event.value is True
    assert rejected_event.capability_id == "raster"
    assert rejected_event.command_type == "set_frame"


@pytest.mark.parametrize(
    "message_updates",
    (
        {"contract": None},
        {"contract": ContractPointer(contractId="wrong-contract", generation=1)},
        {"contract": ContractPointer(contractId="contract-a", generation=2)},
        {"sender": hardware_manager_address("manager-b")},
    ),
)
@pytest.mark.asyncio
async def test_hardware_input_rejects_wrong_endpoint_or_contract_without_reconcile(
    message_updates,
) -> None:
    body = hw_messages.ControlInputMessage(
        deviceRef=_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    endpoint = _InputEndpoint(
        [_hardware_message(hw_messages.CONTROL_INPUT, body, **message_updates)]
    )
    callbacks = _Callbacks()
    service = _controller_hardware(endpoint, callbacks)
    _connect_route(service)
    service.reconcile = AsyncMock()

    await service._input_loop(stopping=anyio.Event())

    callbacks.control_input.assert_not_awaited()
    service.reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_hardware_input_reconciles_stale_session_once_then_drops() -> None:
    body = hw_messages.ControlInputMessage(
        deviceRef=_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    endpoint = _InputEndpoint(
        [
            _hardware_message(
                hw_messages.CONTROL_INPUT,
                body,
                sender_session_id="successor-session",
            )
        ]
    )
    callbacks = _Callbacks()
    service = _controller_hardware(endpoint, callbacks)
    _connect_route(service)
    service.reconcile = AsyncMock()

    await service._input_loop(stopping=anyio.Event())

    service.reconcile.assert_awaited_once_with(
        reason="hardware ingress sender session mismatch"
    )
    callbacks.control_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_hardware_input_rechecks_refreshed_route_after_session_reconcile() -> None:
    body = hw_messages.ControlInputMessage(
        deviceRef=_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    endpoint = _InputEndpoint(
        [
            _hardware_message(
                hw_messages.CONTROL_INPUT,
                body,
                sender_session_id="successor-session",
            )
        ]
    )
    callbacks = _Callbacks()
    service = _controller_hardware(endpoint, callbacks)
    _connect_route(service)

    async def refresh_route(*, reason: str) -> None:
        assert reason == "hardware ingress sender session mismatch"
        service._routes.update_descriptor(
            ref=_ref(),
            device=_device(),
            contract=_contract(),
            manager_session_id="successor-session",
        )

    service.reconcile = AsyncMock(side_effect=refresh_route)

    await service._input_loop(stopping=anyio.Event())

    service.reconcile.assert_awaited_once()
    callbacks.control_input.assert_awaited_once()
    refreshed = callbacks.control_input.await_args.args[0]
    assert refreshed.manager_session_id == "successor-session"


@pytest.mark.asyncio
async def test_hardware_input_rechecks_complete_route_after_reconcile() -> None:
    body = hw_messages.ControlInputMessage(
        deviceRef=_ref(),
        controlId="key-1",
        capabilityId="input",
        eventType="down",
    )
    endpoint = _InputEndpoint(
        [
            _hardware_message(
                hw_messages.CONTROL_INPUT,
                body,
                sender_session_id="successor-session",
            )
        ]
    )
    callbacks = _Callbacks()
    service = _controller_hardware(endpoint, callbacks)
    _connect_route(service)

    async def replace_contract(*, reason: str) -> None:
        assert reason == "hardware ingress sender session mismatch"
        service._routes.update_descriptor(
            ref=_ref(),
            device=_device(),
            contract=ContractPointer(contractId="successor-contract", generation=1),
            manager_session_id="successor-session",
        )

    service.reconcile = AsyncMock(side_effect=replace_contract)

    await service._input_loop(stopping=anyio.Event())

    service.reconcile.assert_awaited_once()
    callbacks.control_input.assert_not_awaited()
