from deckr.actions.messages import CapabilityInputEvent
from deckr.hardware.descriptors import CONTROL_ACTIVATION_EVENTS

from deckr.controller.action_provider.builtin._context import ControllerActionContext


class NavHomeAction:
    uuid: str = "dev.deckr.controller.builtin.action.nav_home"

    async def on_bind(self, context: ControllerActionContext) -> None:
        title = getattr(context.settings, "title", "Home")
        await context.set_title(title)

    async def on_unbind(self, context: ControllerActionContext, reason: str) -> None:
        del context, reason

    async def on_input(
        self,
        context: ControllerActionContext,
        event: CapabilityInputEvent,
    ) -> None:
        if event.event_type in CONTROL_ACTIVATION_EVENTS:
            await context.set_page(profile="default", page=0)
