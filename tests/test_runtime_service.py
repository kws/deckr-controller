from __future__ import annotations

from pathlib import Path

import pytest
from deckr.components import resolve_component_host_plan, start_components
from deckr.core.config import ConfigDocument
from deckr.lanes import Lane
from deckr.runtime import Deckr

from deckr.controller._runtime_service import build_controller_runtime, component
from test_support.memory_lane_substrate import MemoryLaneSubstrate


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


@pytest.mark.asyncio
async def test_controller_component_uses_shared_lanes(
) -> None:
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "controller_main": {
                            "component": "dev.deckr.controller",
                            "instance_id": "main",
                            "endpoints": {"controller": "controller-main"},
                        }
                    }
                }
            }
        }
    )
    plan = resolve_component_host_plan(
        document,
        definitions={"dev.deckr.controller": component},
    )
    substrate = MemoryLaneSubstrate(lane_contracts=plan.lane_contracts)
    async with Deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
        substrate=substrate,
    ) as deckr, start_components(deckr, plan) as result:
        assert [created.name for created in result.components] == [
            "dev.deckr.controller:main"
        ]
        assert set(result.lane_names) == {"hardware_messages", "actions", "services"}
        assert isinstance(result.get_lane("hardware_messages"), Lane)
        assert isinstance(result.get_lane("actions"), Lane)
        assert isinstance(result.get_lane("services"), Lane)


def test_controller_runtime_uses_endpoint_id_not_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROLLER_ID", "from-env")

    runtime = build_controller_runtime(
        config_source={},
        base_dir=Path.cwd(),
        controller_id="controller-main",
    )

    assert runtime.controller_id == "controller-main"
