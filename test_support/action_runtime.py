from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
from deckr.action_runtime import (
    ACTION_RUNTIME_SERVICE_PROTOCOL,
    ACTION_RUNTIME_SERVICE_VIEW_STORE_NAME,
    ActionRuntimeAvailabilityViewPayload,
    action_availability_view_ref,
)
from deckr.actions.messages import ActionAvailabilityEntry, ActionDescriptor
from deckr.components import RunContext
from deckr.contracts.lanes import CORE_LANE_CONTRACTS, MessageContractRegistry
from deckr.core.kv import KvBucketPolicy
from deckr.runtime import Deckr
from deckr.services import ServiceViewStore

from deckr.controller._actions import ControllerActionService
from deckr.controller._controller_service import ControllerService
from deckr.controller._render import RenderResult
from deckr.controller.config import Control, ControlSelector, Page, Profile
from test_support.managed_hardware import (
    MemoryConfigService,
    _config,
    device_descriptor,
    eventually,
)


@asynccontextmanager
async def running_controller(substrate, *, with_output=False):
    contracts = MessageContractRegistry(CORE_LANE_CONTRACTS.values())
    descriptor = device_descriptor()
    if not with_output:
        descriptor = descriptor.model_copy(
            update={
                "controls": tuple(
                    control.model_copy(update={"output_capabilities": ()})
                    for control in descriptor.controls
                )
            }
        )
    config = _config(
        config_id="config-a", fingerprint=descriptor.fingerprint
    ).model_copy(
        update={
            "profiles": [
                Profile(
                    name="default",
                    pages=[
                        Page(
                            controls=[
                                Control(
                                    selector=ControlSelector(control_id="key-1"),
                                    action="action.test",
                                    provider_instance_id="test",
                                )
                            ]
                        )
                    ],
                )
            ],
        }
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=None)
    render = MagicMock()

    async def render_request(request):
        return RenderResult(
            context_id=request.context_id,
            binding_id=request.binding_id,
            control_id=request.control_id,
            generation=request.generation,
            frame=None,
        )

    render.render = AsyncMock(side_effect=render_request)
    commands = AsyncMock(return_value=True)
    render.aclose = AsyncMock()
    async with (
        Deckr(lane_contracts=contracts, message_bus=substrate) as runtime,
        runtime.endpoint("hardware_manager:manager-a") as hardware_endpoint,
        runtime.endpoint("controller:controller-main") as endpoint,
        runtime.endpoint("service:action-runtime.test") as provider_endpoint,
        runtime.hardware_manager(
            hardware_endpoint,
            manager_id="manager-a",
            devices={descriptor.device_id: descriptor},
            command_handler=commands,
        ) as hardware,
        runtime.services(endpoint) as services,
        runtime.service_provider(
            provider_endpoint,
            ACTION_RUNTIME_SERVICE_PROTOCOL,
            service_id="action-runtime.test",
            diagnostics={"providerId": "test.provider"},
        ) as provider,
        anyio.create_task_group() as tg,
    ):
        actions = ControllerActionService(
            controller_id="controller-main",
            controller_session_id=endpoint.session_id,
            manager=registry,
            services=services,
            start_soon=tg.start_soon,
        )
        controller = ControllerService(
            endpoint=endpoint,
            hardware=runtime.hardware(endpoint),
            config_service=MemoryConfigService(config),
            settings_service=None,
            controller_id="controller-main",
            action_registry=registry,
            action_service=actions,
            render_backend=render,
        )
        stopping = anyio.Event()
        store = ServiceViewStore(
            bucket=runtime.kv_bucket(
                KvBucketPolicy(
                    bucket=ACTION_RUNTIME_SERVICE_VIEW_STORE_NAME, ttl_seconds=None
                )
            )
        )
        store.start(tg)
        received = []

        async def receive():
            async for request in provider.requests():
                if request.authorization is not None:
                    received.append(request)

        tg.start_soon(receive)
        await controller.start(RunContext(tg=tg, stopping=stopping))
        try:
            state = await provider.wait_for_state(
                lambda s: any(
                    c.local_authority and c.validity.valid for c in s.contracts
                ),
                timeout_seconds=5,
            )
            agreement = next(
                c.contract
                for c in state.contracts
                if c.local_authority and c.validity.valid
            )
            channel = provider.contract(agreement)
            payload = ActionRuntimeAvailabilityViewPayload(
                providerInstanceId="test",
                providerId="test.provider",
                serviceId="action-runtime.test",
                serviceEndpoint=provider_endpoint.address,
                serviceSessionId=provider_endpoint.session_id,
                entries=(
                    ActionAvailabilityEntry(
                        actionId="action.test",
                        status="available",
                        descriptor=ActionDescriptor(
                            actionId="action.test",
                            requirements=(
                                {
                                    "name": "press",
                                    "preferences": [
                                        {"family": "dev.deckr.input.button"}
                                    ],
                                },
                                *(
                                    (
                                        {
                                            "name": "display",
                                            "preferences": [
                                                {"family": "dev.deckr.output.raster"}
                                            ],
                                        },
                                    )
                                    if with_output
                                    else ()
                                ),
                            ),
                        ),
                    ),
                ),
            )
            await channel.view_access(store).put(
                action_availability_view_ref("action-runtime.test"),
                payload.model_dump(by_alias=True, mode="json"),
            )
            with anyio.fail_after(5):
                while True:
                    device = await controller._controller_contexts.get("config-a")
                    if (
                        device is not None
                        and device.context_for_control("key-1") is not None
                    ):
                        break
                    await anyio.sleep(0.005)
            await eventually(
                lambda: any(
                    r.authorization.body.name == "binding_attached" for r in received
                )
            )
            yield SimpleNamespace(
                controller=controller,
                device=device,
                provider=provider,
                channel=channel,
                received=received,
                availability_store=store,
                availability_payload=payload,
                substrate=substrate,
                endpoint=endpoint,
                actions=actions,
                hardware=hardware,
                hardware_endpoint=hardware_endpoint,
                commands=commands,
            )
        finally:
            stopping.set()
            await controller.stop()
            await store.aclose()
            tg.cancel_scope.cancel()


def page_descriptor(page_id="dynamic"):
    return {
        "pageId": page_id,
        "bindings": [{"controlId": "key-1", "target": {"kind": "self"}}],
    }


def notifications(h, name):
    return [
        request.authorization.body.to_dict()["event"]
        for request in h.received
        if request.authorization.body.name == name
    ]


async def open_page(h):
    binding = h.device.context_for_control("key-1").metadata
    await h.channel.send(
        "open_page",
        {
            "target": {
                "kind": "binding",
                "bindingId": binding.binding_id,
                "generation": binding.output_generation,
            },
            "descriptor": page_descriptor(),
        },
    )
    await eventually(lambda: len(notifications(h, "page_session_opened")) == 1)
    return h.device.active_page_session()
