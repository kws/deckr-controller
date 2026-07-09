from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from deckr.actions.messages import CapabilityInputEvent
from deckr.hardware.descriptors import CapabilityRef, DeviceRef

from deckr.controller.action_provider.builtin._goto import GoToPageAction
from deckr.controller.action_provider.builtin._nav_home import NavHomeAction


def _event(event_type: str) -> CapabilityInputEvent:
    return CapabilityInputEvent(
        capability=CapabilityRef(
            deviceRef=DeviceRef(managerId="manager-a", deviceId="device-a"),
            controlId="key-1",
            capabilityId="button",
        ),
        eventType=event_type,
        occurredAt=datetime.now(UTC),
    )


def _context(settings=None):
    context = SimpleNamespace()
    context.settings = settings or SimpleNamespace()
    context.set_title = AsyncMock()
    context.set_page = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_go_to_page_action_ignores_non_activation_input() -> None:
    context = _context(SimpleNamespace(profile="deck", page=2))

    await GoToPageAction().on_input(context, _event("down"))

    context.set_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_go_to_page_action_sets_configured_page_on_activation() -> None:
    context = _context(SimpleNamespace(profile="deck", page=2))

    await GoToPageAction().on_input(context, _event("press"))

    context.set_page.assert_awaited_once_with(profile="deck", page=2)


@pytest.mark.asyncio
async def test_nav_home_action_ignores_non_activation_input() -> None:
    context = _context()

    await NavHomeAction().on_input(context, _event("down"))

    context.set_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_nav_home_action_sets_default_page_on_activation() -> None:
    context = _context()

    await NavHomeAction().on_input(context, _event("tap"))

    context.set_page.assert_awaited_once_with(profile="default", page=0)
