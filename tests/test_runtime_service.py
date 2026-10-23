from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
from deckr.contracts.messages import SERVICES_LANE, endpoint_address
from deckr.core.config import ConfigDocument
from deckr.lanes import Lane
from deckr.runtime import Deckr

from deckr.controller._config_document import ControllerRuntimeConfig
from deckr.controller._render_observation import ObservingRenderBackend
from deckr.controller._render_workers import (
    ProcessPoolRenderBackend,
    ThreadRenderBackend,
)
from deckr.controller._runtime_service import (
    ControllerRuntimeService,
    _build_render_backend,
    build_controller_runtime,
    component,
    component_factory,
)
from test_support.memory_lane_substrate import MemoryLaneSubstrate


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("startup_failure", ["hardware", "warmup"])
async def test_startup_failure_closes_resources_not_yet_owned_by_controller(
    monkeypatch, close_fails, startup_failure
):
    import deckr.controller._runtime_service as module

    closed = []
    prepared = []

    async def warmup():
        if startup_failure == "warmup":
            raise ValueError("renderer preparation failed")
        prepared.append(True)

    monkeypatch.setattr(module, "prepare_thread_rendering", warmup)

    def close_resource(name, *, fail=False):
        async def close(*args):
            closed.append(name)
            if fail:
                raise RuntimeError(f"close {name}")

        return close

    endpoint = SimpleNamespace(session_id="controller-session")
    endpoint_context = SimpleNamespace(
        __aenter__=AsyncMock(return_value=endpoint),
        __aexit__=close_resource("endpoint"),
    )

    def unavailable_hardware(_endpoint):
        assert prepared == [True]
        raise ValueError("hardware construction failed")

    context = SimpleNamespace(
        open_endpoint=lambda *args, **kwargs: endpoint_context,
        kv_bucket=lambda policy: None,
        hardware=unavailable_hardware,
    )
    actions = SimpleNamespace(aclose=close_resource("actions", fail=close_fails))
    renderer = SimpleNamespace(aclose=close_resource("renderer"))
    monkeypatch.setattr(module, "build_config_service", lambda *a, **kw: object())
    monkeypatch.setattr(module, "build_settings_service", lambda *a, **kw: object())
    monkeypatch.setattr(module, "DeckrServices", lambda **kw: object())
    monkeypatch.setattr(module, "ControllerActionService", lambda **kw: actions)
    monkeypatch.setattr(module, "_build_render_backend", lambda *a, **kw: renderer)
    service = ControllerRuntimeService(
        runtime_name="test-startup",
        runtime=build_controller_runtime(
            config_source={"render": {"backend": "thread"}},
            base_dir=Path.cwd(), controller_id="controller-main"
        ),
        context=context,
        beacon=object(),
        concord=object(),
    )
    service._component_manager = SimpleNamespace(
        run=object(),
        add_component=AsyncMock(),
        stop=close_resource("components"),
    )
    ctx = RunContext(
        tg=SimpleNamespace(start=AsyncMock(), start_soon=object()),
        stopping=anyio.Event(),
    )
    failure_message = (
        "renderer preparation failed"
        if startup_failure == "warmup"
        else "hardware construction failed"
    )
    if close_fails:
        with pytest.raises(ExceptionGroup) as error:
            await service.start(ctx)
        assert [str(exc) for exc in error.value.exceptions] == [
            failure_message, "close actions"
        ]
    else:
        with pytest.raises(ValueError, match=failure_message):
            await service.start(ctx)
    assert closed == ["components", "renderer", "actions", "endpoint"]
    assert service._endpoint is None
    assert service._endpoint_cm is None


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
        ControllerRuntimeConfig.model_validate({"render": {"backend": "process_pool"}}),
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
    assert context.lanes == ["hardware_messages", "services"]
    assert context.bucket_policies == []
    assert service._materialized_config_bucket is None


def test_component_factory_creates_materialized_config_bucket(tmp_path: Path) -> None:
    context = _FakeComponentContext(
        {"device_config": {"materialized": {"bucket": "controller_config"}}},
        base_dir=tmp_path,
    )

    service = component_factory(context)

    assert isinstance(service, ControllerRuntimeService)
    assert context.lanes == ["hardware_messages", "services"]
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
        assert set(result.lane_names) == {"hardware_messages", "services"}
        assert isinstance(result.get_lane("hardware_messages"), Lane)
        assert isinstance(result.get_lane("services"), Lane)


@pytest.mark.asyncio
async def test_controller_runtime_keeps_controller_endpoint_open_until_children_stop() -> (
    None
):
    class BorrowedEndpointComponent(BaseComponent):
        def __init__(self, endpoint) -> None:
            super().__init__(name="borrowed-endpoint")
            self._endpoint = endpoint
            self.stop_entered = anyio.Event()
            self.release_stop = anyio.Event()
            self.endpoint_touched = anyio.Event()

        async def start(self, ctx: RunContext) -> None:
            del ctx

        async def stop(self) -> None:
            self.stop_entered.set()
            await self.release_stop.wait()
            async with self._endpoint.subscribe(SERVICES_LANE):
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
                consumes=("hardware_messages", "services"),
                publishes=("hardware_messages", "services"),
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
            hardware_for=deckr.hardware,
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
        sentinel = BorrowedEndpointComponent(service._endpoint)
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
        async with service._endpoint.subscribe(SERVICES_LANE):
            pass

        sentinel.release_stop.set()
        with anyio.fail_after(1):
            await stop_returned.wait()

        assert sentinel.endpoint_touched.is_set()
        assert service._endpoint is None
        tg.cancel_scope.cancel()
