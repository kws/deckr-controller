from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

import anyio
from deckr.components import (
    BaseComponent,
    Component,
    ComponentContext,
    ComponentDefinition,
    ComponentManager,
    ComponentManifest,
    RunContext,
)
from deckr.contracts.messages import (
    ACTIONS_LANE,
    HARDWARE_MESSAGES_LANE,
    controller_address,
)
from deckr.lanes import Lane, RegisteredEndpointLane
from deckr.state import (
    DEFAULT_DISCOVERY_STATE_STORE_NAME,
    DEFAULT_LEASE_STATE_STORE_NAME,
    PERSISTENT_STATE_STORE_POLICY,
    StateStore,
)

from deckr.controller._config_document import (
    ControllerRuntimeConfig,
    parse_controller_config,
)
from deckr.controller._controller_service import ControllerService
from deckr.controller._runtime_support import (
    build_config_service,
    build_settings_service,
)
from deckr.controller.action_provider.action_registry import ActionRegistry


@dataclass(frozen=True, slots=True)
class ControllerRuntime:
    config_source: Mapping[str, object]
    config: ControllerRuntimeConfig
    controller_id: str


class ControllerRuntimeService(BaseComponent):
    def __init__(
        self,
        *,
        runtime_name: str,
        runtime: ControllerRuntime,
        hardware_messages: Lane,
        actions: Lane,
        lease_state: StateStore,
        discovery_state: StateStore,
        materialized_config_state: StateStore | None = None,
    ) -> None:
        super().__init__(name=runtime_name)
        self._runtime = runtime
        self._hardware_messages = hardware_messages
        self._actions = actions
        self._lease_state = lease_state
        self._discovery_state = discovery_state
        self._materialized_config_state = materialized_config_state
        self._component_manager = ComponentManager()
        self._hardware_endpoint_cm: (
            AbstractAsyncContextManager[RegisteredEndpointLane] | None
        ) = None
        self._actions_endpoint_cm: (
            AbstractAsyncContextManager[RegisteredEndpointLane] | None
        ) = None
        self._hardware_endpoint: RegisteredEndpointLane | None = None
        self._actions_endpoint: RegisteredEndpointLane | None = None

    async def start(self, ctx: RunContext) -> None:
        manager_started = False
        try:
            self._hardware_endpoint_cm = self._hardware_messages.register_endpoint(
                controller_address(self._runtime.controller_id),
                metadata={"runtime": self.name, "role": "controller"},
                task_group=ctx.tg,
            )
            self._hardware_endpoint = await self._hardware_endpoint_cm.__aenter__()
            self._actions_endpoint_cm = self._actions.register_endpoint(
                controller_address(self._runtime.controller_id),
                metadata={"runtime": self.name, "role": "controller"},
                task_group=ctx.tg,
            )
            self._actions_endpoint = await self._actions_endpoint_cm.__aenter__()

            ctx.tg.start_soon(self._component_manager.run)
            manager_started = True

            config_service = build_config_service(
                self._runtime.config,
                controller_id=self._runtime.controller_id,
                materialized_state=self._materialized_config_state,
            )
            if isinstance(config_service, Component):
                await self._component_manager.add_component(config_service)

            controller_service: ControllerService | None = None

            async def on_actions_changed(event) -> None:
                if controller_service is not None:
                    await controller_service.handle_actions_changed_event(event)

            action_registry = ActionRegistry(
                lease_state=self._lease_state,
                discovery_state=self._discovery_state,
                controller_id=self._runtime.controller_id,
                on_actions_changed=on_actions_changed,
            )
            await self._component_manager.add_component(action_registry)
            settings_service = build_settings_service(
                self._runtime.config,
                controller_id=self._runtime.controller_id,
                config_service=config_service,
                action_provider=action_registry.get_action,
            )

            controller_service = ControllerService(
                hardware_endpoint=self._hardware_endpoint,
                lease_state=self._lease_state,
                discovery_state=self._discovery_state,
                config_service=config_service,
                settings_service=settings_service,
                controller_id=self._runtime.controller_id,
                action_registry=action_registry,
                actions_endpoint=self._actions_endpoint,
            )
            await self._component_manager.add_component(controller_service)
        except BaseException:
            with anyio.CancelScope(shield=True):
                if manager_started:
                    await self._component_manager.stop()
                await self._close_endpoint_contexts()
            raise

    async def stop(self) -> None:
        await self._component_manager.stop()
        await self._close_endpoint_contexts()

    async def _close_endpoint_contexts(self) -> None:
        actions_cm = self._actions_endpoint_cm
        self._actions_endpoint_cm = None
        self._actions_endpoint = None
        if actions_cm is not None:
            await actions_cm.__aexit__(None, None, None)
        hardware_cm = self._hardware_endpoint_cm
        self._hardware_endpoint_cm = None
        self._hardware_endpoint = None
        if hardware_cm is not None:
            await hardware_cm.__aexit__(None, None, None)


def build_controller_runtime(
    *,
    config_source: dict,
    base_dir: Path,
    controller_id: str,
) -> ControllerRuntime:
    config = parse_controller_config(config_source, base_dir=base_dir)
    return ControllerRuntime(
        config_source=dict(config_source),
        config=config,
        controller_id=controller_id,
    )


def component_factory(context: ComponentContext):
    source = dict(context.config)
    runtime = build_controller_runtime(
        config_source=source,
        base_dir=context.base_dir,
        controller_id=context.require_endpoint_id("controller"),
    )
    materialized_config_state = None
    device_config = runtime.config.device_config
    if device_config is not None and device_config.materialized is not None:
        materialized_config_state = context.state(
            device_config.materialized.bucket,
            policy=PERSISTENT_STATE_STORE_POLICY,
        )

    return ControllerRuntimeService(
        runtime_name=context.runtime_name,
        runtime=runtime,
        hardware_messages=context.require_lane(HARDWARE_MESSAGES_LANE),
        actions=context.require_lane(ACTIONS_LANE),
        lease_state=context.state(DEFAULT_LEASE_STATE_STORE_NAME),
        discovery_state=context.state(DEFAULT_DISCOVERY_STATE_STORE_NAME),
        materialized_config_state=materialized_config_state,
    )

component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id="dev.deckr.controller",
        consumes=(HARDWARE_MESSAGES_LANE, ACTIONS_LANE),
        publishes=(HARDWARE_MESSAGES_LANE, ACTIONS_LANE),
        endpoint_slots=("controller",),
        role="controller",
    ),
    factory=component_factory,
)
