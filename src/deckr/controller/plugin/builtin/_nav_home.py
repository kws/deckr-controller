from deckr.pluginhost.messages import CapabilityInputEvent

from deckr.controller.plugin.builtin._context import BuiltInPluginContext


class NavHomeAction:
    uuid: str = "deckr.plugin.builtin.navhome"

    async def on_bind(self, context: BuiltInPluginContext) -> None:
        settings = await context.get_settings()
        title = getattr(settings, "title", "Home")
        await context.set_title(title)

    async def on_unbind(self, context: BuiltInPluginContext, reason: str) -> None:
        del context, reason

    async def on_input(
        self,
        context: BuiltInPluginContext,
        event: CapabilityInputEvent,
    ) -> None:
        if event.event_type in {"press", "up"}:
            await context.set_page(profile="default", page=0)
