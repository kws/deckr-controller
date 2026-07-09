import anyio
from deckr.actions.messages import CapabilityInputEvent
from deckr.hardware.descriptors import CONTROL_ACTIVATION_EVENTS

from deckr.controller.action_provider.builtin._context import ControllerActionContext


class GoToPageAction:
    uuid: str = "dev.deckr.controller.builtin.action.go_to_page"

    async def run(self):
        while True:
            await anyio.sleep_forever()

    async def on_bind(self, context: ControllerActionContext) -> None:
        await context.set_title(context.settings.title)

    async def on_unbind(self, context: ControllerActionContext, reason: str) -> None:
        del context, reason

    async def on_input(
        self,
        context: ControllerActionContext,
        event: CapabilityInputEvent,
    ) -> None:
        if event.event_type not in CONTROL_ACTIVATION_EVENTS:
            return
        await context.set_page(
            profile=getattr(context.settings, "profile", "default"),
            page=getattr(context.settings, "page", 0),
        )
