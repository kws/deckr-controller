"""DeviceManager façade tests."""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import anyio
import pytest
from conftest import LaneHarness
from deckr.contracts.messages import DeckrMessage, controller_address
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
    DeviceRef,
)

from deckr.controller import _device_manager as device_manager_module
from deckr.controller._device_manager import DeviceManager
from deckr.controller.config._data import DeviceConfig, Profile

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)


class _FakeBindingService:
    instances: list["_FakeBindingService"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, object]] = []
        self.config_active = True
        self.snapshot_value = SimpleNamespace(kind="snapshot")
        self.context_value = SimpleNamespace(kind="context")
        self.interest_value = SimpleNamespace(kind="interest")
        _FakeBindingService.instances.append(self)

    def snapshot(self):
        self.calls.append(("snapshot", None))
        return self.snapshot_value

    def context_for_control(self, control_id: str):
        self.calls.append(("context_for_control", control_id))
        return self.context_value

    def action_interest_snapshot(self, *, now=None):
        self.calls.append(("action_interest_snapshot", now))
        return self.interest_value

    async def start(self, tg, stopping) -> None:
        self.calls.append(("start", (tg, stopping)))

    async def on_config_changed(self, config) -> None:
        self.calls.append(("on_config_changed", config))

    async def set_page(self, **kwargs) -> bool:
        self.calls.append(("set_page", kwargs))
        return True

    async def open_page(self, **kwargs):
        self.calls.append(("open_page", kwargs))
        return SimpleNamespace(kind="dynamic-session")

    async def replace_page(self, **kwargs) -> None:
        self.calls.append(("replace_page", kwargs))

    async def close_page(self, **kwargs) -> None:
        self.calls.append(("close_page", kwargs))

    async def clear_page(self, **kwargs) -> None:
        self.calls.append(("clear_page", kwargs))

    async def on_device_descriptor_changed(self, descriptor) -> None:
        self.calls.append(("on_device_descriptor_changed", descriptor))

    async def on_capability_state_changed(self, event) -> None:
        self.calls.append(("on_capability_state_changed", event))

    async def on_command_rejected(self, event) -> None:
        self.calls.append(("on_command_rejected", event))

    async def on_action_availability_changed(self, changed_keys=()) -> None:
        self.calls.append(("on_action_availability_changed", tuple(changed_keys)))

    async def handle_provider_command(self, message) -> None:
        self.calls.append(("handle_provider_command", message))

    async def handle_hardware_input(self, message) -> None:
        self.calls.append(("handle_hardware_input", message))


def _device_config(config_id: str = "test-device") -> DeviceConfig:
    return DeviceConfig(
        id=config_id,
        name="Test Device",
        match={"fingerprint": f"fingerprint:{config_id}"},
        profiles=[Profile(name="default", pages=[])],
    )


def _device(device_id: str = "test-device") -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test Device",
        fingerprint=f"fingerprint:{device_id}",
        controls=(
            ControlDescriptor(
                controlId="0,0",
                kind="key",
                geometry=ControlGeometry(x=0, y=0, width=1, height=1, unit="grid"),
                inputCapabilities=(),
                outputCapabilities=(),
            ),
        ),
    )


def _hardware_ref(device: DeviceDescriptor) -> DeviceRef:
    return DeviceRef(managerId="manager-main", deviceId=device.device_id)


def _actions_session():
    return LaneHarness("actions", default_endpoint=CONTROLLER_ADDR).endpoint(
        CONTROLLER_ADDR
    ).session


def _manager(monkeypatch: pytest.MonkeyPatch, *, config_stream=None) -> DeviceManager:
    _FakeBindingService.instances.clear()
    monkeypatch.setattr(
        device_manager_module,
        "ControlBindingService",
        _FakeBindingService,
    )
    device = _device()
    return DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=MagicMock(),
        config=_device_config(),
        manager=MagicMock(),
        actions_bus=_actions_session(),
        start_soon=lambda *args, **kwargs: None,
        config_stream=config_stream,
    )


@pytest.mark.asyncio
async def test_device_manager_constructs_binding_service_with_page_session(
    monkeypatch: pytest.MonkeyPatch,
):
    manager = _manager(monkeypatch)
    binding = _FakeBindingService.instances[0]

    assert binding.kwargs["pages"] is not None
    assert binding.kwargs["page_command_port"] is manager
    assert binding.kwargs["action_service"].controller_id == CONTROLLER_ID
    assert binding.kwargs["device"] is manager.device
    assert binding.kwargs["config"] is manager.config


@pytest.mark.asyncio
async def test_device_manager_delegates_binding_and_page_operations(
    monkeypatch: pytest.MonkeyPatch,
):
    manager = _manager(monkeypatch)
    binding = _FakeBindingService.instances[0]
    message = MagicMock(spec=DeckrMessage)
    descriptor = _device("updated-device")
    event = MagicMock(spec=hw_messages.CapabilityStateChangedMessage)
    rejection = MagicMock(spec=hw_messages.CommandRejectedMessage)

    assert manager.config_active is True
    assert manager.snapshot() is binding.snapshot_value
    assert manager.context_for_control("0,0") is binding.context_value
    assert manager.action_interest_snapshot(now=12.0) is binding.interest_value
    assert await manager.set_page(profile="default", page=0) is True
    assert (await manager.open_page(descriptor=MagicMock(), context_id="ctx")).kind == (
        "dynamic-session"
    )
    await manager.replace_page(descriptor=MagicMock(), context_id="ctx")
    await manager.close_page(context_id="ctx", reason="done")
    await manager.clear_page(clear_outputs=False, reason="reset")
    await manager.on_device_descriptor_changed(descriptor)
    await manager.on_capability_state_changed(event)
    await manager.on_command_rejected(rejection)
    await manager.on_action_availability_changed({"changed"})
    await manager.handle_provider_command(message)
    await manager.handle_hardware_input(message)

    assert manager.device is descriptor
    assert ("context_for_control", "0,0") in binding.calls
    assert ("action_interest_snapshot", 12.0) in binding.calls
    assert ("set_page", {"profile": "default", "page": 0, "descriptor": None, "causation_id": None}) in binding.calls
    assert ("close_page", {"context_id": "ctx", "reason": "done", "causation_id": None}) in binding.calls
    assert ("clear_page", {"clear_outputs": False, "reason": "reset"}) in binding.calls
    assert ("on_device_descriptor_changed", descriptor) in binding.calls
    assert ("on_capability_state_changed", event) in binding.calls
    assert ("on_command_rejected", rejection) in binding.calls
    assert ("on_action_availability_changed", ("changed",)) in binding.calls
    assert ("handle_provider_command", message) in binding.calls
    assert ("handle_hardware_input", message) in binding.calls


@pytest.mark.asyncio
async def test_device_manager_config_listener_fans_out_config_changes(
    monkeypatch: pytest.MonkeyPatch,
):
    reloaded = _device_config("reloaded-device")
    listener_finished = anyio.Event()

    async def config_stream() -> AsyncIterator[DeviceConfig | None]:
        yield reloaded
        yield None
        listener_finished.set()

    manager = _manager(monkeypatch, config_stream=config_stream())
    binding = _FakeBindingService.instances[0]

    stopping = anyio.Event()
    async with anyio.create_task_group() as tg:
        await manager.start(tg, stopping)
        await listener_finished.wait()
        tg.cancel_scope.cancel()

    assert manager.config is reloaded
    assert ("start", (tg, stopping)) in binding.calls
    assert ("on_config_changed", reloaded) in binding.calls
    assert ("on_config_changed", None) in binding.calls
