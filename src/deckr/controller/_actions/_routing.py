"""Read controller routing metadata without changing the service envelope."""

from deckr.actions.messages import action_body
from deckr.contracts.messages import DeckrMessage


def action_command_metadata(message: DeckrMessage):
    body = action_body(message)
    return next(
        (
            target
            for name in ("binding", "action_instance", "page_session")
            if (target := getattr(body, name, None)) is not None
        ),
        None,
    )
