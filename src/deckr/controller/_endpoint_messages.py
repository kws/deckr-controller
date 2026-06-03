from __future__ import annotations

from deckr.contracts.messages import DeckrMessage
from deckr.contracts.models import thaw_json
from deckr.lanes import EndpointSession


async def send_with_endpoint_identity(
    endpoint: EndpointSession, message: DeckrMessage
) -> DeckrMessage:
    """Send a prebuilt message restamped with the endpoint's local identity."""

    return await endpoint.send(
        lane=message.lane,
        recipient=message.recipient,
        recipient_session_id=message.recipient_session_id,
        subject=message.subject,
        message_type=message.message_type,
        body=thaw_json(message.body),
        ttl_ms=message.ttl_ms,
        causation_id=message.causation_id,
        trace=message.trace,
    )
