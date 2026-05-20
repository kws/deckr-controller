"""Pytest configuration for deckr-controller package tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from deckr.contracts.lanes import CORE_LANE_CONTRACTS, LaneContractRegistry
from deckr.contracts.messages import (
    DeckrMessage,
    EndpointAddress,
    parse_endpoint_address,
)
from deckr.lanes import RegisteredEndpointLane
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
        self._endpoints: dict[EndpointAddress, EndpointHarness] = {}

    def endpoint(
        self,
        endpoint: str | EndpointAddress | None = None,
    ) -> "EndpointHarness":
        parsed = parse_endpoint_address(endpoint or self.default_endpoint)
        handle = self._endpoints.get(parsed)
        if handle is None:
            handle = EndpointHarness(
                lane=self.lane,
                endpoint=parsed,
                session_id=f"{parsed.family.replace('_', '-')}-session",
            )
            self._endpoints[parsed] = handle
        return handle

    @property
    def session_id(self) -> str:
        return self.endpoint(self.default_endpoint).session_id

    async def publish(self, message: DeckrMessage) -> DeckrMessage:
        endpoint = self.endpoint(message.sender)
        stamped = DeckrMessage.from_dict(
            {
                **message.to_dict(),
                "senderSessionId": endpoint.session_id,
            }
        )
        return await endpoint.publish(stamped)

    async def send(self, message: DeckrMessage) -> DeckrMessage:
        return await self.publish(message)

    async def reply_to(self, request: DeckrMessage, **kwargs) -> DeckrMessage:
        sender = kwargs.pop("sender", self.default_endpoint)
        return await self.endpoint(sender).reply_to(request, **kwargs)

    def subscribe(self, endpoint: str | EndpointAddress | None = None):
        return self.endpoint(endpoint).subscribe()


class EndpointHarness:
    """Registered endpoint handle for tests without the renewal background task."""

    def __init__(
        self,
        *,
        lane,
        endpoint: EndpointAddress,
        session_id: str,
    ) -> None:
        self._registered = RegisteredEndpointLane(
            lane=lane,
            endpoint=endpoint,
            session_id=session_id,
            metadata={"runtime": "test"},
        )

    @property
    def lane(self):
        return self._registered.lane

    @property
    def endpoint(self) -> EndpointAddress:
        return self._registered.endpoint

    @property
    def session_id(self) -> str:
        return self._registered.session_id

    async def publish(self, message: DeckrMessage) -> DeckrMessage:
        return await self._registered.publish(message)

    async def reply_to(self, request: DeckrMessage, **kwargs) -> DeckrMessage:
        return await self._registered.reply_to(request, **kwargs)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator:
        async with self._registered.subscribe() as stream:
            yield stream
