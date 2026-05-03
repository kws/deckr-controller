"""Builtin actions: run in controller with privileged access."""

from typing import Protocol

from deckr.actions.endpoints import (
    BUILTIN_ACTION_PROVIDER_ID,
    RESERVED_BUILTIN_PROVIDER_IDS,
)
from deckr.actions.messages import (
    ActionDescriptor,
    CapabilityInputEvent,
    CapabilityRequirement,
    CapabilityRequirementSelector,
)
from deckr.hardware.descriptors import (
    CONTROL_ACTIVATION_EVENTS,
    DECKR_INPUT_BUTTON,
    DECKR_INPUT_TOUCH,
)

from deckr.controller.action_provider.builtin._context import ControllerActionContext
from deckr.controller.action_provider.builtin._goto import GoToPageAction
from deckr.controller.action_provider.builtin._nav_home import NavHomeAction

__all__ = [
    "BUILTIN_ACTION_PROVIDER_ID",
    "RESERVED_BUILTIN_PROVIDER_IDS",
    "BuiltinRegistry",
]


class BuiltinAction(Protocol):
    uuid: str

    async def on_bind(self, context: ControllerActionContext) -> None: ...
    async def on_unbind(self, context: ControllerActionContext, reason: str) -> None: ...
    async def on_input(
        self,
        context: ControllerActionContext,
        event: CapabilityInputEvent,
    ) -> None: ...


class BuiltinRegistry:
    """Registry of builtin actions. Resolved by controller before action provider catalogs."""

    def __init__(self):
        self._goto_page_action = GoToPageAction()
        self._nav_home_action = NavHomeAction()
        self._actions: dict[str, BuiltinAction] = {
            self._goto_page_action.uuid: self._goto_page_action,
            self._nav_home_action.uuid: self._nav_home_action,
        }

    def get_action(self, uuid: str) -> BuiltinAction | None:
        return self._actions.get(uuid)

    def provides_actions(self) -> list[str]:
        return list(self._actions.keys())

    def get_action_descriptor(self, uuid: str) -> ActionDescriptor | None:
        """Return action registration descriptor."""
        action = self._actions.get(uuid)
        if action is None:
            return None
        return ActionDescriptor(
            actionId=action.uuid,
            name=getattr(action, "name", None),
            providerId=getattr(action, "provider_id", BUILTIN_ACTION_PROVIDER_ID),
            requirements=(_activation_input_requirement(),),
        )


def _activation_input_requirement() -> CapabilityRequirement:
    return CapabilityRequirement(
        name="input",
        preferences=(
            CapabilityRequirementSelector(
                family=DECKR_INPUT_BUTTON,
                type="momentary",
                direction="input",
                eventTypes=("up",),
            ),
            CapabilityRequirementSelector(
                family=DECKR_INPUT_BUTTON,
                type="activation",
                direction="input",
                eventTypes=("press",),
            ),
            CapabilityRequirementSelector(
                family=DECKR_INPUT_TOUCH,
                type="gesture",
                direction="input",
                eventTypes=("tap",),
            ),
        ),
        eventTypes=CONTROL_ACTIVATION_EVENTS,
        views=("native",),
    )
