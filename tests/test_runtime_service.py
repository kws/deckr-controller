from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from deckr.core.component import ComponentManager
from deckr.core.components import InactiveComponent, activate_components
from deckr.core.config import ConfigDocument
from deckr.core.messaging import EventBus

from deckr.controller._runtime_service import component


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


@pytest.mark.asyncio
async def test_controller_component_uses_shared_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.controller"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
        lambda component_id: component,
    )

    document = _document({"deckr": {"controller": {}}})
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result = await activate_components(document, component_manager)

        assert [created.name for created in result.components] == ["deckr.controller"]
        assert set(result.lane_names) == {"hardware_events", "plugin_messages"}
        assert isinstance(result.get_lane("hardware_events"), EventBus)
        assert isinstance(result.get_lane("plugin_messages"), EventBus)

        tg.cancel_scope.cancel()


def test_controller_component_can_be_disabled_explicitly() -> None:
    created = component.factory(
        type(
            "Context",
            (),
            {
                "raw_config": {"enabled": False},
                "runtime_name": "deckr.controller",
                "base_dir": Path.cwd(),
                "require_lane": lambda self, name: None,
            },
        )()
    )

    assert isinstance(created, InactiveComponent)
