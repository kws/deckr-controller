"""Pytest configuration for deckr-controller package tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from deckr.contracts.lanes import CORE_LANE_CONTRACTS, MessageContractRegistry
from deckr.contracts.messages import (
    DeckrMessage,
    EndpointAddress,
    parse_endpoint_address,
)
from deckr.lanes import EndpointSession, endpoint_session
from deckr.runtime import Deckr

from deckr.controller._endpoint_messages import send_with_endpoint_identity
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
        lane_contracts = MessageContractRegistry(CORE_LANE_CONTRACTS.values())
        substrate = MemoryLaneSubstrate(lane_contracts=lane_contracts)
        self.substrate = substrate
        self.deckr = Deckr(lane_contracts=lane_contracts, message_bus=substrate)
        self.lane = self.deckr.lane(lane_name)
        self.lane_name = lane_name
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
                lane_name=self.lane_name,
                message_bus=self.substrate,
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
        return await endpoint.publish(message)

    async def send(self, message: DeckrMessage | None = None, **kwargs) -> DeckrMessage:
        if message is not None:
            return await self.publish(message)
        return await self.endpoint(self.default_endpoint).send(**kwargs)

    async def reply_to(self, request: DeckrMessage, **kwargs) -> DeckrMessage:
        sender = kwargs.pop("sender", self.default_endpoint)
        return await self.endpoint(sender).reply_to(request, **kwargs)

    def subscribe(self, endpoint: str | EndpointAddress | None = None):
        return self.endpoint(endpoint).subscribe()


class EndpointHarness:
    """Endpoint-session handle for tests."""

    def __init__(
        self,
        *,
        lane_name: str,
        message_bus,
        endpoint: EndpointAddress,
        session_id: str,
    ) -> None:
        self._lane_name = lane_name
        self._session = endpoint_session(
            address=endpoint,
            session_id=session_id,
            metadata={"runtime": "test"},
            message_bus=message_bus,
        )

    @property
    def lane_name(self) -> str:
        return self._lane_name

    @property
    def endpoint(self) -> EndpointAddress:
        return self._session.address

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def session(self) -> EndpointSession:
        return self._session

    @property
    def address(self) -> EndpointAddress:
        return self._session.address

    async def send(self, **kwargs) -> DeckrMessage:
        return await self._session.send(**kwargs)

    async def publish(self, message: DeckrMessage) -> DeckrMessage:
        return await send_with_endpoint_identity(self._session, message)

    async def reply_to(self, request: DeckrMessage, **kwargs) -> DeckrMessage:
        return await self._session.reply_to(request, **kwargs)

    @asynccontextmanager
    async def subscribe(self, lane: str | None = None) -> AsyncIterator:
        async with self._session.subscribe(lane or self._lane_name) as stream:
            yield stream
