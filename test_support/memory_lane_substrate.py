"""In-process lane substrate for controller unit tests (no NATS server).

Deckr defaults to ``NatsSubstrate``; controller tests need deterministic,
connected-free messaging and KV behavior. This module vendors the former
``LocalSubstrate`` / ``LocalStateStore`` pair from Deckr with local expiry
helpers that were removed from ``deckr.state`` when NATS became the only
product substrate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from deckr.contracts.lanes import LaneContractRegistry
from deckr.contracts.messages import DeckrMessage, EndpointAddress
from deckr.contracts.models import DeckrModel
from deckr.lanes import (
    ReplyPredicate,
    message_is_deliverable,
    reply_is_accepted,
    validate_message_for_contract,
)
from deckr.state import (
    DEFAULT_STATE_STORE_NAME,
    StateChange,
    StateConflict,
    StateEntry,
    StateStore,
    StateStorePolicy,
    state_value,
)


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp must be an ISO-8601 string or datetime")
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _state_expires_at(
    value: Mapping[str, Any],
    *,
    ttl: float | None = None,
    now: datetime | None = None,
) -> datetime | None:
    timestamp = value.get("timestamp")
    ttl_seconds = value.get("ttlSeconds", value.get("ttl_seconds"))
    payload_deadline: datetime | None = None
    if timestamp is not None and ttl_seconds is not None:
        parsed = _parse_timestamp(timestamp)
        try:
            seconds = float(ttl_seconds)
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            payload_deadline = parsed + timedelta(seconds=seconds)

    ttl_deadline = None
    if ttl is not None and ttl > 0:
        ttl_deadline = (now or datetime.now(UTC)) + timedelta(seconds=ttl)

    deadlines = [deadline for deadline in (payload_deadline, ttl_deadline) if deadline]
    if not deadlines:
        return None
    return min(deadlines)


class MemoryLaneSubstrate:
    def __init__(
        self,
        *,
        lane_contracts: LaneContractRegistry,
        buffer_size: int = 100,
        sweep_interval: float = 0.05,
        default_state_name: str = DEFAULT_STATE_STORE_NAME,
    ) -> None:
        self._lane_contracts = lane_contracts
        self.default_state_name = default_state_name
        self._buffer_size = buffer_size
        self._sweep_interval = sweep_interval
        self._lock = anyio.Lock()
        self._subscribers: dict[
            tuple[str, EndpointAddress, str],
            set[anyio.abc.ObjectSendStream[DeckrMessage]],
        ] = {}
        self._states: dict[str, MemoryStateStore] = {}

    def start(self, tg: anyio.abc.TaskGroup) -> None:
        tg.start_soon(self._sweep_state, name="deckr_memory_state_sweeper")

    async def publish(self, message: DeckrMessage) -> None:
        contract = self._lane_contracts.contract_for(message.lane)
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

    def state(
        self,
        name: str,
        *,
        policy: StateStorePolicy | None = None,
    ) -> StateStore:
        del policy
        store = self._states.get(name)
        if store is None:
            store = MemoryStateStore(name=name, buffer_size=self._buffer_size)
            self._states[name] = store
        return store

    async def _sweep_state(self) -> None:
        while True:
            await anyio.sleep(self._sweep_interval)
            for store in tuple(self._states.values()):
                await store.expire()


class MemoryStateStore:
    def __init__(self, *, name: str, buffer_size: int = 100) -> None:
        self.name = name
        self._buffer_size = buffer_size
        self._lock = anyio.Lock()
        self._revision = 0
        self._entries: dict[str, tuple[StateEntry, datetime | None]] = {}
        self._watchers: dict[
            anyio.abc.ObjectSendStream[StateChange], str
        ] = {}

    async def get(self, key: str) -> StateEntry | None:
        async with self._lock:
            item = self._entries.get(key)
            return item[0] if item is not None else None

    async def items(self, prefix: str = "") -> tuple[StateEntry, ...]:
        async with self._lock:
            return tuple(
                entry
                for key, (entry, _expires_at) in sorted(self._entries.items())
                if key.startswith(prefix)
            )

    async def put(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> StateEntry:
        normalized = state_value(value)
        async with self._lock:
            entry = self._next_entry(key, normalized)
            self._entries[key] = (entry, _state_expires_at(normalized, ttl=ttl))
            watchers = self._watchers_for(key)
        await self._publish(watchers, StateChange("put", key, entry))
        return entry

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> StateEntry:
        normalized = state_value(value)
        async with self._lock:
            if key in self._entries:
                raise StateConflict(f"State key {key!r} already exists")
            entry = self._next_entry(key, normalized)
            self._entries[key] = (entry, _state_expires_at(normalized, ttl=ttl))
            watchers = self._watchers_for(key)
        await self._publish(watchers, StateChange("put", key, entry))
        return entry

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> StateEntry:
        normalized = state_value(value)
        async with self._lock:
            current = self._entries.get(key)
            if current is None or current[0].revision != revision:
                raise StateConflict(f"State key {key!r} revision changed")
            entry = self._next_entry(key, normalized)
            self._entries[key] = (entry, _state_expires_at(normalized, ttl=ttl))
            watchers = self._watchers_for(key)
        await self._publish(watchers, StateChange("put", key, entry))
        return entry

    async def delete(self, key: str, *, revision: int | None = None) -> None:
        async with self._lock:
            current = self._entries.get(key)
            if current is None:
                return
            if revision is not None and current[0].revision != revision:
                raise StateConflict(f"State key {key!r} revision changed")
            self._entries.pop(key, None)
            watchers = self._watchers_for(key)
        await self._publish(watchers, StateChange("delete", key, None))

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[StateChange]]:
        send, receive = anyio.create_memory_object_stream[StateChange](
            max_buffer_size=self._buffer_size
        )
        async with self._lock:
            self._watchers[send] = prefix
            snapshot = tuple(
                entry
                for key, (entry, _expires_at) in sorted(self._entries.items())
                if key.startswith(prefix)
            )
        for entry in snapshot:
            await send.send(StateChange("put", entry.key, entry))
        try:
            yield receive
        finally:
            async with self._lock:
                self._watchers.pop(send, None)
            await send.aclose()
            await receive.aclose()

    async def expire(self) -> None:
        now = datetime.now(UTC)
        expired: list[tuple[str, tuple[anyio.abc.ObjectSendStream[StateChange], ...]]] = []
        async with self._lock:
            for key, (_entry, expires_at) in tuple(self._entries.items()):
                if expires_at is None or expires_at > now:
                    continue
                self._entries.pop(key, None)
                expired.append((key, self._watchers_for(key)))
        for key, watchers in expired:
            await self._publish(watchers, StateChange("expire", key, None))

    def _next_entry(self, key: str, value: Mapping[str, Any]) -> StateEntry:
        self._revision += 1
        return StateEntry(key=key, value=value, revision=self._revision)

    def _watchers_for(
        self, key: str
    ) -> tuple[anyio.abc.ObjectSendStream[StateChange], ...]:
        return tuple(
            stream for stream, prefix in self._watchers.items() if key.startswith(prefix)
        )

    async def _publish(
        self,
        watchers: tuple[anyio.abc.ObjectSendStream[StateChange], ...],
        change: StateChange,
    ) -> None:
        for watcher in watchers:
            await watcher.send(change)
