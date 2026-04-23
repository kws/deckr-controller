from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import websockets
from deckr.core.component import (
    BaseComponent,
    RunContext,
)
from deckr.core.components import available_component_ids, run_components
from deckr.core.config import ConfigDocument, load_config_document
from deckr.core.messaging import EventBus
from deckr.core.util.anyio import add_signal_handler
from deckr.core.util.host_id import resolve_host_id
from deckr.hardware import events as hw_events
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

_MAX_WS_MESSAGE_SIZE = 10 * 1024 * 1024


class RemoteHWDevice:
    def __init__(
        self,
        *,
        manager_id: str,
        local_device_id: str,
        info: hw_events.HWDeviceInfo,
        send_command: Callable[[hw_events.HardwareOutputCommand], Awaitable[None]],
    ) -> None:
        self._manager_id = manager_id
        self._local_device_id = local_device_id
        self._id = hw_events.build_remote_device_id(manager_id, local_device_id)
        self._hid = f"remote:{manager_id}:{info.hid}"
        self._name = info.name
        self._slots = list(info.slots)
        self._send_command = send_command

    @property
    def id(self) -> str:
        return self._id

    @property
    def hid(self) -> str:
        return self._hid

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def slots(self) -> list[hw_events.HWSlot]:
        return self._slots

    async def _dispatch(self, command: hw_events.HardwareOutputCommand) -> None:
        try:
            await self._send_command(command)
        except (RuntimeError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            logger.debug(
                "Dropping hardware command for disconnected remote device %s",
                self._id,
            )

    async def set_image(self, slot_id: str, image: bytes) -> None:
        await self._dispatch(
            hw_events.SetImageCommand(
                device_id=self._local_device_id,
                slot_id=slot_id,
                image=image,
            )
        )

    async def clear_slot(self, slot_id: str) -> None:
        await self._dispatch(
            hw_events.ClearSlotCommand(
                device_id=self._local_device_id,
                slot_id=slot_id,
            )
        )

    async def sleep_screen(self) -> None:
        await self._dispatch(
            hw_events.SleepScreenCommand(device_id=self._local_device_id)
        )

    async def wake_screen(self) -> None:
        await self._dispatch(
            hw_events.WakeScreenCommand(device_id=self._local_device_id)
        )


class RemoteHardwareWebSocketServer(BaseComponent):
    def __init__(
        self,
        driver_bus: EventBus,
        *,
        controller_id: str,
        host: str,
        port: int,
    ) -> None:
        super().__init__("remote_hardware_ws_server")
        self._driver_bus = driver_bus
        self._controller_id = controller_id
        self._host = host
        self._port = port
        self._bound_port = port
        self._ready = anyio.Event()
        self._active_managers: set[str] = set()
        self._active_managers_lock = anyio.Lock()

    @property
    def bound_port(self) -> int:
        return self._bound_port

    async def wait_ready(self, timeout: float | None = None) -> bool:
        if timeout is None:
            await self._ready.wait()
            return True
        with anyio.move_on_after(timeout) as scope:
            await self._ready.wait()
        return not scope.cancel_called

    async def start(self, ctx: RunContext) -> None:
        ctx.tg.start_soon(self._run_server)

    async def _run_server(self) -> None:
        async with websockets.serve(
            self._handle_connection,
            self._host,
            self._port,
            ping_interval=20,
            ping_timeout=20,
            max_size=_MAX_WS_MESSAGE_SIZE,
        ) as server:
            sockets = list(server.sockets or [])
            if sockets:
                self._bound_port = sockets[0].getsockname()[1]
            self._ready.set()
            logger.info(
                "Remote hardware websocket listening on %s:%s",
                self._host,
                self._bound_port,
            )
            await anyio.sleep_forever()

    async def _register_manager(self, manager_id: str) -> bool:
        async with self._active_managers_lock:
            if manager_id in self._active_managers:
                return False
            self._active_managers.add(manager_id)
            return True

    async def _unregister_manager(self, manager_id: str) -> None:
        async with self._active_managers_lock:
            self._active_managers.discard(manager_id)

    async def _recv_message(self, websocket) -> hw_events.HardwareTransportMessage:
        raw = await websocket.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return hw_events.hardware_message_from_wire(data)

    async def _send_message(
        self,
        websocket,
        message: hw_events.HardwareTransportMessage,
    ) -> None:
        await websocket.send(json.dumps(hw_events.hardware_message_to_wire(message)))

    async def _handle_connection(self, websocket, *args) -> None:
        manager_id: str | None = None
        manager_registered = False
        devices_by_local_id: dict[str, RemoteHWDevice] = {}
        command_send, command_receive = anyio.create_memory_object_stream[
            hw_events.HardwareOutputCommand
        ](max_buffer_size=100)

        async def send_command(command: hw_events.HardwareOutputCommand) -> None:
            await command_send.send(command)

        async def writer_loop() -> None:
            async with command_receive:
                async for command in command_receive:
                    await self._send_message(
                        websocket,
                        hw_events.command_to_transport_message(command),
                    )

        try:
            hello = await self._recv_message(websocket)
            if not isinstance(hello, hw_events.ManagerHelloMessage):
                logger.warning("Rejecting websocket connection without managerHello")
                await websocket.close(code=1008, reason="expected managerHello")
                return
            manager_id = hello.manager_id
            manager_registered = await self._register_manager(manager_id)
            if not manager_registered:
                logger.error("Rejecting duplicate remote manager id %s", manager_id)
                await websocket.close(code=1008, reason="duplicate manager id")
                return

            await self._send_message(
                websocket,
                hw_events.ControllerHelloMessage(controller_id=self._controller_id),
            )
            logger.info("Remote manager %s connected", manager_id)

            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(writer_loop)
                    async for raw in websocket:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        data = json.loads(raw)
                        message = hw_events.hardware_message_from_wire(data)
                        await self._handle_runtime_message(
                            message,
                            manager_id=manager_id,
                            devices_by_local_id=devices_by_local_id,
                            send_command=send_command,
                        )
                    tg.cancel_scope.cancel()
            except* ConnectionClosed:
                pass
        except ConnectionClosed:
            pass
        finally:
            await command_send.aclose()
            for device in list(devices_by_local_id.values()):
                await self._driver_bus.send(
                    hw_events.DeviceDisconnectedEvent(device_id=device.id)
                )
            if manager_id is not None:
                await self._unregister_manager(manager_id)
                logger.info("Remote manager %s disconnected", manager_id)

    async def _handle_runtime_message(
        self,
        message: hw_events.HardwareTransportMessage,
        *,
        manager_id: str,
        devices_by_local_id: dict[str, RemoteHWDevice],
        send_command: Callable[[hw_events.HardwareOutputCommand], Awaitable[None]],
    ) -> None:
        if isinstance(message, hw_events.DeviceConnectedMessage):
            previous = devices_by_local_id.pop(message.device_id, None)
            if previous is not None:
                await self._driver_bus.send(
                    hw_events.DeviceDisconnectedEvent(device_id=previous.id)
                )

            info = hw_events.device_info_from_wire(message.device)
            proxy = RemoteHWDevice(
                manager_id=manager_id,
                local_device_id=message.device_id,
                info=info,
                send_command=send_command,
            )
            devices_by_local_id[message.device_id] = proxy
            await self._driver_bus.send(
                hw_events.DeviceConnectedEvent(device_id=proxy.id, device=proxy)
            )
            return

        if isinstance(message, hw_events.DeviceDisconnectedMessage):
            device = devices_by_local_id.pop(message.device_id, None)
            if device is not None:
                await self._driver_bus.send(
                    hw_events.DeviceDisconnectedEvent(device_id=device.id)
                )
            return

        device = devices_by_local_id.get(getattr(message, "device_id", ""))
        if device is None:
            logger.warning(
                "Ignoring hardware message for unknown device %s from %s",
                getattr(message, "device_id", ""),
                manager_id,
            )
            return

        event = hw_events.transport_message_to_event(message, device_id=device.id)
        await self._driver_bus.send(event)

    async def stop(self) -> None:
        return


class _RemoteDeviceManagerBridge:
    def __init__(self, driver_bus: EventBus, websocket) -> None:
        self._driver_bus = driver_bus
        self._websocket = websocket
        self._devices: dict[str, hw_events.HWDevice] = {}
        self._ready = anyio.Event()
        self._closed = anyio.Event()

    async def run(self) -> None:
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._driver_to_websocket_loop)
                await self._websocket_to_driver_loop()
                tg.cancel_scope.cancel()
        finally:
            self._closed.set()

    async def wait_ready(self, timeout: float | None = None) -> bool:
        if timeout is None:
            await self._ready.wait()
            return True
        with anyio.move_on_after(timeout) as scope:
            await self._ready.wait()
        return not scope.cancel_called

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _send_message(
        self,
        message: hw_events.HardwareTransportMessage,
    ) -> None:
        await self._websocket.send(
            json.dumps(hw_events.hardware_message_to_wire(message))
        )

    async def _driver_to_websocket_loop(self) -> None:
        async with self._driver_bus.subscribe() as stream:
            self._ready.set()
            async for event in stream:
                if not isinstance(
                    event,
                    (
                        hw_events.DeviceConnectedEvent,
                        hw_events.DeviceDisconnectedEvent,
                        hw_events.KeyDownEvent,
                        hw_events.KeyUpEvent,
                        hw_events.DialRotateEvent,
                        hw_events.TouchTapEvent,
                        hw_events.TouchSwipeEvent,
                    ),
                ):
                    continue

                if isinstance(event, hw_events.DeviceConnectedEvent):
                    self._devices[event.device_id] = event.device
                elif isinstance(event, hw_events.DeviceDisconnectedEvent):
                    self._devices.pop(event.device_id, None)

                await self._send_message(hw_events.event_to_transport_message(event))

    async def _websocket_to_driver_loop(self) -> None:
        async for raw in self._websocket:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            message = hw_events.hardware_message_from_wire(data)
            if isinstance(message, hw_events.ControllerHelloMessage):
                continue

            command = hw_events.transport_message_to_command(message)
            device = self._devices.get(command.device_id)
            if device is None:
                logger.warning(
                    "Ignoring controller command for unknown local device %s",
                    command.device_id,
                )
                continue
            await hw_events.apply_command(device, command)


