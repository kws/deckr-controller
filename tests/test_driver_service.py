from __future__ import annotations

import anyio
import pytest
from deckr.core.component import BaseComponent, RunContext
from deckr.core.messaging import EventBus

from deckr.controller._driver_service import DriverService


class FakeDriverComponent(BaseComponent):
    async def start(self, ctx: RunContext) -> None:
        return

    async def stop(self) -> None:
        return


class FakeEntryPoint:
    def __init__(
        self,
        name: str,
        created: list[tuple[str, dict | None]],
        *,
        accepts_config: bool = False,
    ) -> None:
        self.name = name
        self._created = created
        self._accepts_config = accepts_config

    def load(self):
        def factory(event_bus: EventBus, config: dict | None = None):
            self._created.append((self.name, config))
            return FakeDriverComponent(name=f"{self.name}_component")

        if self._accepts_config:
            return factory

        def factory_without_config(event_bus: EventBus):
            return factory(event_bus, None)

        return factory_without_config


class FakeEntryPoints:
    def __init__(self, entry_points) -> None:
        self._entry_points = entry_points

    def select(self, *, group: str):
        assert group == "deckr.drivers"
        return list(self._entry_points)


@pytest.mark.asyncio
async def test_driver_service_only_loads_selected_drivers(monkeypatch):
    created: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        "deckr.controller._driver_service.entry_points",
        lambda: FakeEntryPoints(
            [
                FakeEntryPoint("elgato", created),
                FakeEntryPoint("virtual", created),
            ]
        ),
    )

    service = DriverService(driver_bus=EventBus(), enabled_drivers=["virtual"])

    async with anyio.create_task_group() as tg:
        await service.start(RunContext(tg=tg, stopping=anyio.Event()))
        assert created == [("virtual", None)]
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_driver_service_passes_driver_namespace_config_when_supported(monkeypatch):
    created: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        "deckr.controller._driver_service.entry_points",
        lambda: FakeEntryPoints(
            [
                FakeEntryPoint("mqtt", created, accepts_config=True),
            ]
        ),
    )

    service = DriverService(
        driver_bus=EventBus(),
        enabled_drivers=["mqtt"],
        driver_configs={"mqtt": {"broker": {"hostname": "mqtt.local"}}},
    )

    async with anyio.create_task_group() as tg:
        await service.start(RunContext(tg=tg, stopping=anyio.Event()))
        assert created == [("mqtt", {"broker": {"hostname": "mqtt.local"}})]
        tg.cancel_scope.cancel()
