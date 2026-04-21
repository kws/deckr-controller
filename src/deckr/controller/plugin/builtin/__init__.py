"""Builtin actions: run in controller with privileged access."""

from deckr.plugin.interface import PluginAction
from deckr.plugin.manifest import build_action_metadata

from deckr.controller.plugin.builtin._goto import GoToPageAction
from deckr.controller.plugin.builtin._nav_home import NavHomeAction


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

    def get_metadata(self, uuid: str) -> dict | None:
        """Return manifest_defaults for an action."""
        action = self._actions.get(uuid)
        if action is None:
            return None
        meta = build_action_metadata(action)
        return {"manifest_defaults": meta["manifestDefaults"]}
