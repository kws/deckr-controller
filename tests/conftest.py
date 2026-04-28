"""Pytest configuration for deckr-controller package tests."""

import pytest
from deckr.contracts.lanes import CORE_LANE_CONTRACTS, LaneContractRegistry
from deckr.contracts.messages import (
    DeckrMessage,
    EndpointAddress,
    parse_endpoint_address,
)
from deckr.lanes import EndpointLane
from deckr.runtime import Deckr

from test_support.memory_lane_substrate import MemoryLaneSubstrate

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session")
def anyio_backend():
    """Use anyio as the async backend for all tests."""
    return "anyio"


@pytest.fixture
def persistence_tmp_dir(tmp_path):
    """Legacy fixture name for tests that need an isolated scratch path."""

    yield tmp_path


class LaneHarness:
    """Small test helper backed by endpoint-bound Deckr lanes."""

    def __init__(self, lane_name: str, *, default_endpoint: str | EndpointAddress):
        lane_contracts = LaneContractRegistry(CORE_LANE_CONTRACTS.values())
        substrate = MemoryLaneSubstrate(lane_contracts=lane_contracts)
        self.deckr = Deckr(lane_contracts=lane_contracts, substrate=substrate)
        self.lane = self.deckr.lane(lane_name)
        self.default_endpoint = parse_endpoint_address(default_endpoint)

    def endpoint(self, endpoint: str | EndpointAddress | None = None) -> EndpointLane:
        return self.lane.endpoint(endpoint or self.default_endpoint)

    async def publish(self, message: DeckrMessage) -> DeckrMessage:
        return await self.lane.endpoint(message.sender).publish(message)

    async def send(self, message: DeckrMessage) -> DeckrMessage:
        return await self.publish(message)

    async def reply_to(self, request: DeckrMessage, **kwargs) -> DeckrMessage:
        sender = kwargs.pop("sender", self.default_endpoint)
        return await self.lane.endpoint(sender).reply_to(request, **kwargs)

    def subscribe(self, endpoint: str | EndpointAddress | None = None):
        return self.endpoint(endpoint).subscribe()
