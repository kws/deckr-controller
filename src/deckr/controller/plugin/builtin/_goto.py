import anyio
from deckr.plugin.events import KeyUp, WillAppear, WillDisappear
from deckr.plugin.interface import PluginAction, PluginContext


class GoToPageAction(PluginAction):
    uuid: str = "deckr.plugin.builtin.gotopage"

    async def run(self):
        while True:
            await anyio.sleep_forever()

    async def on_will_appear(self, event: WillAppear, context: PluginContext):
        settings = await context.get_settings()
        await context.set_title(settings.title)

    async def on_will_disappear(self, event: WillDisappear, context: PluginContext):
        pass

    async def on_key_up(self, event: KeyUp, context: PluginContext):
        settings = await context.get_settings()
        await context.switch_to_profile(
            profile=getattr(settings, "profile", "default"),
            page=getattr(settings, "page", 0),
        )
