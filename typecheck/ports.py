"""Static witnesses for the public application ports used by composition."""

from deckr.controller._actions import BuiltinActionCatalog, ControllerActionService
from deckr.controller._bindings._context import RuntimeMessageSender
from deckr.controller._bindings._ports import BindingActionService
from deckr.controller._device_session import DeviceSession
from deckr.controller.action_provider.builtin_registry import BuiltinActionRegistry


def action_ports(
    actions: ControllerActionService,
    registry: BuiltinActionRegistry,
    session: DeviceSession,
) -> tuple[BindingActionService, BuiltinActionCatalog, RuntimeMessageSender]:
    return actions, registry, session