class RemoteDeviceManagerService:
    def __init__(
        self,
        *,
        controller_url: str,
        manager_id: str,
        driver_bus: EventBus,
    ) -> None:
        self._controller_url = controller_url
        self._manager_id = manager_id
        self._driver_bus = driver_bus

    async def run(self) -> None:
        backoff = 1.0
        cancelled_exc = anyio.get_cancelled_exc_class()

        while True:
            try:
                async with websockets.connect(
                    self._controller_url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=_MAX_WS_MESSAGE_SIZE,
                ) as websocket:
                    await websocket.send(
                        json.dumps(
                            hw_events.hardware_message_to_wire(
                                hw_events.ManagerHelloMessage(
                                    manager_id=self._manager_id
                                )
                            )
                        )
                    )
                    raw = await websocket.recv()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    hello = hw_events.hardware_message_from_wire(json.loads(raw))
                    if not isinstance(hello, hw_events.ControllerHelloMessage):
                        raise RuntimeError("Expected controllerHello from controller")

                    logger.info(
                        "Connected device manager %s to controller %s",
                        self._manager_id,
                        hello.controller_id,
                    )
                    backoff = 1.0
                    await self._run_connected_session(websocket)
                    logger.warning(
                        "Controller websocket closed for %s; reconnecting",
                        self._manager_id,
                    )
            except cancelled_exc:
                raise
            except Exception:
                logger.exception(
                    "Device manager %s disconnected; retrying in %.1fs",
                    self._manager_id,
                    backoff,
                )
                await anyio.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)

    async def _run_connected_session(self, websocket) -> None:
        bridge = _RemoteDeviceManagerBridge(self._driver_bus, websocket)

        async with anyio.create_task_group() as tg:
            tg.start_soon(bridge.run)
            ready = await bridge.wait_ready(timeout=5.0)
            if not ready:
                raise TimeoutError("Timed out waiting for remote hardware bridge")
            try:
                await bridge.wait_closed()
            finally:
                tg.cancel_scope.cancel()


