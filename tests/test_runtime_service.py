from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from deckr.components import (
    BaseComponent,
    ComponentContext,
    ComponentManifest,
    ComponentState,
    RunContext,
    resolve_component_host_plan,
    start_components,
)
from deckr.contracts.lanes import CORE_LANE_CONTRACTS, MessageContractRegistry
from deckr.contracts.messages import ACTIONS_LANE, endpoint_address
from deckr.core.config import ConfigDocument
from deckr.lanes import Lane
from deckr.runtime import Deckr

from deckr.controller._config_document import ControllerRuntimeConfig
from deckr.controller._render_dispatcher import (
    ProcessPoolRenderBackend,
    ThreadRenderBackend,
)
from deckr.controller._render_observation import ObservingRenderBackend
from deckr.controller._runtime_service import (
    ControllerRuntimeService,
    _build_render_backend,
    build_controller_runtime,
    component,
    component_factory,
)
from test_support.memory_lane_substrate import MemoryLaneSubstrate


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


class _FakeComponentContext:
    def __init__(self, config: dict, *, base_dir: Path) -> None:
        self.config = config
        self.base_dir = base_dir
        self.runtime_name = "dev.deckr.controller:main"
        self.lanes: list[str] = []
        self.bucket_policies = []
        self.beacon = object()
        self.concord = object()

    def require_endpoint_id(self, slot: str) -> str:
        assert slot == "controller"
        return "controller-main"

    def require_lane(self, lane: str) -> None:
        self.lanes.append(lane)

    def kv_bucket(self, policy):
        self.bucket_policies.append(policy)
        return {"bucket": policy.bucket}

    def require_beacon(self):
        return self.beacon

    def require_concord(self):
        return self.concord


def test_build_render_backend_returns_none_without_render_config() -> None:
    assert (
        _build_render_backend(
            ControllerRuntimeConfig.model_validate({}),
            controller_id="controller-main",
        )
        is None
    )


def test_build_render_backend_returns_thread_and_process_backends() -> None:
    thread_backend = _build_render_backend(
        ControllerRuntimeConfig.model_validate({"render": {"backend": "thread"}}),
        controller_id="controller-main",
    )
    process_backend = _build_render_backend(
        ControllerRuntimeConfig.model_validate(
            {"render": {"backend": "process_pool"}}
        ),
        controller_id="controller-main",
    )

    assert isinstance(thread_backend, ThreadRenderBackend)
    assert isinstance(process_backend, ProcessPoolRenderBackend)


def test_build_render_backend_wraps_observing_backend(tmp_path: Path) -> None:
    backend = _build_render_backend(
        ControllerRuntimeConfig.model_validate(
            {
                "render": {
                    "backend": "thread",
                    "observation": {
                        "enabled": True,
                        "path": tmp_path / "observations.jsonl",
                        "include_graph": True,
                        "include_context": True,
                    },
                }
            }
        ),
        controller_id="controller-main",
    )

    assert isinstance(backend, ObservingRenderBackend)
    assert isinstance(backend._backend, ThreadRenderBackend)
    assert backend._options.path == tmp_path / "observations.jsonl"
    assert backend._options.include_graph is True
    assert backend._options.include_context is True


def test_component_factory_requests_lanes_without_materialized_bucket(
    tmp_path: Path,
) -> None:
    context = _FakeComponentContext({}, base_dir=tmp_path)

    service = component_factory(context)

    assert isinstance(service, ControllerRuntimeService)
    assert context.lanes == ["hardware_messages", "actions"]
    assert context.bucket_policies == []
    assert service._materialized_config_bucket is None


def test_component_factory_creates_materialized_config_bucket(tmp_path: Path) -> None:
    context = _FakeComponentContext(
        {"device_config": {"materialized": {"bucket": "controller_config"}}},
        base_dir=tmp_path,
    )

    service = component_factory(context)

    assert isinstance(service, ControllerRuntimeService)
    assert context.lanes == ["hardware_messages", "actions"]
    assert [policy.bucket for policy in context.bucket_policies] == [
        "controller_config"
    ]
    assert service._materialized_config_bucket == {"bucket": "controller_config"}


