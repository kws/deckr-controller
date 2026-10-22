"""Typed runtime commands retain their original service envelope through dispatch."""

from dataclasses import dataclass

from deckr.action_runtime import action_runtime_body_from_service_message
from deckr.actions.messages import (
    ActionInstanceMetadata,
    ActionLifecycleRejectedBody,
    ActionMessageBody,
    BindingMetadata,
    BindingOutputBody,
    BindingOverlayBody,
    BindingOverlayClearBody,
    PageSessionMetadata,
)
from deckr.contracts.messages import DeckrMessage
from deckr.services import ServiceMessageBody

CommandMetadata = BindingMetadata | ActionInstanceMetadata | PageSessionMetadata


@dataclass(frozen=True, slots=True)
class ActionRuntimeCommand:
    envelope: DeckrMessage
    name: str
    body: ActionMessageBody

    @property
    def metadata(self) -> CommandMetadata | None:
        if isinstance(
            self.body, (BindingOutputBody, BindingOverlayBody, BindingOverlayClearBody)
        ):
            return self.body.binding
        if isinstance(self.body, ActionLifecycleRejectedBody):
            return (
                self.body.binding or self.body.action_instance or self.body.page_session
            )
        return None


def decode_action_command(
    message: DeckrMessage, body: ServiceMessageBody | None = None
) -> ActionRuntimeCommand:
    if message.message_type != "serviceMessage" or message.lane != "services":
        raise ValueError("Expected an Action Runtime service envelope")
    body = body or ServiceMessageBody.from_wire(message.body)
    return ActionRuntimeCommand(
        message, body.name, action_runtime_body_from_service_message(body)
    )
