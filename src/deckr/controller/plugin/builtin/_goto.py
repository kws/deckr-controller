import anyio
from deckr.pluginhost.messages import CapabilityInputEvent

from deckr.controller.plugin.builtin._context import BuiltInPluginContext


class GoToPageAction:
    uuid: str = "deckr.plugin.builtin.gotopage"

    async def run(self):
        while True:
            await anyio.sleep_forever()

    async def on_bind(self, context: BuiltInPluginContext) -> None:
        settings = await context.get_settings()
        await context.set_title(settings.title)

    async def on_unbind(self, context: BuiltInPluginContext, reason: str) -> None:
        del context, reason

    async def on_input(
        self,
        context: BuiltInPluginContext,
        event: CapabilityInputEvent,
    ) -> None:
        if event.event_type not in {"press", "up"}:
            return
        settings = await context.get_settings()
        await context.set_page(
            profile=getattr(settings, "profile", "default"),
            page=getattr(settings, "page", 0),
        )
