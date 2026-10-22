"""Production service envelopes, managed authority, and controller page mutations."""

import anyio
import pytest
from deckr.contracts.messages import entity_subject
from deckr.hardware import CapabilityRef
from deckr.hardware import messages as hw

from test_support.action_runtime import (
    notifications,
    open_page,
    page_descriptor,
    running_controller,
)
from test_support.managed_hardware import (
    eventually,
)


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
        opening = await h.channel.send("open_page", params)
        await eventually(lambda: len(notifications(h, "page_session_opened")) == 1)
        session = h.device.active_page_session()
        opened_event = next(
            r.message for r in h.received
            if r.authorization.body.name == "page_session_opened"
        )
        assert opened_event.causation_id == opening.message_id
        assert opened_event.contract == opening.contract
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
        replacing = await h.channel.send(
            "replace_page", {"target": target, "descriptor": page_descriptor()}
        )
        await eventually(lambda: len(notifications(h, "page_session_opened")) == 2)
        opened_events = [
            r.message for r in h.received
            if r.authorization.body.name == "page_session_opened"
        ]
        assert opened_events[-1].causation_id == replacing.message_id
        assert session.generation == 1
        await h.channel.send("close_page", {"target": target})
        closing = await h.channel.send(
            "close_page", {"target": {**target, "generation": 1}}
        )
        await eventually(lambda: len(notifications(h, "page_session_closed")) == 1)
        closed_event = next(
            r.message for r in h.received
            if r.authorization.body.name == "page_session_closed"
        )
        assert closed_event.causation_id == closing.message_id
        assert closed_event.contract == closing.contract
        assert (
            notifications(h, "page_session_closed")[0]["pageSession"]["generation"] == 1
        )
        assert h.device.active_page_session() is None


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
        current = h.device.active_page_session()
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
        bindings = h.device
        entered, release = anyio.Event(), anyio.Event()
        original = bindings.components.planning._build_page_plan

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        from dataclasses import replace

        monkeypatch.setattr(
            bindings.components.navigation,
            "bindings",
            replace(bindings.components.navigation.bindings, build_plan=delayed),
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
        bindings = h.device
        entered, release, finished = anyio.Event(), anyio.Event(), anyio.Event()
        original = bindings.components.planning._build_page_plan

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            finished.set()
            return result

        from dataclasses import replace

        monkeypatch.setattr(
            bindings.components.navigation,
            "bindings",
            replace(bindings.components.navigation.bindings, build_plan=delayed),
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
        # The shared declaration now rejects this schema-invalid target before
        # publication, consistently with the other language cores.
        with pytest.raises(ValueError, match="Service payload does not match"):
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
        assert h.device.active_page_session() is session


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
        assert h.device.binding_by_id(binding.binding_id) is None


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