def available_driver_names() -> list[str]:
    return sorted(
        component_id.removeprefix("deckr.drivers.")
        for component_id in available_component_ids()
        if component_id.startswith("deckr.drivers.")
    )


def _parse_driver_args(
    parser: argparse.ArgumentParser,
    *,
    available: list[str],
) -> None:
    kwargs: dict[str, object] = {
        "action": "append",
        "dest": "drivers",
        "metavar": "DRIVER",
        "help": "Hardware driver to enable. Repeatable.",
    }
    if available:
        kwargs["choices"] = available
    parser.add_argument("--driver", **kwargs)


def _parse_device_manager_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deckr remote device manager")
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        metavar="PATH",
        help="Load configuration from PATH instead of CLI/controller env flags.",
    )
    parser.add_argument(
        "--print-default-config",
        action="store_true",
        dest="print_default_config",
        help="Print the built-in default deckr.toml document and exit.",
    )
    parser.add_argument(
        "--controller-url",
        default=os.getenv("DECKR_CONTROLLER_URL"),
        help="Controller websocket URL (or DECKR_CONTROLLER_URL).",
    )
    parser.add_argument(
        "--manager-id",
        default=None,
        metavar="ID",
        help="Stable device manager ID (DEVICE_MANAGER_ID env, else hostname).",
    )
    _parse_driver_args(parser, available=available_driver_names())
    return parser.parse_args()


