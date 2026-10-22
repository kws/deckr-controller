from collections.abc import AsyncIterator

import anyio
from deckr.actions.messages import (
    CAPABILITY_INPUT,
    CLOSE_PAGE,
    OPEN_PAGE,
    REPLACE_PAGE,
    CapabilityInputBody,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage
from deckr.hardware import messages as hw_messages

from deckr.controller._bindings._lifecycle_state import ActionInstanceSnapshot
from deckr.controller._device_session import DeviceSession
from test_support.action_messages import runtime_message
from test_support.binding_builders import _contract_pointer_for_provider_session
from test_support.binding_constants import (
    CONTROLLER_ADDR,
    PROVIDER_ADDR,
    PROVIDER_SESSION_ID,
)


def _action_instance_body(metadata: ActionInstanceSnapshot) -> dict:
    return {
        "providerInstanceId": metadata.provider_instance_id,
        "providerId": metadata.provider_id,
        "actionId": metadata.action_id,
        "actionInstanceId": metadata.action_instance_id,
        "configId": metadata.config_id,
        "contextId": metadata.context_id,
    }


def _page_wire_message(
    message_type,
    payload,
    target,
    *,
    sender_session_id=PROVIDER_SESSION_ID,
    contract=None,
):
    from deckr.action_runtime import (
        ACTION_RUNTIME_SERVICE_NAMESPACE,
        action_runtime_message_name,
    )
    from deckr.contracts.messages import entity_subject
    from deckr.services import ServiceMessageBody
    from deckr.services.messages import service_message

    name = action_runtime_message_name(message_type)
    return service_message(
        sender="service:action-runtime.python",
        sender_session_id=sender_session_id,
        recipient=CONTROLLER_ADDR,
        recipient_session_id="controller-session",
        subject=entity_subject(
            "service",
            serviceId="action-runtime.python",
            namespace=ACTION_RUNTIME_SERVICE_NAMESPACE,
            name=name,
        ),
        contract=contract or _contract_pointer_for_provider_session(sender_session_id),
        body=ServiceMessageBody(
            serviceNamespace=ACTION_RUNTIME_SERVICE_NAMESPACE,
            name=name,
            intent="command",
            exchangePattern="one_way",
            params={**(payload or {}), "target": target},
        ),
    )


def _action_command(
    message_type: str,
    payload: dict | None = None,
    *,
    config_id: str = "test-device",
    context_id: str,
    action_instance_id: str,
    binding_id: str,
    page_session_id: str | None = None,
    contract: ContractPointer | None = None,
    generation: int = 0,
) -> DeckrMessage:
    if message_type in {OPEN_PAGE, REPLACE_PAGE, CLOSE_PAGE}:
        return _page_wire_message(
            message_type,
            payload,
            {"kind": "binding", "bindingId": binding_id, "generation": generation},
            contract=contract,
        )
    return runtime_message(
        sender=PROVIDER_ADDR,
        sender_session_id=PROVIDER_SESSION_ID,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload or {},
        contract=contract or _contract_pointer_for_provider_session(),
    )


async def _action_command_for_active_binding(
    manager: DeviceSession,
    message_type: str,
    payload: dict | None = None,
    *,
    control_id: str = "0,0",
    config_id: str = "test-device",
) -> DeckrMessage:
    ctx = manager.context_for_control(control_id)
    assert ctx is not None
    return _action_command(
        message_type,
        payload,
        config_id=config_id,
        context_id=ctx.id,
        action_instance_id=ctx.action_instance_id,
        binding_id=ctx.binding_id,
        page_session_id=ctx.page_session_id,
        generation=ctx.metadata.output_generation,
    )


def _action_instance_command(
    message_type: str,
    payload: dict,
    *,
    context_id: str,
    action_instance_id: str,
    config_id: str = "test-device",
    sender_session_id: str = PROVIDER_SESSION_ID,
    contract: ContractPointer | None = None,
) -> DeckrMessage:
    return runtime_message(
        sender=PROVIDER_ADDR,
        sender_session_id=sender_session_id,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload,
        contract=contract or _contract_pointer_for_provider_session(sender_session_id),
    )


def _page_session_command(
    message_type: str,
    payload: dict,
    *,
    session_id: str,
    context_id: str,
    action_instance_id: str,
    config_id: str = "test-device",
    sender_session_id: str = PROVIDER_SESSION_ID,
    contract: ContractPointer | None = None,
    generation: int = 0,
) -> DeckrMessage:
    if message_type not in {OPEN_PAGE, REPLACE_PAGE, CLOSE_PAGE}:
        return runtime_message(
            sender=PROVIDER_ADDR,
            sender_session_id=sender_session_id,
            recipient=CONTROLLER_ADDR,
            message_type=message_type,
            body=payload,
            contract=contract
            or _contract_pointer_for_provider_session(sender_session_id),
        )

    return _page_wire_message(
        message_type,
        payload,
        {"kind": "page_session", "pageSessionId": session_id, "generation": generation},
        sender_session_id=sender_session_id,
        contract=contract,
    )


def _hardware_input(
    control_id: str,
    event_type: str,
    *,
    capability_id: str = "button.momentary",
    sequence: int | None = None,
    device_id: str = "test-device",
) -> DeckrMessage:
    return hw_messages.control_input_message(
        manager_id="manager-main",
        sender_session_id="manager-session",
        device_id=device_id,
        control_id=control_id,
        capability_id=capability_id,
        event_type=event_type,
        sequence=sequence,
    )


async def _next_action_message(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 1.0,
) -> DeckrMessage:
    with anyio.fail_after(timeout):
        return await anext(stream)


async def _assert_no_action_message(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 0.1,
) -> None:
    with anyio.move_on_after(timeout) as scope:
        await anext(stream)
    assert scope.cancel_called


async def _drain_action_messages(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 0.05,
) -> None:
    while True:
        with anyio.move_on_after(timeout) as scope:
            await anext(stream)
        if scope.cancel_called:
            return


async def _collect_action_messages(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 0.05,
) -> list[DeckrMessage]:
    messages: list[DeckrMessage] = []
    while True:
        with anyio.move_on_after(timeout) as scope:
            message = await anext(stream)
        if scope.cancel_called:
            return messages
        messages.append(message)


async def _next_capability_input(
    stream: AsyncIterator[DeckrMessage],
) -> CapabilityInputBody:
    msg = await _next_action_message(stream)
    assert msg.message_type == CAPABILITY_INPUT
    return CapabilityInputBody.model_validate(msg.body)
