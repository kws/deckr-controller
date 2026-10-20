"""Adapt an authorized service envelope to controller-local context routing."""

from deckr.actions.messages import action_body, context_subject
from deckr.contracts.messages import DeckrMessage


def contextualize_action_command(message: DeckrMessage) -> DeckrMessage:
    if message.subject.kind != "service":
        return message
    body = action_body(message)
    target = next(
        (
            target
            for name in ("binding", "action_instance", "page_session")
            if (target := getattr(body, name, None)) is not None
        ),
        None,
    )
    if target is None:
        # Page commands currently carry their target only in context subjects.
        return message
    return message.model_copy(
        update={
            "subject": context_subject(
                target.context_id,
                config_id=target.config_id,
                provider_instance_id=target.provider_instance_id,
                provider_id=target.provider_id,
                action_instance_id=target.action_instance_id,
                binding_id=getattr(target, "binding_id", None),
                page_session_id=getattr(target, "page_session_id", None),
            )
        }
    )
