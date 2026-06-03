"""In-process Deckr message bus and KV buckets for controller tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
from deckr.contracts.lanes import MessageContract, MessageContractRegistry
from deckr.contracts.messages import DeckrMessage, EndpointAddress
from deckr.contracts.models import DeckrModel
from deckr.lanes import (
    ReplyPredicate,
    message_is_deliverable,
    reply_is_accepted,
    validate_message_for_contract,
)
from deckr.substrates.nats_kv import (
    KvBucketPolicy,
    KvChange,
    KvConflict,
    KvEntry,
    kv_value,
)


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
                bucket=policy.bucket, buffer_size=self._buffer_size
            )
            self.kv_buckets[policy.bucket] = bucket
        return bucket


class MemoryJsonKvBucket:
    def __init__(self, *, bucket: str, buffer_size: int = 100) -> None:
        self.bucket = bucket
        self._buffer_size = buffer_size
        self._revision = 0
        self._entries: dict[str, KvEntry] = {}
        self._watchers: dict[anyio.abc.ObjectSendStream[KvChange | None], str] = {}
        self._lock = anyio.Lock()

    async def get(self, key: str) -> KvEntry | None:
        async with self._lock:
            return self._entries.get(key)

    async def items(self, prefix: str = "") -> tuple[KvEntry, ...]:
        async with self._lock:
            return tuple(
                entry
                for key, entry in sorted(self._entries.items())
                if key.startswith(prefix)
            )

    async def put(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        del ttl
        entry, watchers = await self._write(key, value)
        await self._publish(
            watchers, KvChange(self.bucket, key, entry.revision, "put", entry)
        )
        return entry

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        del ttl
        async with self._lock:
            if key in self._entries:
                raise KvConflict(f"KV key {key!r} already exists")
        return await self.put(key, value)

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry:
        del ttl
        async with self._lock:
            current = self._entries.get(key)
            if current is None or current.revision != revision:
                raise KvConflict(f"KV key {key!r} revision changed")
        return await self.put(key, value)

    async def delete(self, key: str, *, revision: int | None = None) -> int | None:
        async with self._lock:
            current = self._entries.get(key)
            if current is None:
                return None
            if revision is not None and current.revision != revision:
                raise KvConflict(f"KV key {key!r} revision changed")
            self._revision += 1
            self._entries.pop(key, None)
            delete_revision = self._revision
            watchers = self._watchers_for(key)
        await self._publish(
            watchers,
            KvChange(self.bucket, key, delete_revision, "delete"),
        )
        return delete_revision

    async def expire(self, key: str) -> None:
        async with self._lock:
            current = self._entries.pop(key, None)
            if current is None:
                return
            self._revision += 1
            expire_revision = self._revision
            watchers = self._watchers_for(key)
        await self._publish(
            watchers,
            KvChange(self.bucket, key, expire_revision, "expire"),
        )

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange | None]]:
        send, receive = anyio.create_memory_object_stream[KvChange | None](
            max_buffer_size=self._buffer_size
        )
        async with self._lock:
            self._watchers[send] = prefix
            snapshot = tuple(
                entry
                for key, entry in sorted(self._entries.items())
                if key.startswith(prefix)
            )
        for entry in snapshot:
            await send.send(
                KvChange(self.bucket, entry.key, entry.revision, "put", entry)
            )
        await send.send(None)
        try:
            async with send, receive:
                yield receive
        finally:
            async with self._lock:
                self._watchers.pop(send, None)

    async def _write(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
    ) -> tuple[KvEntry, tuple[anyio.abc.ObjectSendStream[KvChange | None], ...]]:
        normalized = kv_value(value)
        async with self._lock:
            self._revision += 1
            entry = KvEntry(self.bucket, key, normalized, self._revision)
            self._entries[key] = entry
            watchers = self._watchers_for(key)
        return entry, watchers

    def _watchers_for(
        self,
        key: str,
    ) -> tuple[anyio.abc.ObjectSendStream[KvChange | None], ...]:
        return tuple(
            stream
            for stream, prefix in self._watchers.items()
            if key.startswith(prefix)
        )

    async def _publish(
        self,
        watchers: tuple[anyio.abc.ObjectSendStream[KvChange | None], ...],
        change: KvChange,
    ) -> None:
        for watcher in watchers:
            await watcher.send(change)
