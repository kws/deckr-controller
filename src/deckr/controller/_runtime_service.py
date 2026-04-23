from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from deckr.core.component import BaseComponent, Component, ComponentManager, RunContext
from deckr.core.components import (
    ComponentContext,
    ComponentDefinition,
    ComponentManifest,
)
from deckr.core.messaging import EventBus
from deckr.core.util.host_id import resolve_controller_id

from deckr.controller._config_document import (
    ControllerRuntimeConfig,
    parse_controller_config,
)
from deckr.controller._controller_service import ControllerService
from deckr.controller._remote_hardware import RemoteHardwareWebSocketServer
from deckr.controller._remote_hardware_service import RemoteHardwareWebSocketConfig
from deckr.controller._service import _build_config_service, _build_settings_service
from deckr.controller.plugin.action_registry import ActionRegistry


@dataclass(frozen=True, slots=True)
class ControllerRuntime:
    raw_config: Mapping[str, object]
    config: ControllerRuntimeConfig
    controller_id: str


class ControllerRuntimeService(BaseComponent):
    def __init__(
        self,
        *,
        runtime_name: str,
        runtime: ControllerRuntime,
        hardware_events: EventBus,
        plugin_messages: EventBus,
    ) -> None:
        super().__init__(name=runtime_name)
        self._runtime = runtime
        self._hardware_events = hardware_events
        self._plugin_messages = plugin_messages
        self._component_manager = ComponentManager()

    async def start(self, ctx: RunContext) -> None:
        ctx.tg.start_soon(self._component_manager.run)

        config_service = _build_config_service(self._runtime.config)
        if isinstance(config_service, Component):
            await self._component_manager.add_component(config_service)
        settings_service = _build_settings_service(self._runtime.config)

        action_registry = ActionRegistry(
            event_bus=self._plugin_messages,
            controller_id=self._runtime.controller_id,
        )
        await self._component_manager.add_component(action_registry)

        controller_service = ControllerService(
            driver_bus=self._hardware_events,
            config_service=config_service,
            settings_service=settings_service,
            controller_id=self._runtime.controller_id,
            action_registry=action_registry,
            plugin_bus=self._plugin_messages,
        )
        await self._component_manager.add_component(controller_service)
        websocket = _build_remote_hardware_websocket(
            raw_config=self._runtime.raw_config,
            controller_id=self._runtime.controller_id,
            hardware_events=self._hardware_events,
        )
        if websocket is not None:
            await self._component_manager.add_component(websocket)

    async def stop(self) -> None:
        await self._component_manager.stop()


def build_controller_runtime(
    *,
    raw_config: dict,
    base_dir: Path,
) -> ControllerRuntime:
    config = parse_controller_config(raw_config, base_dir=base_dir)
    controller_id = resolve_controller_id(cli_value=config.id)
    return ControllerRuntime(
        raw_config=dict(raw_config),
        config=config,
        controller_id=controller_id,
    )


def _remote_websocket_payload(
    source: Mapping[str, object],
) -> Mapping[str, object]:
    remote_hardware = source.get("remote_hardware")
    if not isinstance(remote_hardware, Mapping):
        return {}
    websocket = remote_hardware.get("websocket")
    if not isinstance(websocket, Mapping):
        return {}
    return websocket


def _build_remote_hardware_websocket(
    *,
    raw_config: Mapping[str, object],
    controller_id: str,
    hardware_events: EventBus,
) -> RemoteHardwareWebSocketServer | None:
    source = _remote_websocket_payload(raw_config)
    if not source:
        return None
    config = RemoteHardwareWebSocketConfig.model_validate(dict(source))
    if not config.enabled:
        return None
    return RemoteHardwareWebSocketServer(
        hardware_events,
        controller_id=controller_id,
        host=config.host,
        port=config.port,
    )


def component_factory(context: ComponentContext) -> ControllerRuntimeService:
    runtime = build_controller_runtime(
        raw_config=dict(context.raw_config),
        base_dir=context.base_dir,
    )
    return ControllerRuntimeService(
        runtime_name=context.runtime_name,
        runtime=runtime,
        hardware_events=context.require_lane("hardware_events"),
        plugin_messages=context.require_lane("plugin_messages"),
    )


component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id="deckr.controller",
        config_prefix="deckr.controller",
        consumes=("hardware_events", "plugin_messages"),
        publishes=("plugin_messages",),
    ),
    factory=component_factory,
)
