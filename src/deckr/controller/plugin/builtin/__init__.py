"""Builtin actions: run in controller with privileged access."""

from typing import Protocol

from deckr.contracts.messages import (
    BUILTIN_ACTION_PROVIDER_ID,
    RESERVED_BUILTIN_PROVIDER_IDS,
)
from deckr.pluginhost.messages import ActionDescriptor, CapabilityInputEvent

from deckr.controller.plugin.builtin._context import BuiltInPluginContext
from deckr.controller.plugin.builtin._goto import GoToPageAction
from deckr.controller.plugin.builtin._nav_home import NavHomeAction

__all__ = [
    "BUILTIN_ACTION_PROVIDER_ID",
    "RESERVED_BUILTIN_PROVIDER_IDS",
    "BuiltinRegistry",
]


class BuiltinAction(Protocol):
    uuid: str

    async def on_bind(self, context: BuiltInPluginContext) -> None: ...
    async def on_unbind(self, context: BuiltInPluginContext, reason: str) -> None: ...
    async def on_input(
        self,
        context: BuiltInPluginContext,
        event: CapabilityInputEvent,
    ) -> None: ...


class BuiltinRegistry:
    """Registry of builtin actions. Resolved by controller before plugin hosts."""

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
            pluginId=getattr(action, "plugin_uuid", None),
        )
