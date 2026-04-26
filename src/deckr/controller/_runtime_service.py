from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from deckr.core.component import BaseComponent, Component, ComponentManager, RunContext
from deckr.core.components import (
    ComponentContext,
    ComponentDefinition,
    ComponentManifest,
    InactiveComponent,
)
from deckr.core.util.runtime_id import require_runtime_id
from deckr.transports.bus import EventBus

from deckr.controller._config_document import (
    ControllerRuntimeConfig,
    parse_controller_config,
)
from deckr.controller._controller_service import ControllerService
from deckr.controller._runtime_support import (
    build_config_service,
    build_settings_service,
)
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

        config_service = build_config_service(self._runtime.config)
        if isinstance(config_service, Component):
            await self._component_manager.add_component(config_service)
        settings_service = build_settings_service(self._runtime.config)

        controller_service: ControllerService | None = None

        async def on_actions_changed(event) -> None:
            if controller_service is not None:
                await controller_service.handle_actions_changed_event(event)

        action_registry = ActionRegistry(
            event_bus=self._plugin_messages,
            controller_id=self._runtime.controller_id,
            on_actions_changed=on_actions_changed,
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

    async def stop(self) -> None:
        await self._component_manager.stop()


def build_controller_runtime(
    *,
    raw_config: dict,
    base_dir: Path,
) -> ControllerRuntime:
    config = parse_controller_config(raw_config, base_dir=base_dir)
    controller_id = require_runtime_id(
        config.id,
        label="Controller ID",
        source_hint="Set `[deckr.controller].id`.",
    )
    return ControllerRuntime(
        raw_config=dict(raw_config),
        config=config,
        controller_id=controller_id,
    )


def component_factory(context: ComponentContext):
    source = dict(context.raw_config)
    if source.get("enabled") is False:
        return InactiveComponent(name=context.runtime_name)

    runtime = build_controller_runtime(
        raw_config=source,
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
        publishes=("hardware_events", "plugin_messages"),
    ),
    factory=component_factory,
)
