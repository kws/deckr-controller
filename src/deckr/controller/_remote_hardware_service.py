from __future__ import annotations

from deckr.core.component import BaseComponent
from deckr.core.components import (
    ComponentContext,
    ComponentDefinition,
    ComponentManifest,
    InactiveComponent,
)
from deckr.core.util.host_id import resolve_host_id
from pydantic import BaseModel, ConfigDict

from deckr.controller._remote_hardware import RemoteDeviceManagerService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemoteHardwareWebSocketConfig(_StrictModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 0


class RemoteDeviceManagerConfig(_StrictModel):
    enabled: bool = True
    controller_url: str = ""
    manager_id: str | None = None
    drivers: tuple[str, ...] = ()


class RemoteDeviceManagerComponent(BaseComponent):
    def __init__(self, runtime_name: str, service: RemoteDeviceManagerService) -> None:
        super().__init__(name=runtime_name)
        self._service = service

    async def start(self, ctx) -> None:
        ctx.tg.start_soon(self._service.run)

    async def stop(self) -> None:
        return


def component_factory(context: ComponentContext):
    source = dict(context.raw_config)
    if not source:
        return InactiveComponent(name=context.runtime_name)

    config = RemoteDeviceManagerConfig.model_validate(source)
    if not config.enabled:
        return InactiveComponent(name=context.runtime_name)
    if not config.controller_url.strip():
        raise ValueError("remote device manager requires controller_url")

    service = RemoteDeviceManagerService(
        controller_url=config.controller_url,
        manager_id=resolve_host_id(
            cli_value=config.manager_id,
            env_var="DEVICE_MANAGER_ID",
            fallback_to_hostname=True,
            fallback_to_uuid=True,
        ),
        driver_bus=context.require_lane("hardware_events"),
    )
    return RemoteDeviceManagerComponent(context.runtime_name, service)


component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id="deckr.controller.remote_device_manager",
        config_prefix="deckr.controller.remote_device_manager",
        consumes=("hardware_events",),
    ),
    factory=component_factory,
)
