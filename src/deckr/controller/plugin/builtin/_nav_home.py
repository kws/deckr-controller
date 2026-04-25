from deckr.python_plugin.events import KeyUp, WillAppear, WillDisappear
from deckr.python_plugin.interface import PluginAction, PluginContext


class NavHomeAction(PluginAction):
    uuid: str = "deckr.plugin.builtin.navhome"

    async def on_will_appear(self, event: WillAppear, context: PluginContext):
        settings = await context.get_settings()
        title = getattr(settings, "title", "Home")
        await context.set_title(title)

    async def on_will_disappear(self, event: WillDisappear, context: PluginContext):
        pass

    async def on_key_up(self, event: KeyUp, context: PluginContext):
        await context.set_page(profile="default", page=0)
