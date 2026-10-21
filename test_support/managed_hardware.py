"""Real managed endpoints over an in-process transport for controller integration."""

import io
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
from deckr.contracts.lanes import CORE_LANE_CONTRACTS, MessageContractRegistry
from deckr.hardware.capabilities import (
    button_momentary_value_schema,
    device_power_command_schema,
    raster_bitmap_command_schema,
)
from deckr.hardware.descriptors import (
    DECKR_DEVICE_POWER,
    CapabilityDescriptor,
    ControlDescriptor,
    DeviceDescriptor,
)
from deckr.runtime import Deckr
from PIL import Image

from deckr.controller._hardware import ControllerHardwareService
from deckr.controller.config import DeviceConfig, DeviceConfigMatch, Page, Profile
from test_support.memory_lane_substrate import MemoryLaneSubstrate


def _image(format="JPEG", size=(72, 72)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size).save(stream, format=format)
    return stream.getvalue()


def device_descriptor(*, power: bool = False) -> DeviceDescriptor:
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
                commandSchema=device_power_command_schema(),
            ),
        )
    return DeviceDescriptor(
        deviceId="device-a",
        fingerprint="fingerprint:device-a",
        displayName="Device A",
        capabilities=capabilities,
        controls=(
            ControlDescriptor(
                controlId="key-1",
                kind="key",
                inputCapabilities=(
                    CapabilityDescriptor(
                        capabilityId="input",
                        family="dev.deckr.input.button",
                        type="momentary",
                        direction="input",
                        access=("emits",),
                        eventTypes=("down", "up"),
                        valueSchema=button_momentary_value_schema(),
                    ),
                ),
                outputCapabilities=(
                    CapabilityDescriptor(
                        capabilityId="raster",
                        family="dev.deckr.output.raster",
                        type="bitmap",
                        direction="output",
                        access=("settable",),
                        commandTypes=("set_frame", "clear"),
                        commandSchema=raster_bitmap_command_schema(),
                    ),
                ),
            ),
        ),
    )


class MemoryConfigService:
    def __init__(self, *configs: DeviceConfig) -> None:
        self._configs = {config.id: config for config in configs}
        self._subscribers: dict[
            str,
            set[anyio.abc.ObjectSendStream[DeviceConfig | None]],
        ] = {}

    async def match_device(
        self,
        *,
        fingerprint: str,
        labels: Mapping[str, str],
    ) -> DeviceConfig | None:
        matches = [
            config
            for config in self._configs.values()
            if config.enabled
            and config.match.fingerprint == fingerprint
            and all(
                labels.get(key) == value for key, value in config.match.labels.items()
            )
        ]
        if len(matches) > 1:
            raise ValueError("ambiguous config")
        return matches[0] if matches else None

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        return self._configs.get(config_id)

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        self._configs[config.id] = config
        await self._notify(config.id, config)
        return config

    async def remove_config(self, config_id: str) -> None:
        self._configs.pop(config_id, None)
        await self._notify(config_id, None)

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        return self._subscribe(config_id)

    async def _subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        send, receive = anyio.create_memory_object_stream[DeviceConfig | None](10)
        self._subscribers.setdefault(config_id, set()).add(send)
        try:
            yield self._configs.get(config_id)
            async with receive:
                async for config in receive:
                    yield config
        finally:
            self._subscribers.get(config_id, set()).discard(send)
            await send.aclose()

    async def _notify(
        self,
        config_id: str,
        config: DeviceConfig | None,
    ) -> None:
        for subscriber in tuple(self._subscribers.get(config_id, set())):
            await subscriber.send(config)


def _config(
    *,
    config_id: str = "config-room-a",
    fingerprint: str = "serial-a",
    labels: dict[str, str] | None = None,
) -> DeviceConfig:
    return DeviceConfig(
        id=config_id,
        name="Test Device",
        match=DeviceConfigMatch(fingerprint=fingerprint, labels=labels or {}),
        profiles=[Profile(name="default", pages=[Page(controls=[])])],
    )


async def eventually(predicate):
    with anyio.fail_after(5):
        while not predicate():
            await anyio.sleep(0.005)


@asynccontextmanager
async def setup(
    *,
    labels=None,
    config_labels=None,
    power=True,
    wait=True,
    config_service=None,
    substrate=None,
):
    contracts = MessageContractRegistry(CORE_LANE_CONTRACTS.values())
    substrate = substrate or MemoryLaneSubstrate(lane_contracts=contracts)
    config_service = config_service or MemoryConfigService(
        _config(
            config_id="config-a",
            fingerprint="fingerprint:device-a",
            labels=config_labels,
        )
    )
    callbacks = SimpleNamespace(
        **{
            name: AsyncMock()
            for name in (
                "on_hardware_connected",
                "on_hardware_disconnected",
                "on_hardware_control_input",
                "on_hardware_capability_state_changed",
                "on_hardware_command_rejected",
            )
        }
    )
    commands = AsyncMock(return_value=True)
    async with (
        Deckr(lane_contracts=contracts, message_bus=substrate) as runtime,
        runtime.endpoint("hardware_manager:manager-a") as manager_endpoint,
        runtime.endpoint("controller:controller-main") as endpoint,
        runtime.hardware_manager(
            manager_endpoint,
            manager_id="manager-a",
            devices={"device-a": device_descriptor(power=power)},
            labels=labels,
            command_handler=commands,
        ) as manager,
        anyio.create_task_group() as tg,
    ):
        service = ControllerHardwareService(
            hardware=runtime.hardware(endpoint),
            config_service=config_service,
            callbacks=callbacks,
        )
        stopping = anyio.Event()
        await service.start(tg, stopping)
        try:
            if wait:
                await eventually(
                    lambda: service.route_for_config("config-a") is not None
                )
            yield SimpleNamespace(
                runtime=runtime,
                substrate=substrate,
                manager=manager,
                manager_endpoint=manager_endpoint,
                endpoint=endpoint,
                service=service,
                config_service=config_service,
                callbacks=callbacks,
                commands=commands,
                stopping=stopping,
            )
        finally:
            stopping.set()
            await service.aclose()
            tg.cancel_scope.cancel()
