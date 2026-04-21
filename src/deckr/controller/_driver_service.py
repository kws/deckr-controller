import logging
from collections.abc import Iterable
from importlib.metadata import entry_points

from deckr.core.component import BaseComponent, Component, ComponentManager, RunContext
from deckr.core.messaging import EventBus

logger = logging.getLogger(__name__)

DRIVER_ENTRYPOINT_GROUP = "deckr.drivers"


def available_driver_names() -> list[str]:
    return sorted(
        entry_point.name
        for entry_point in entry_points().select(group=DRIVER_ENTRYPOINT_GROUP)
    )


class DriverService(BaseComponent):
    def __init__(
        self,
        driver_bus: EventBus,
        *,
        enabled_drivers: Iterable[str] | None = None,
    ):
        super().__init__()
        self._driver_bus = driver_bus
        self._driver_manager = ComponentManager()
        self._enabled_drivers = (
            frozenset(enabled_drivers) if enabled_drivers is not None else None
        )

    async def start(self, ctx: RunContext):
        await self._driver_manager.start(ctx)

        eps = sorted(
            entry_points().select(group=DRIVER_ENTRYPOINT_GROUP),
            key=lambda entry_point: entry_point.name,
        )
        available = {entry_point.name for entry_point in eps}
        if self._enabled_drivers is not None:
            missing = sorted(self._enabled_drivers - available)
            for name in missing:
                logger.warning("Driver %s requested but not installed", name)

        for ep in eps:
            if (
                self._enabled_drivers is not None
                and ep.name not in self._enabled_drivers
            ):
                continue
            try:
                factory = ep.load()
            except Exception as e:
                logger.exception(f"Error loading driver {ep.name}: {e}", exc_info=True)
                continue

            try:
                driver = factory(event_bus=self._driver_bus)
            except Exception as e:
                logger.exception(f"Error creating driver {ep.name}: {e}", exc_info=True)
                continue

            if not isinstance(driver, Component):
                logger.error(f"Driver {ep.name} is not a Component")
                continue

            await self._driver_manager.add_component(driver)

    async def stop(self):
        await self._driver_manager.stop()
