from __future__ import annotations

import anyio
import pytest
from conftest import LaneHarness
from deckr.actions.endpoints import action_provider_address
from deckr.concord import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_MAINTENANCE_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    Concord,
    ContractValidityStatus,
    concord_participant_token_key,
)
from deckr.contracts.messages import controller_address
from deckr.substrates.nats_kv import KvChange

from deckr.controller._action_provider_sessions import (
    ActionProviderSessionManager,
    provider_session_key,
)
from deckr.controller.action_provider.provider import ActionMetadata

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)
PROVIDER_INSTANCE_ID = "python"
PROVIDER_ID = "test.provider"
PROVIDER_SESSION_ID = "provider-session"


def _actions_bus() -> LaneHarness:
    return LaneHarness("actions", default_endpoint=CONTROLLER_ADDR)


def _concord(bus: LaneHarness) -> Concord:
    return Concord(
        bus.substrate.kv_bucket(CONCORD_CONTRACT_BUCKET_POLICY),
        bus.substrate.kv_bucket(CONCORD_TOKEN_BUCKET_POLICY),
        bus.substrate.kv_bucket(CONCORD_MAINTENANCE_BUCKET_POLICY),
    )


def _manager(concord: Concord, bus: LaneHarness) -> ActionProviderSessionManager:
    return ActionProviderSessionManager(
        controller_id=CONTROLLER_ID,
        controller_session_id=bus.session_id,
        concord=concord,
        start_soon=lambda fn, *a, **k: None,
    )


def _action(*, session_id: str = PROVIDER_SESSION_ID) -> ActionMetadata:
    return ActionMetadata(
        uuid="action.alpha",
        provider_instance_id=PROVIDER_INSTANCE_ID,
        provider_id=PROVIDER_ID,
        provider_session_id=session_id,
    )


@pytest.mark.asyncio
async def test_not_yet_fulfilled_provider_session_remains_nonterminal_pending() -> None:
    bus = _actions_bus()
    concord = _concord(bus)
    manager = _manager(concord, bus)
    action = _action()
    key = provider_session_key(action)
    assert key is not None

    snapshot = await manager.prepare(action)
    assert snapshot is not None
    assert snapshot.ready is False
    assert snapshot.terminal is False
    assert snapshot.status == ContractValidityStatus.NOT_YET_FULFILLED

    await anyio.sleep(0.06)
    refreshed = (await manager.refresh_many((key,)))[key]

    assert refreshed.ready is False
    assert refreshed.terminal is False
    assert refreshed.status == ContractValidityStatus.NOT_YET_FULFILLED
    assert refreshed.reason == str(action_provider_address(PROVIDER_INSTANCE_ID))


@pytest.mark.asyncio
async def test_terminal_provider_session_status_retires_session() -> None:
    bus = _actions_bus()
    concord = _concord(bus)
    manager = _manager(concord, bus)
    action = _action()
    key = provider_session_key(action)
    assert key is not None
    await manager.prepare(action)
    session = next(iter(manager._sessions.values()))

    assert await concord.cancel(
        session.contract,
        participant=CONTROLLER_ADDR,
        reason="provider_stopped",
    )
    snapshot = (await manager.refresh_many((key,)))[key]

    assert snapshot.ready is False
    assert snapshot.terminal is True
    assert snapshot.status == ContractValidityStatus.CANCELLED
    assert snapshot.reason == "provider_stopped"
    assert manager._sessions == {}

    retired = await manager.prepare(action)
    assert retired is not None
    assert retired.terminal is True
    assert retired.reason == "provider_session_retired"


@pytest.mark.asyncio
async def test_provider_session_ready_after_concord_token_cache_rebuild() -> None:
    bus = _actions_bus()
    concord = _concord(bus)
    manager = _manager(concord, bus)
    action = _action()
    key = provider_session_key(action)
    assert key is not None

    async with anyio.create_task_group() as tg:
        concord.start(tg)
        pending = await manager.prepare(action)
        assert pending is not None
        assert pending.ready is False
        session = manager._sessions[key]

        provider = action_provider_address(PROVIDER_INSTANCE_ID)
        provider_token = await concord._attach(  # noqa: SLF001
            session.contract,
            provider,
            PROVIDER_SESSION_ID,
            token_id="provider-token",
        )
        await concord.wait_current()
        token_generation = concord._coordinator._token_bucket.generation  # noqa: SLF001

        async with concord._lock:  # noqa: SLF001
            concord._clear_indexes_locked()  # noqa: SLF001
            concord._contract_bucket_generation = (  # noqa: SLF001
                concord._coordinator._contract_bucket.generation  # noqa: SLF001
            )
            concord._token_bucket_generation = 0  # noqa: SLF001
            concord._maintenance_bucket_generation = (  # noqa: SLF001
                concord._maintenance_bucket.generation  # noqa: SLF001
            )

        await concord._apply_token_change(  # noqa: SLF001
            KvChange(
                concord.token_bucket,
                concord_participant_token_key(
                    contract_id=session.contract.contract_id,
                    generation=session.contract.generation,
                    participant=provider,
                ),
                provider_token.revision,
                "put",
                view_generation=token_generation,
            )
        )
        snapshot = (await manager.refresh_many((key,)))[key]

        assert snapshot.ready is True
        assert snapshot.terminal is False
        assert snapshot.status == ContractValidityStatus.VALID
        tg.cancel_scope.cancel()
