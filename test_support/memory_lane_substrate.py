"""In-process Deckr message bus and KV buckets for controller tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from deckr.contracts.lanes import MessageContract, MessageContractRegistry
from deckr.contracts.messages import DeckrMessage, EndpointAddress
from deckr.core.kv import KvBucketPolicy
from deckr.lanes import (
    ReplyPredicate,
    message_is_deliverable,
    reply_is_accepted,
    validate_message_for_contract,
)
from deckr.testing import MemoryJsonKvBucket as MemoryJsonKvBucket


class MemoryLaneSubstrate:
    def __init__(
        self,
        *,
        lane_contracts: MessageContractRegistry,
        buffer_size: int = 100,
    ) -> None:
        self._lane_contracts = lane_contracts
        self._buffer_size = buffer_size
        self._lock = anyio.Lock()
        self._subscribers: dict[
            tuple[str, EndpointAddress, str],
            set[anyio.abc.ObjectSendStream[DeckrMessage]],
        ] = {}
        self.kv_buckets: dict[str, MemoryJsonKvBucket] = {}

    def contract_for(self, lane: str) -> MessageContract:
        return self._lane_contracts.contract_for(lane)

    async def connect(self) -> None:
        return

    def start(self, _tg: anyio.abc.TaskGroup) -> None:
        return

    async def aclose(self) -> None:
        return

    async def publish(self, message: DeckrMessage) -> None:
        contract = self.contract_for(message.lane)
        validate_message_for_contract(message, contract)
        async with self._lock:
            subscribers = [
                (endpoint, endpoint_session_id, tuple(streams))
                for (
                    lane,
                    endpoint,
                    endpoint_session_id,
                ), streams in self._subscribers.items()
                if lane == message.lane
            ]
        for endpoint, endpoint_session_id, streams in subscribers:
            if not message_is_deliverable(
                message,
                endpoint=endpoint,
                endpoint_session_id=endpoint_session_id,
                contract=contract,
            ):
                continue
            for stream in streams:
                await stream.send(message)

    async def publish_reply(
        self,
        message: DeckrMessage,
        *,
        request: DeckrMessage,
    ) -> None:
        del request
        await self.publish(message)

    async def request(
        self,
        message: DeckrMessage,
        *,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
    ) -> DeckrMessage:
        async with self.subscribe(
            message.lane,
            message.sender,
            endpoint_session_id=message.sender_session_id,
        ) as stream:
            await self.publish(message)
            with anyio.fail_after(timeout):
                while True:
                    reply = await stream.receive()
                    if await reply_is_accepted(reply, request=message, accept=accept):
                        return reply

    @asynccontextmanager
    async def subscribe(
        self,
        lane: str,
        endpoint: EndpointAddress,
        *,
        endpoint_session_id: str,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        send, receive = anyio.create_memory_object_stream[DeckrMessage](
            max_buffer_size=self._buffer_size
        )
        key = (lane, endpoint, endpoint_session_id)
        async with self._lock:
            self._subscribers.setdefault(key, set()).add(send)
        try:
            yield receive
        finally:
            async with self._lock:
                streams = self._subscribers.get(key)
                if streams is not None:
                    streams.discard(send)
                    if not streams:
                        self._subscribers.pop(key, None)
            await send.aclose()
            await receive.aclose()

    def kv_bucket(self, policy: KvBucketPolicy) -> MemoryJsonKvBucket:
        bucket = self.kv_buckets.get(policy.bucket)
        if bucket is None:
            bucket = MemoryJsonKvBucket(
                bucket=policy.bucket,
                buffer_size=self._buffer_size,
                ttl_seconds=policy.ttl_seconds,
            )
            self.kv_buckets[policy.bucket] = bucket
        return bucket
