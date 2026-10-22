"""Pytest configuration for deckr-controller package tests."""

import pytest
import pytest_asyncio
from deckr.contracts.lanes import (
    CORE_LANE_CONTRACTS,
    MessageContractRegistry,
)

from test_support.memory_lane_substrate import MemoryLaneSubstrate

pytest_plugins = (
    "pytest_asyncio",
    "test_support.binding_fixtures",
    "test_support.binding_builders",
)


@pytest.fixture(scope="session")
def anyio_backend():
    """Use anyio as the async backend for all tests."""
    return "anyio"


@pytest.fixture
def persistence_tmp_dir(tmp_path):
    """Legacy fixture name for tests that need an isolated scratch path."""

    yield tmp_path


@pytest_asyncio.fixture(params=("memory", "nats"))
async def managed_substrate(request, tmp_path):
    """Run managed controller integration against both supported test transports."""
    from deckr.bindings.nats import DeckrNatsSubstrate
    from deckr.substrates.nats_server import NatsServerSupervisor

    contracts = MessageContractRegistry(CORE_LANE_CONTRACTS.values())
    if request.param == "memory":
        yield MemoryLaneSubstrate(lane_contracts=contracts)
        return
    supervisor = NatsServerSupervisor(port=-1, runtime_dir=tmp_path)
    handle = await supervisor.start()
    try:
        yield DeckrNatsSubstrate(
            url=handle.url, auth_token=handle.auth_token, lane_contracts=contracts
        )
    finally:
        await supervisor.stop()
