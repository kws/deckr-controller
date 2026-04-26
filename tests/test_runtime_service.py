from __future__ import annotations

from pathlib import Path

import pytest
from deckr.components import (
    InactiveComponent,
    resolve_component_host_plan,
    start_components,
)
from deckr.core.config import ConfigDocument
from deckr.runtime import Deckr
from deckr.transports.bus import EventBus

from deckr.controller._runtime_service import build_controller_runtime, component


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


@pytest.mark.asyncio
async def test_controller_component_uses_shared_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.controller"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: component,
    )

    document = _document({"deckr": {"controller": {"id": "controller-main"}}})
    plan = resolve_component_host_plan(document)
    async with Deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
        route_expiry_interval=0.01,
    ) as deckr, start_components(deckr, plan) as result:
        assert [created.name for created in result.components] == [
            "deckr.controller"
        ]
        assert set(result.lane_names) == {"hardware_messages", "plugin_messages"}
        assert isinstance(result.get_lane("hardware_messages"), EventBus)
        assert isinstance(result.get_lane("plugin_messages"), EventBus)


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


def test_controller_runtime_requires_configured_id_even_when_env_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROLLER_ID", "from-env")

    with pytest.raises(ValueError, match=r"Set `\[deckr\.controller\]\.id`"):
        build_controller_runtime(
            raw_config={},
            base_dir=Path.cwd(),
        )
