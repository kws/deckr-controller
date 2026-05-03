from deckr.actions.messages import CapabilityInputEvent

from deckr.controller.action_provider.builtin._context import ControllerActionContext


class NavHomeAction:
    uuid: str = "deckr.controller.builtin.navhome"

    async def on_bind(self, context: ControllerActionContext) -> None:
        settings = await context.get_settings()
        title = getattr(settings, "title", "Home")
        await context.set_title(title)

    async def on_unbind(self, context: ControllerActionContext, reason: str) -> None:
        del context, reason

    async def on_input(
        self,
        context: ControllerActionContext,
        event: CapabilityInputEvent,
    ) -> None:
        if event.event_type in {"press", "up"}:
            await context.set_page(profile="default", page=0)
