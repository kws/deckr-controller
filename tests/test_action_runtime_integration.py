"""Production service envelopes, managed authority, and controller page mutations."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.action_runtime import (
    ACTION_RUNTIME_SERVICE_PROTOCOL,
    ACTION_RUNTIME_SERVICE_VIEW_STORE_NAME,
    ActionRuntimeAvailabilityViewPayload,
    action_availability_view_ref,
)
from deckr.actions.messages import ActionAvailabilityEntry, ActionDescriptor
from deckr.components import RunContext
from deckr.contracts.lanes import CORE_LANE_CONTRACTS, MessageContractRegistry
from deckr.contracts.messages import entity_subject
from deckr.core.kv import KvBucketPolicy
from deckr.hardware import CapabilityRef
from deckr.hardware import messages as hw
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
    return h.device._bindings.active_page_session()


@pytest.mark.asyncio
async def test_page_targets_generations_and_lifecycle_over_managed_services(
    managed_substrate,
):
    async with running_controller(managed_substrate) as h:
        binding = h.device.context_for_control("key-1").metadata
        params = {
            "target": {
                "kind": "binding",
                "bindingId": binding.binding_id,
                "generation": binding.output_generation,
            },
            "descriptor": page_descriptor(),
        }
        # A context subject and an incorrect generation must not open anything.
        await h.channel.send(
            "open_page",
            params,
            subject=entity_subject("context", contextId=binding.context_id),
        )
        await h.channel.send(
            "open_page",
            {
                **params,
                "target": {
                    **params["target"],
                    "generation": binding.output_generation + 1,
                },
            },
        )
        session = await open_page(h)
        assert session.generation == 0
        assert len(notifications(h, "page_session_opened")) == 1
        assert (
            notifications(h, "page_session_opened")[0]["pageSession"]["generation"] == 0
        )
        target = {
            "kind": "page_session",
            "pageSessionId": session.page_session_id,
            "generation": 0,
        }
        await h.channel.send(
            "replace_page", {"target": target, "descriptor": page_descriptor()}
        )
        await eventually(lambda: len(notifications(h, "page_session_opened")) == 2)
        assert session.generation == 1
        await h.channel.send("close_page", {"target": target})
        await h.channel.send("close_page", {"target": {**target, "generation": 1}})
        await eventually(lambda: len(notifications(h, "page_session_closed")) == 1)
        assert (
            notifications(h, "page_session_closed")[0]["pageSession"]["generation"] == 1
        )
        assert h.device._bindings.active_page_session() is None


@pytest.mark.asyncio
async def test_page_session_can_open_a_successor_without_visible_opener(
    managed_substrate,
):
    async with running_controller(managed_substrate) as h:
        old = await open_page(h)
        await h.channel.send(
            "open_page",
            {
                "target": {
                    "kind": "page_session",
                    "pageSessionId": old.page_session_id,
                    "generation": 0,
                },
                "descriptor": page_descriptor("nested"),
            },
        )
        await eventually(lambda: len(notifications(h, "page_session_opened")) == 2)
        current = h.device._bindings.active_page_session()
        assert (
            current.page_id == "nested"
            and current.page_session_id != old.page_session_id
        )
        assert current.owner_contract == old.owner_contract
        assert len(notifications(h, "page_session_closed")) == 1


@pytest.mark.asyncio
async def test_page_commit_rechecks_service_authority_after_planning(
    monkeypatch, managed_substrate
):
    async with running_controller(managed_substrate) as h:
        session = await open_page(h)
        bindings = h.device._bindings
        entered, release = anyio.Event(), anyio.Event()
        original = bindings._build_page_plan

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(bindings, "_build_page_plan", delayed)
        await h.channel.send(
            "replace_page",
            {
                "target": {
                    "kind": "page_session",
                    "pageSessionId": session.page_session_id,
                    "generation": 0,
                },
                "descriptor": page_descriptor(),
            },
        )
        with anyio.fail_after(5):
            await entered.wait()
        authorization = h.received[-1].authorization
        await h.provider.cancel_authorized(
            authorization, reason="test authority loss during planning"
        )
        release.set()
        await eventually(lambda: bindings.active_page_session() is None)
        assert session.generation == 0
        assert len(notifications(h, "page_session_opened")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["sender_session", "recipient_session", "contract", "control", "binding"]
)
async def test_page_scope_rejections_do_not_mutate_retained_state(
    managed_substrate, invalid
):
    async with running_controller(managed_substrate) as h:
        binding = h.device.context_for_control("key-1").metadata
        # Obtain an ordinary managed envelope addressed to an unknown target.
        template = await h.channel.send(
            "open_page",
            {
                "target": {"kind": "binding", "bindingId": "unknown", "generation": 0},
                "descriptor": page_descriptor(),
            },
        )
        params = {
            "target": {
                "kind": "binding",
                "bindingId": binding.binding_id,
                "generation": binding.output_generation,
            },
            "descriptor": page_descriptor("unauthorized"),
        }
        updates = {"body": {**template.body, "params": params}}
        if invalid == "sender_session":
            updates["sender_session_id"] = "stale-provider-session"
        elif invalid == "recipient_session":
            updates["recipient_session_id"] = "stale-controller-session"
        elif invalid == "contract":
            updates["contract"] = template.contract.model_copy(
                update={"generation": template.contract.generation + 1}
            )
        elif invalid == "control":
            params["descriptor"]["bindings"][0]["controlId"] = "outside-device"
        else:
            params["target"]["bindingId"] = "another-binding"
        await h.substrate.publish(template.model_copy(update=updates))
        session = await open_page(h)
        assert session.page_id == "dynamic"
        assert len(notifications(h, "page_session_opened")) == 1


@pytest.mark.asyncio
async def test_queued_replacements_commit_once_and_preserve_page_identity(
    managed_substrate,
):
    async with running_controller(managed_substrate) as h:
        session = await open_page(h)
        target = {
            "kind": "page_session",
            "pageSessionId": session.page_session_id,
            "generation": 0,
        }
        await h.channel.send(
            "replace_page",
            {"target": target, "descriptor": page_descriptor("different-page-id")},
        )
        for _ in range(2):
            await h.channel.send(
                "replace_page", {"target": target, "descriptor": page_descriptor()}
            )
        await h.channel.send("close_page", {"target": {**target, "generation": 1}})
        await eventually(lambda: len(notifications(h, "page_session_closed")) == 1)
        assert len(notifications(h, "page_session_opened")) == 2
        assert session.generation == 1 and session.page_id == "dynamic"


@pytest.mark.asyncio
async def test_page_commit_rechecks_hardware_authority_after_planning(
    monkeypatch, managed_substrate
):
    async with running_controller(managed_substrate) as h:
        session = await open_page(h)
        bindings = h.device._bindings
        entered, release, finished = anyio.Event(), anyio.Event(), anyio.Event()
        original = bindings._build_page_plan

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            finished.set()
            return result

        monkeypatch.setattr(bindings, "_build_page_plan", delayed)
        await h.channel.send(
            "replace_page",
            {
                "target": {
                    "kind": "page_session",
                    "pageSessionId": session.page_session_id,
                    "generation": 0,
                },
                "descriptor": page_descriptor(),
            },
        )
        with anyio.fail_after(5):
            await entered.wait()
        await h.hardware.remove_device("device-a")
        await eventually(
            lambda: h.controller._hardware.claim_for_config("config-a") is None
        )
        release.set()
        with anyio.fail_after(5):
            await finished.wait()
            while await h.controller._controller_contexts.get("config-a") is not None:
                await anyio.sleep(0.005)
        assert session.generation == 0
        assert len(notifications(h, "page_session_opened")) == 1


@pytest.mark.asyncio
async def test_replace_page_requires_a_page_session_target(managed_substrate):
    async with running_controller(managed_substrate) as h:
        session = await open_page(h)
        binding = h.device.context_for_control("key-1").metadata
        await h.channel.send(
            "replace_page",
            {
                "target": {
                    "kind": "binding",
                    "bindingId": binding.binding_id,
                    "generation": binding.output_generation,
                },
                "descriptor": page_descriptor(),
            },
        )
        await h.channel.send(
            "replace_page",
            {
                "target": {
                    "kind": "page_session",
                    "pageSessionId": session.page_session_id,
                    "generation": 0,
                },
                "descriptor": page_descriptor(),
            },
        )
        await eventually(lambda: len(notifications(h, "page_session_opened")) == 2)
        assert session.generation == 1
        assert h.device._bindings.active_page_session() is session


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ("binding", "action_instance"))
async def test_lifecycle_events_keep_the_service_envelope(
    managed_substrate, target_kind
):
    async with running_controller(managed_substrate) as h:
        binding = h.device.context_for_control("key-1").metadata
        metadata = (
            binding.model_dump(by_alias=True, mode="json")
            if target_kind == "binding"
            else notifications(h, "action_instance_created")[0]["metadata"]
        )
        await h.channel.send(
            "action_lifecycle_rejected",
            event={
                "targetKind": target_kind,
                ("binding" if target_kind == "binding" else "actionInstance"): metadata,
                "reason": "permission_denied",
                "retryable": False,
            },
        )
        await eventually(lambda: h.device.context_for_control("key-1") is None)
        assert h.controller._hardware.route_for_config("config-a") is not None
        assert h.device._bindings.binding_by_id(binding.binding_id) is None


@pytest.mark.asyncio
async def test_stale_page_lifecycle_rejection_cannot_close_new_generation(
    managed_substrate,
):
    async with running_controller(managed_substrate) as h:
        session = await open_page(h)
        old_metadata = notifications(h, "page_session_opened")[0]["pageSession"]
        target = {
            "kind": "page_session",
            "pageSessionId": session.page_session_id,
            "generation": 0,
        }
        await h.channel.send(
            "replace_page", {"target": target, "descriptor": page_descriptor()}
        )
        await eventually(lambda: len(notifications(h, "page_session_opened")) == 2)
        await h.channel.send(
            "action_lifecycle_rejected",
            event={
                "targetKind": "page_session",
                "pageSession": old_metadata,
                "reason": "permission_denied",
                "retryable": False,
            },
        )
        await h.channel.send(
            "replace_page",
            {"target": {**target, "generation": 1}, "descriptor": page_descriptor()},
        )
        await eventually(lambda: len(notifications(h, "page_session_opened")) == 3)
        current_metadata = notifications(h, "page_session_opened")[-1]["pageSession"]
        await h.channel.send(
            "action_lifecycle_rejected",
            event={
                "targetKind": "page_session",
                "pageSession": current_metadata,
                "reason": "permission_denied",
                "retryable": False,
            },
        )
        await eventually(lambda: len(notifications(h, "page_session_closed")) == 1)
        assert (
            notifications(h, "page_session_closed")[0]["pageSession"]["generation"] == 2
        )


@pytest.mark.asyncio
async def test_hardware_input_and_provider_output_cross_production_boundaries(
    managed_substrate,
):
    async with running_controller(managed_substrate, with_output=True) as h:
        binding = h.device.context_for_control("key-1").metadata
        live = h.controller._hardware.route_for_config("config-a")
        capability = CapabilityRef(
            deviceRef=live.ref, controlId="key-1", capabilityId="input"
        )
        accepted = await h.hardware.handle_hardware_message(
            hw.hardware_message(
                sender=h.hardware.endpoint.address,
                sender_session_id=h.hardware.endpoint.session_id,
                recipient=h.endpoint.address,
                recipient_session_id=h.endpoint.session_id,
                contract=live.contract,
                message_type=hw.CONTROL_INPUT,
                subject=hw.hardware_subject_for_capability(capability),
                body=hw.ControlInputMessage(
                    deviceRef=live.ref,
                    controlId="key-1",
                    capabilityId="input",
                    eventType="down",
                    sequence=1,
                    value={"eventType": "down"},
                ),
            )
        )
        assert accepted
        await eventually(lambda: len(notifications(h, "capability_input")) == 1)
        assert (
            notifications(h, "capability_input")[0]["binding"]["bindingId"]
            == binding.binding_id
        )
        h.commands.reset_mock()
        output = {
            "binding": binding.model_dump(by_alias=True, mode="json"),
            "capability": capability.model_copy(
                update={"capability_id": "raster"}
            ).model_dump(by_alias=True, mode="json"),
            "commandType": "clear",
            "generation": binding.output_generation,
            "params": {},
        }
        await h.channel.send(
            "binding_output",
            {
                **output,
                "capability": {
                    **output["capability"],
                    "capabilityId": "outside-binding",
                },
            },
        )
        await h.channel.send("binding_output", output)
        await eventually(lambda: h.commands.await_count == 1)
        command = h.commands.await_args.args[0].message
        assert command.contract == live.contract
        assert command.body["commandType"] == "clear"
        assert command.body["capabilityId"] == "raster"