@pytest.mark.asyncio
async def test_controller_component_uses_shared_lanes() -> None:
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "controller_main": {
                            "component": "dev.deckr.controller",
                            "instance_id": "main",
                            "endpoints": {"controller": "controller-main"},
                        }
                    }
                }
            }
        }
    )
    plan = resolve_component_host_plan(
        document,
        definitions={"dev.deckr.controller": component},
    )
    substrate = MemoryLaneSubstrate(lane_contracts=plan.lane_contracts)
    async with (
        Deckr(
            lane_contracts=plan.lane_contracts,
            lanes=plan.lane_names,
            message_bus=substrate,
        ) as deckr,
        start_components(deckr, plan) as result,
    ):
        assert [created.name for created in result.components] == [
            "dev.deckr.controller:main"
        ]
        assert set(result.lane_names) == {"hardware_messages", "actions"}
        assert isinstance(result.get_lane("hardware_messages"), Lane)
        assert isinstance(result.get_lane("actions"), Lane)


@pytest.mark.asyncio
async def test_controller_runtime_keeps_actions_endpoint_open_until_children_stop() -> (
    None
):
    class BorrowedActionsEndpointComponent(BaseComponent):
        def __init__(self, endpoint) -> None:
            super().__init__(name="borrowed-actions-endpoint")
            self._endpoint = endpoint
            self.stop_entered = anyio.Event()
            self.release_stop = anyio.Event()
            self.endpoint_touched = anyio.Event()

        async def start(self, ctx: RunContext) -> None:
            del ctx

        async def stop(self) -> None:
            self.stop_entered.set()
            await self.release_stop.wait()
            async with self._endpoint.subscribe(ACTIONS_LANE):
                self.endpoint_touched.set()

    lane_contracts = MessageContractRegistry(CORE_LANE_CONTRACTS.values())
    substrate = MemoryLaneSubstrate(lane_contracts=lane_contracts)
    runtime = build_controller_runtime(
        config_source={},
        base_dir=Path.cwd(),
        controller_id="controller-main",
    )
    async with (
        Deckr(
            lane_contracts=lane_contracts,
            message_bus=substrate,
        ) as deckr,
        anyio.create_task_group() as tg,
    ):

        def open_endpoint(slot, *, session_id=None, metadata=None):
            return deckr.endpoint(
                endpoint_address(slot, "controller-main"),
                session_id=session_id,
                metadata=metadata,
            )

        context = ComponentContext(
            component_id="dev.deckr.controller",
            instance_id="main",
            runtime_name="dev.deckr.controller:main",
            manifest=ComponentManifest(
                component_id="dev.deckr.controller",
                consumes=("hardware_messages", "actions"),
                publishes=("hardware_messages", "actions"),
                endpoint_slots=("controller",),
            ),
            config={},
            endpoints={"controller": "controller-main"},
            base_dir=Path.cwd(),
            lanes=deckr.lanes,
            beacon=deckr.beacon,
            concord=deckr.concord,
            kv_bucket_for=deckr.kv_bucket,
            endpoint_for=open_endpoint,
        )
        service = ControllerRuntimeService(
            runtime_name="dev.deckr.controller:main",
            runtime=runtime,
            context=context,
            beacon=deckr.beacon,
            concord=deckr.concord,
        )
        await service.start(RunContext(tg=tg, stopping=anyio.Event()))
        assert service._endpoint is not None
        sentinel = BorrowedActionsEndpointComponent(service._endpoint)
        await service._component_manager.add_component(sentinel)
        await service._component_manager.wait_for_state(
            sentinel,
            ComponentState.RUNNING,
            timeout=1.0,
        )
        stop_returned = anyio.Event()

        async def stop_service_from_cancelled_scope() -> None:
            with anyio.CancelScope() as scope:
                scope.cancel()
                await service.stop()
            stop_returned.set()

        tg.start_soon(stop_service_from_cancelled_scope)
        with anyio.fail_after(1):
            await sentinel.stop_entered.wait()

        assert not stop_returned.is_set()
        assert service._endpoint is not None
        async with service._endpoint.subscribe(ACTIONS_LANE):
            pass

        sentinel.release_stop.set()
        with anyio.fail_after(1):
            await stop_returned.wait()

        assert sentinel.endpoint_touched.is_set()
        assert service._endpoint is None
        tg.cancel_scope.cancel()
