"""Builtin actions: run in controller with privileged access."""

from deckr.python_plugin.interface import PluginAction
from deckr.pluginhost.messages import ActionDescriptor

from deckr.controller.plugin.builtin._goto import GoToPageAction
from deckr.controller.plugin.builtin._nav_home import NavHomeAction

BUILTIN_ACTION_PROVIDER_ID = "deckr.controller.builtin"
LEGACY_BUILTIN_ACTION_PROVIDER_ID = "builtin"
RESERVED_BUILTIN_PROVIDER_IDS = frozenset(
    {
        BUILTIN_ACTION_PROVIDER_ID,
        LEGACY_BUILTIN_ACTION_PROVIDER_ID,
    }
)


class BuiltinRegistry:
    """Registry of builtin actions. Resolved by controller before plugin hosts."""

    def __init__(self):
        self._goto_page_action = GoToPageAction()
        self._nav_home_action = NavHomeAction()
        self._actions: dict[str, PluginAction] = {
            self._goto_page_action.uuid: self._goto_page_action,
            self._nav_home_action.uuid: self._nav_home_action,
        }

    def get_action(self, uuid: str) -> PluginAction | None:
        return self._actions.get(uuid)

    def provides_actions(self) -> list[str]:
        return list(self._actions.keys())

    def get_action_descriptor(self, uuid: str) -> ActionDescriptor | None:
        """Return action registration descriptor."""
        action = self._actions.get(uuid)
        if action is None:
            return None
        return ActionDescriptor(
            uuid=action.uuid,
            name=getattr(action, "name", None),
            plugin_uuid=getattr(action, "plugin_uuid", None),
        )
