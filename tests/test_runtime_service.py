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

from deckr.controller._runtime_service import (
    ControllerRuntimeService,
    build_controller_runtime,
    component,
)
from test_support.memory_lane_substrate import MemoryLaneSubstrate


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


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


def test_controller_runtime_uses_endpoint_id_not_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROLLER_ID", "from-env")

    runtime = build_controller_runtime(
        config_source={},
        base_dir=Path.cwd(),
        controller_id="controller-main",
    )

    assert runtime.controller_id == "controller-main"


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
