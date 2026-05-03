import anyio
from deckr.actions.messages import CapabilityInputEvent
from deckr.hardware.descriptors import CONTROL_ACTIVATION_EVENTS

from deckr.controller.action_provider.builtin._context import ControllerActionContext


class GoToPageAction:
    uuid: str = "deckr.controller.builtin.gotopage"

    async def run(self):
        while True:
            await anyio.sleep_forever()

    async def on_bind(self, context: ControllerActionContext) -> None:
        settings = await context.get_settings()
        await context.set_title(settings.title)

    async def on_unbind(self, context: ControllerActionContext, reason: str) -> None:
        del context, reason

    async def on_input(
        self,
        context: ControllerActionContext,
        event: CapabilityInputEvent,
    ) -> None:
        if event.event_type not in CONTROL_ACTIVATION_EVENTS:
            return
        settings = await context.get_settings()
        await context.set_page(
            profile=getattr(settings, "profile", "default"),
            page=getattr(settings, "page", 0),
        )
