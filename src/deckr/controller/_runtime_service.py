from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

import anyio
from deckr.beacon import Beacon
from deckr.components import (
    BaseComponent,
    Component,
    ComponentContext,
    ComponentDefinition,
    ComponentManager,
    ComponentManifest,
    RunContext,
)
from deckr.concord import Concord
from deckr.contracts.messages import HARDWARE_MESSAGES_LANE, SERVICES_LANE
from deckr.lanes import EndpointSession
from deckr.services import DeckrServices

from deckr.controller._action_availability import ActionAvailabilityService
from deckr.controller._config_document import (
    ControllerRuntimeConfig,
    parse_controller_config,
)
from deckr.controller._controller_service import ControllerService
from deckr.controller._render_dispatcher import (
    ProcessPoolRenderBackend,
    RenderBackend,
    ThreadRenderBackend,
)
from deckr.controller._render_observation import (
    ObservingRenderBackend,
    RenderObservationOptions,
)
from deckr.controller._runtime_support import (
    build_config_service,
    build_settings_service,
)
from deckr.controller.action_provider.action_registry import ActionRegistry
from deckr.controller.config import materialized_config_bucket_policy


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
        context: ComponentContext,
        beacon: Beacon,
        concord: Concord,
        materialized_config_bucket=None,
    ) -> None:
        super().__init__(name=runtime_name)
        self._runtime = runtime
        self._context = context
        self._beacon = beacon
        self._concord = concord
        self._materialized_config_bucket = materialized_config_bucket
        self._component_manager = ComponentManager()
        self._endpoint_cm: AbstractAsyncContextManager[EndpointSession] | None = None
        self._endpoint: EndpointSession | None = None

    async def start(self, ctx: RunContext) -> None:
        manager_started = False
        try:
            self._endpoint_cm = self._context.open_endpoint(
                "controller",
                metadata={"runtime": self.name, "role": "controller"},
            )
            self._endpoint = await self._endpoint_cm.__aenter__()

            await ctx.tg.start(self._component_manager.run)
            manager_started = True

            config_service = build_config_service(
                self._runtime.config,
                controller_id=self._runtime.controller_id,
                materialized_bucket=self._materialized_config_bucket,
            )
            if isinstance(config_service, Component):
                await self._component_manager.add_component(config_service)

            controller_service: ControllerService | None = None

            async def on_catalog_changed(event) -> None:
                if controller_service is not None:
                    await controller_service.handle_action_catalog_changed_event(event)

            action_registry = ActionRegistry(
                self._beacon,
                controller_id=self._runtime.controller_id,
                on_catalog_changed=on_catalog_changed,
            )
            await self._component_manager.add_component(action_registry)
            services = DeckrServices(
                endpoint=self._endpoint,
                beacon=self._beacon,
                concord=self._concord,
                task_group=ctx.tg,
                kv_bucket_for=self._context.kv_bucket,
            )
            action_availability_service = ActionAvailabilityService(
                controller_id=self._runtime.controller_id,
                controller_session_id=self._endpoint.session_id,
                actions_bus=self._endpoint,
                manager=action_registry,
                services=services,
                close_services_on_aclose=True,
                start_soon=ctx.tg.start_soon,
            )
            settings_service = build_settings_service(
                self._runtime.config,
                controller_id=self._runtime.controller_id,
                config_service=config_service,
                action_provider=action_registry.get_action,
                availability_service=action_availability_service,
            )
            render_backend = _build_render_backend(
                self._runtime.config,
                controller_id=self._runtime.controller_id,
            )

            controller_service = ControllerService(
                endpoint=self._endpoint,
                beacon=self._beacon,
                concord=self._concord,
                config_service=config_service,
                settings_service=settings_service,
                controller_id=self._runtime.controller_id,
                action_registry=action_registry,
                action_availability_service=action_availability_service,
                render_backend=render_backend,
            )
            await self._component_manager.add_component(controller_service)
        except BaseException:
            with anyio.CancelScope(shield=True):
                if manager_started:
                    await self._component_manager.stop()
                await self._close_endpoint_context()
            raise

    async def stop(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._component_manager.stop()
            await self._close_endpoint_context()

    async def _close_endpoint_context(self) -> None:
        endpoint_cm = self._endpoint_cm
        self._endpoint_cm = None
        self._endpoint = None
        if endpoint_cm is not None:
            await endpoint_cm.__aexit__(None, None, None)


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


def _build_render_backend(
    config: ControllerRuntimeConfig,
    *,
    controller_id: str,
) -> RenderBackend | None:
    render = config.render
    if render is None:
        return None

    if render.backend == "thread":
        backend: RenderBackend = ThreadRenderBackend()
    else:
        backend = ProcessPoolRenderBackend()

    observation = render.observation
    if observation is not None and observation.enabled:
        if observation.path is None:
            raise ValueError("render observation path is required when enabled")
        backend = ObservingRenderBackend(
            backend,
            controller_id=controller_id,
            options=RenderObservationOptions(
                path=observation.path,
                include_graph=observation.include_graph,
                include_context=observation.include_context,
            ),
        )

    return backend


def component_factory(context: ComponentContext):
    source = dict(context.config)
    runtime = build_controller_runtime(
        config_source=source,
        base_dir=context.base_dir,
        controller_id=context.require_endpoint_id("controller"),
    )
    context.require_lane(HARDWARE_MESSAGES_LANE)
    context.require_lane(SERVICES_LANE)

    materialized_config_bucket = None
    device_config = runtime.config.device_config
    if device_config is not None and device_config.materialized is not None:
        materialized_config_bucket = context.kv_bucket(
            materialized_config_bucket_policy(device_config.materialized.bucket)
        )

    return ControllerRuntimeService(
        runtime_name=context.runtime_name,
        runtime=runtime,
        context=context,
        beacon=context.require_beacon(),
        concord=context.require_concord(),
        materialized_config_bucket=materialized_config_bucket,
    )


component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id="dev.deckr.controller",
        consumes=(HARDWARE_MESSAGES_LANE, SERVICES_LANE),
        publishes=(HARDWARE_MESSAGES_LANE, SERVICES_LANE),
        endpoint_slots=("controller",),
        role="controller",
    ),
    factory=component_factory,
)
