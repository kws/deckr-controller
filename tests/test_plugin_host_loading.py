from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from deckr.core.backplane import DeckrBackplane
from deckr.core.component import BaseComponent, ComponentManager
from deckr.core.providers import ProviderNotConfigured

from deckr.controller._config_document import (
    ControllerRuntimeConfig,
    DeckrConfigDocument,
)
from deckr.controller._providers import activate_plugin_host_providers


class _DummyHost(BaseComponent):
    def __init__(self, name: str = "dummy-host") -> None:
        super().__init__(name=name)

    async def start(self, ctx) -> None:
        return

    async def stop(self) -> None:
        return


@pytest.mark.asyncio
async def test_activate_plugin_host_providers_suppresses_disabled_implicit_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_factory(*, context):
        captured["context"] = context
        raise ProviderNotConfigured("provider disabled")

    monkeypatch.setattr(
        "deckr.controller._providers.available_plugin_host_names",
        lambda: ["python"],
    )
    monkeypatch.setattr(
        "deckr.controller._providers.load_provider_factory",
        lambda group, provider_id: fake_factory,
    )

    document = DeckrConfigDocument(
        controller=ControllerRuntimeConfig(),
        raw={"deckr": {"plugin_hosts": {"python": {"enabled": False}}}},
        base_dir=Path.cwd(),
    )
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        hosts = await activate_plugin_host_providers(
            document,
            DeckrBackplane(),
            component_manager,
            controller_id="controller-main",
        )

        assert hosts == []
        assert component_manager.list_components() == []
        context = captured["context"]
        assert context.instance_id == "python"
        assert dict(context.raw_config) == {"enabled": False}

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_activate_plugin_host_providers_loads_provider_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_factory(*, context):
        captured["context"] = context
        return _DummyHost(name="demo-host")

    monkeypatch.setattr(
        "deckr.controller._providers.available_plugin_host_names",
        lambda: [],
    )
    monkeypatch.setattr(
        "deckr.controller._providers.load_provider_factory",
        lambda group, provider_id: fake_factory,
    )
    document = DeckrConfigDocument(
        controller=ControllerRuntimeConfig(),
        raw={
            "deckr": {
                "controller": {"log_level": "info"},
                "plugin_hosts": {"main": {"provider": "demo", "enabled": True}},
                "plugins": {"openhab": {"url": "http://openhab.local:8080"}},
            }
        },
        base_dir=Path("/tmp/deckr-test"),
    )
    component_manager = ComponentManager()
    backplane = DeckrBackplane()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        hosts = await activate_plugin_host_providers(
            document,
            backplane,
            component_manager,
            controller_id="controller-main",
        )

        assert len(hosts) == 1
        context = captured["context"]
        assert context.instance_id == "main"
        assert context.provider_id == "demo"
        assert context.controller_id == "controller-main"
        assert dict(context.raw_config) == {"provider": "demo", "enabled": True}
        assert context.document.children("deckr.plugins") == {
            "openhab": {"url": "http://openhab.local:8080"}
        }
        assert context.backplane is backplane
        assert isinstance(hosts[0], _DummyHost)

        tg.cancel_scope.cancel()
