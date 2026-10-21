"""Private controller binding/action lifecycle service."""

from deckr.controller._bindings._action_lifecycle import ActionInstanceSnapshot
from deckr.controller._bindings._context import (
    ControlContext,
    PageCommandPort,
    RuntimeMessageSender,
)


def __getattr__(name):
    if name in {
        "BindingActionSnapshot",
        "BindingLeaseSnapshot",
        "ControlBindingService",
        "ControlContextSnapshot",
        "HeldInputSnapshot",
    }:
        from deckr.controller._bindings import _service

        return getattr(_service, name)
    raise AttributeError(name)


__all__ = [
    "ActionInstanceSnapshot",
    "BindingActionSnapshot",
    "BindingLeaseSnapshot",
    "ControlBindingService",
    "ControlContext",
    "ControlContextSnapshot",
    "HeldInputSnapshot",
    "PageCommandPort",
    "RuntimeMessageSender",
]
