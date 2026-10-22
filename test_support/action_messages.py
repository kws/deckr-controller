"""Native Action Runtime service envelopes for controller tests."""

from deckr.action_runtime import (
    ACTION_RUNTIME_SERVICE_NAMESPACE,
    EVENT_MESSAGES,
    action_runtime_message_name,
    action_runtime_payload,
    action_runtime_service_id,
)
from deckr.actions.endpoints import parse_action_provider_address
from deckr.contracts.messages import entity_subject, service_address
from deckr.services import ServiceMessageBody
from deckr.services.messages import service_message


def runtime_message(
    *,
    sender,
    sender_session_id,
    recipient,
    message_type,
    body,
    contract,
    recipient_session_id="controller-session",
):
    provider = parse_action_provider_address(sender)
    service_id = action_runtime_service_id(provider) if provider else "invalid-runtime"
    name = action_runtime_message_name(message_type)
    params, event = action_runtime_payload(name, body)
    return service_message(
        sender=service_address(service_id) if provider else sender,
        sender_session_id=sender_session_id,
        recipient=recipient,
        recipient_session_id=recipient_session_id,
        contract=contract,
        subject=entity_subject(
            "service",
            serviceId=service_id,
            namespace=ACTION_RUNTIME_SERVICE_NAMESPACE,
            name=name,
        ),
        body=ServiceMessageBody(
            serviceNamespace=ACTION_RUNTIME_SERVICE_NAMESPACE,
            name=name,
            intent="event" if name in EVENT_MESSAGES else "command",
            exchangePattern="one_way",
            params=params,
            event=event,
        ),
    )