async def device_manager_async_main(
    controller_url: str,
    manager_id: str,
    driver_names: tuple[str, ...] | None,
) -> None:
    service = RemoteDeviceManagerService(
        controller_url=controller_url,
        manager_id=manager_id,
        driver_names=driver_names,
    )
    await service.run()


def _device_manager_default_config_text() -> str:
    return """# Deckr remote device manager

[deckr.controller.remote_device_manager]
controller_url = "ws://127.0.0.1:9876"
"""


def _device_manager_selected_drivers(document: ConfigDocument) -> tuple[str, ...] | None:
    config = document.namespace("deckr.controller.remote_device_manager") or {}
    raw_drivers = config.get("drivers")
    if not isinstance(raw_drivers, tuple | list):
        return None

    selected = tuple(
        str(name).strip()
        for name in raw_drivers
        if str(name).strip()
    )
    return selected or None


def _device_manager_component_filter(document: ConfigDocument) -> Callable[[str], bool]:
    selected = _device_manager_selected_drivers(document)
    enabled_driver_ids = None
    if selected is not None:
        enabled_driver_ids = {
            f"deckr.drivers.{name.removeprefix('deckr.drivers.')}"
            for name in selected
        }

    def include(component_id: str) -> bool:
        if component_id == "deckr.controller.remote_device_manager":
            return True
        if not component_id.startswith("deckr.drivers."):
            return False
        if enabled_driver_ids is None:
            return True
        return component_id in enabled_driver_ids

    return include


def _device_manager_config_text(
    *,
    controller_url: str,
    manager_id: str,
    driver_names: tuple[str, ...] | None,
) -> str:
    lines = [
        "[deckr.controller.remote_device_manager]",
        f"controller_url = {json.dumps(controller_url)}",
        f"manager_id = {json.dumps(manager_id)}",
    ]
    if driver_names:
        driver_values = ", ".join(json.dumps(name) for name in driver_names)
        lines.append(f"drivers = [{driver_values}]")
    return "\n".join(lines)


async def _device_manager_component_main(document: ConfigDocument) -> None:
    async with anyio.create_task_group() as tg:
        await add_signal_handler(tg)
        await run_components(
            document,
            component_filter=_device_manager_component_filter(document),
        )
        tg.cancel_scope.cancel()


def device_manager_main() -> None:
    log_level = logging.DEBUG if os.getenv("DEBUG") == "1" else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    args = _parse_device_manager_args()
    if args.print_default_config:
        print(_device_manager_default_config_text())
        return

    if args.config_path:
        document = load_config_document(Path(args.config_path).expanduser().resolve())
    else:
        if not args.controller_url:
            raise SystemExit("A controller websocket URL is required.")
        manager_id = resolve_host_id(
            cli_value=args.manager_id,
            env_var="DEVICE_MANAGER_ID",
            fallback_to_hostname=True,
            fallback_to_uuid=True,
        )
        driver_names = (
            tuple(args.drivers) if args.drivers else tuple(available_driver_names())
        )
        document = load_config_document(
            None,
            default_text=_device_manager_config_text(
                controller_url=args.controller_url,
                manager_id=manager_id,
                driver_names=driver_names,
            ),
        )

    anyio.run(_device_manager_component_main, document)
