from __future__ import annotations

from unittest.mock import MagicMock

import anyio
import pytest
from deckr.components import RunContext

from deckr.controller.action_provider.action_registry import ActionRegistry
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID

CONTROLLER_ID = "controller-main"
BUILTIN_ACTION_UUID = "dev.deckr.controller.builtin.action.go_to_page"
EXTERNAL_PROVIDER_INSTANCE_ID = "python-dev.deckr.clock"
EXTERNAL_ACTION_UUID = "dev.deckr.clock.action.time"


def _registry() -> ActionRegistry:
    return ActionRegistry(MagicMock(), controller_id=CONTROLLER_ID)


async def _start_registry(registry: ActionRegistry) -> None:
    await registry.start(
        RunContext(
            tg=MagicMock(),
            stopping=anyio.Event(),
        )
    )


@pytest.mark.asyncio
async def test_action_registry_loads_builtin_actions() -> None:
    registry = _registry()

    await _start_registry(registry)

    action = await registry.get_action(BUILTIN_ACTION_UUID)
    assert action is not None
    assert action.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
    descriptor = await registry.get_action_descriptor(BUILTIN_ACTION_UUID)
    assert descriptor is not None
    assert descriptor.requirements is not None
    assert descriptor.requirements[0].event_types == ("up", "press", "tap")


@pytest.mark.asyncio
async def test_action_registry_resolves_qualified_builtin_actions_only() -> None:
    registry = _registry()

    await _start_registry(registry)

    qualified = f"{BUILTIN_ACTION_PROVIDER_ID}::{BUILTIN_ACTION_UUID}"
    assert await registry.get_action(qualified) is not None
    assert (
        await registry.get_action(
            qualified,
            provider_instance_id=EXTERNAL_PROVIDER_INSTANCE_ID,
        )
        is None
    )
    assert (
        await registry.get_action(
            f"{EXTERNAL_PROVIDER_INSTANCE_ID}::{EXTERNAL_ACTION_UUID}",
        )
        is None
    )


@pytest.mark.asyncio
async def test_action_registry_does_not_resolve_external_actions_or_labels() -> None:
    registry = _registry()

    await _start_registry(registry)

    assert (
        await registry.get_action(
            EXTERNAL_ACTION_UUID,
            provider_instance_id=EXTERNAL_PROVIDER_INSTANCE_ID,
        )
        is None
    )
    assert (
        await registry.get_action(
            BUILTIN_ACTION_UUID,
            provider_labels={"room": "office"},
        )
        is None
    )


@pytest.mark.asyncio
async def test_action_registry_stop_clears_builtin_metadata() -> None:
    registry = _registry()

    await _start_registry(registry)
    assert await registry.get_action(BUILTIN_ACTION_UUID) is not None

    await registry.stop()

    assert await registry.get_action(BUILTIN_ACTION_UUID) is None
