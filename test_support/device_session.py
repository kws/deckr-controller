from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import anyio

from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from test_support.action_catalog import _metadata
from test_support.binding_constants import CONTROLLER_ID
from test_support.binding_runtime import (
    _actions_bus,
    _actions_session,
    _make_binding_service,
    _runtime_sender_for,
    _seed_action_availability,
)
from test_support.device_layouts import (
    FakeHardwareCommandService,
    _hardware_ref,
    _make_mock_device,
)


@asynccontextmanager
async def session(*, settings=None, hardware=None):
    device = _make_mock_device()
    config = DeviceConfig(
        id="test-device",
        name="Session",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(selector={"control_id": "0,0"}, action=action)
                        ]
                    )
                    for action in ("action.first", "action.second")
                ],
            )
        ],
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(side_effect=lambda action, **_: _metadata(action))
    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=hardware or FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(_actions_bus()),
            start_soon=tg.start_soon,
            settings_service=settings,
        )
        _seed_action_availability(
            manager, _metadata("action.first"), _metadata("action.second")
        )
        try:
            yield manager
        finally:
            tg.cancel_scope.cancel()


def gate_notification(manager, message_type):
    sender = _runtime_sender_for(manager)
    send = sender.send_action_runtime_message
    entered, release = anyio.Event(), anyio.Event()

    async def gated(**kwargs):
        if kwargs["message_type"] == message_type and not entered.is_set():
            entered.set()
            await release.wait()
        return await send(**kwargs)

    sender.send_action_runtime_message = gated
    return entered, release
