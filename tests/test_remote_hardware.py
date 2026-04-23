from __future__ import annotations

import json

import anyio
import pytest
import websockets
from deckr.core.messaging import EventBus
from deckr.hardware import events as hw_events
from websockets.exceptions import ConnectionClosed

from deckr.controller._remote_hardware import (
    RemoteDeviceManagerService,
    RemoteHardwareWebSocketServer,
)


class StubDevice:
    def __init__(self, device_id: str = "virtual-1") -> None:
        self.id = device_id
        self.hid = f"virtual:{device_id}"
        self.slots = [
            hw_events.HWSlot(
                id="0,0",
                coordinates=hw_events.Coordinates(column=0, row=0),
                image_format=hw_events.HWSImageFormat(width=72, height=72),
                gestures=frozenset({"key_down", "key_up"}),
            )
        ]

    async def set_image(self, slot_id: str, image: bytes) -> None:
        return

    async def clear_slot(self, slot_id: str) -> None:
        return

    async def sleep_screen(self) -> None:
        return

    async def wake_screen(self) -> None:
        return


async def _next_event(stream, event_type):
    with anyio.fail_after(3):
        while True:
            event = await stream.receive()
            if isinstance(event, event_type):
                return event


@pytest.mark.asyncio
async def test_remote_hardware_server_bridges_device_events_and_commands():
    driver_bus = EventBus()
    server = RemoteHardwareWebSocketServer(
        driver_bus=driver_bus,
        controller_id="controller-1",
        host="127.0.0.1",
        port=0,
    )

    async with anyio.create_task_group() as tg:
        await server.start(type("Ctx", (), {"tg": tg})())
        assert await server.wait_ready(timeout=3.0)

        uri = f"ws://127.0.0.1:{server.bound_port}"
        async with driver_bus.subscribe() as stream:
            async with websockets.connect(uri) as websocket:
                await websocket.send(
                    json.dumps(
                        hw_events.hardware_message_to_wire(
                            hw_events.ManagerHelloMessage(manager_id="bedroom-pi")
                        )
                    )
                )
                hello = hw_events.hardware_message_from_wire(
                    json.loads(await websocket.recv())
                )
                assert isinstance(hello, hw_events.ControllerHelloMessage)
                assert hello.controller_id == "controller-1"

                device = StubDevice()
                await websocket.send(
                    json.dumps(
                        hw_events.hardware_message_to_wire(
                            hw_events.DeviceConnectedMessage(
                                device_id=device.id,
                                device=hw_events.device_info_to_wire(
                                    hw_events.device_info_from_device(device)
                                ),
                            )
                        )
                    )
                )

                connected = await _next_event(stream, hw_events.DeviceConnectedEvent)
                assert connected.device_id == hw_events.build_remote_device_id(
                    "bedroom-pi", "virtual-1"
                )

                await connected.device.set_image("0,0", b"\x01\x02\x03")
                command = hw_events.hardware_message_from_wire(
                    json.loads(await websocket.recv())
                )
                assert isinstance(command, hw_events.SetImageMessage)
                assert command.device_id == "virtual-1"
                assert command.slot_id == "0,0"
                assert command.image == b"\x01\x02\x03"

                await websocket.send(
                    json.dumps(
                        hw_events.hardware_message_to_wire(
                            hw_events.KeyDownMessage(
                                device_id="virtual-1",
                                key_id="0,0",
                            )
                        )
                    )
                )
                key_down = await _next_event(stream, hw_events.KeyDownEvent)
                assert key_down.device_id == connected.device_id
                assert key_down.key_id == "0,0"

            disconnected = await _next_event(stream, hw_events.DeviceDisconnectedEvent)
            assert disconnected.device_id == connected.device_id

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_remote_hardware_server_ignores_abrupt_disconnect(caplog):
    driver_bus = EventBus()
    server = RemoteHardwareWebSocketServer(
        driver_bus=driver_bus,
        controller_id="controller-1",
        host="127.0.0.1",
        port=0,
    )

    caplog.set_level("ERROR")

    async with anyio.create_task_group() as tg:
        await server.start(type("Ctx", (), {"tg": tg})())
        assert await server.wait_ready(timeout=3.0)

        uri = f"ws://127.0.0.1:{server.bound_port}"
        async with driver_bus.subscribe() as stream:
            websocket = await websockets.connect(uri)
            try:
                await websocket.send(
                    json.dumps(
                        hw_events.hardware_message_to_wire(
                            hw_events.ManagerHelloMessage(manager_id="bedroom-pi")
                        )
                    )
                )
                hello = hw_events.hardware_message_from_wire(
                    json.loads(await websocket.recv())
                )
                assert isinstance(hello, hw_events.ControllerHelloMessage)

                device = StubDevice()
                await websocket.send(
                    json.dumps(
                        hw_events.hardware_message_to_wire(
                            hw_events.DeviceConnectedMessage(
                                device_id=device.id,
                                device=hw_events.device_info_to_wire(
                                    hw_events.device_info_from_device(device)
                                ),
                            )
                        )
                    )
                )
                connected = await _next_event(stream, hw_events.DeviceConnectedEvent)
                assert connected.device_id == hw_events.build_remote_device_id(
                    "bedroom-pi", "virtual-1"
                )

                websocket.transport.abort()
                disconnected = await _next_event(
                    stream, hw_events.DeviceDisconnectedEvent
                )
                assert disconnected.device_id == connected.device_id
            finally:
                try:
                    await websocket.close()
                except ConnectionClosed:
                    pass

        assert not any(
            record.name == "websockets.server"
            and "connection handler failed" in record.getMessage()
            for record in caplog.records
        )

        tg.cancel_scope.cancel()
@pytest.mark.asyncio
async def test_remote_device_manager_reconnects_and_rediscover_devices():
    connections: list[str] = []
    reconnected = anyio.Event()

    async def handler(websocket, *args) -> None:
        hello = hw_events.hardware_message_from_wire(json.loads(await websocket.recv()))
        assert isinstance(hello, hw_events.ManagerHelloMessage)
        connections.append(hello.manager_id)
        await websocket.send(
            json.dumps(
                hw_events.hardware_message_to_wire(
                    hw_events.ControllerHelloMessage(controller_id="controller-1")
                )
            )
        )

        message = hw_events.hardware_message_from_wire(
            json.loads(await websocket.recv())
        )
        assert isinstance(message, hw_events.DeviceConnectedMessage)
        if len(connections) >= 2:
            reconnected.set()
        await websocket.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        driver_bus = EventBus()
        service = RemoteDeviceManagerService(
            controller_url=f"ws://127.0.0.1:{port}",
            manager_id="bedroom-pi",
            driver_bus=driver_bus,
        )

        async def emit_when_connected(expected_count: int) -> None:
            with anyio.fail_after(5):
                while len(connections) < expected_count:
                    await anyio.sleep(0.05)
            await driver_bus.send(
                hw_events.DeviceConnectedEvent(
                    device_id="virtual-1",
                    device=StubDevice(),
                )
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(service.run)
            tg.start_soon(emit_when_connected, 1)
            tg.start_soon(emit_when_connected, 2)
            with anyio.fail_after(5):
                await reconnected.wait()
            tg.cancel_scope.cancel()

    assert connections[:2] == ["bedroom-pi", "bedroom-pi"]
