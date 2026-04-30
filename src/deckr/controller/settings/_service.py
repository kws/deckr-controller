from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import anyio
from deckr.contracts.models import thaw_json

logger = logging.getLogger(__name__)

SettingsScope = Literal["action_instance"]


def _store_key(target: SettingsTarget) -> str:
    return f"controller={target.controller_id}|{target.as_key()}"


def _settings_copy(value: dict[str, Any]) -> dict[str, Any]:
    """Return a mutable JSON-shaped copy of settings-like data."""

    copied = thaw_json(value)
    return dict(copied) if isinstance(copied, dict) else {}


@dataclass(frozen=True, slots=True, kw_only=True)
class SettingsTarget:
    scope: SettingsScope
    controller_id: str
    config_id: str | None = None
    action_instance_id: str | None = None
    action_uuid: str | None = None
    plugin_uuid: str | None = None

    @classmethod
    def for_action_instance(
        cls,
        *,
        controller_id: str,
        config_id: str,
        action_instance_id: str,
        action_uuid: str,
        plugin_uuid: str | None = None,
    ) -> SettingsTarget:
        return cls(
            scope="action_instance",
            controller_id=controller_id,
            config_id=config_id,
            action_instance_id=action_instance_id,
            action_uuid=action_uuid,
            plugin_uuid=plugin_uuid,
        )

    def as_key(self) -> str:
        """Stable storage key for this target."""

        required = {
            "config_id": self.config_id,
            "action_instance_id": self.action_instance_id,
            "action_uuid": self.action_uuid,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing context settings fields: {', '.join(missing)}")

        parts = [
            f"config={self.config_id}",
            f"action_instance={self.action_instance_id}",
            f"action={self.action_uuid}",
        ]
        return "|".join(parts)


class SettingsService(Protocol):
    async def exists(self, target: SettingsTarget) -> bool: ...
    async def get(self, target: SettingsTarget) -> dict[str, Any]: ...
    async def merge(
        self, target: SettingsTarget, patch: dict[str, Any]
    ) -> dict[str, Any]: ...
    def subscribe(self, target: SettingsTarget) -> AsyncIterator[dict[str, Any]]: ...
    async def clear_config_targets(self, *, controller_id: str, config_id: str) -> int:
        ...


class InMemorySettingsService:
    """Test-friendly settings service with subscription support."""

    def __init__(self) -> None:
        self._values: dict[str, dict[str, Any]] = {}
        self._targets_by_key: dict[str, SettingsTarget] = {}
        self._subscribers: dict[
            str, set[anyio.abc.ObjectSendStream[dict[str, Any]]]
        ] = {}
        self._lock = anyio.Lock()

    async def exists(self, target: SettingsTarget) -> bool:
        async with self._lock:
            return _store_key(target) in self._values

    async def get(self, target: SettingsTarget) -> dict[str, Any]:
        async with self._lock:
            return _settings_copy(self._values.get(_store_key(target), {}))

    async def merge(
        self, target: SettingsTarget, patch: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            key = _store_key(target)
            current = _settings_copy(self._values.get(key, {}))
            current.update(_settings_copy(patch))
            self._values[key] = current
            self._targets_by_key[key] = target
            subscribers = set(self._subscribers.get(key, set()))
        await self._notify(subscribers, current)
        return _settings_copy(current)

    def subscribe(self, target: SettingsTarget) -> AsyncIterator[dict[str, Any]]:
        return self._subscribe_impl(target)

    async def clear_config_targets(self, *, controller_id: str, config_id: str) -> int:
        """Drop runtime overlays for a config so reloaded config values win."""

        async with self._lock:
            to_remove = [
                key
                for key, target in self._targets_by_key.items()
                if target.scope == "action_instance"
                and target.controller_id == controller_id
                and target.config_id == config_id
            ]
            for key in to_remove:
                self._values.pop(key, None)
                self._targets_by_key.pop(key, None)
        return len(to_remove)

    async def _subscribe_impl(
        self, target: SettingsTarget
    ) -> AsyncIterator[dict[str, Any]]:
        send, receive = anyio.create_memory_object_stream[dict[str, Any]](
            max_buffer_size=32
        )
        key = _store_key(target)
        async with self._lock:
            if key not in self._subscribers:
                self._subscribers[key] = set()
            self._subscribers[key].add(send)
            current = _settings_copy(self._values.get(key, {}))
        send.send_nowait(current)

        try:
            async for value in receive:
                yield _settings_copy(value)
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(key)
                if subscribers is not None:
                    subscribers.discard(send)
                    if not subscribers:
                        self._subscribers.pop(key, None)
            await send.aclose()

    async def _notify(
        self,
        subscribers: set[anyio.abc.ObjectSendStream[dict[str, Any]]],
        snapshot: dict[str, Any],
    ) -> None:
        for send in subscribers:
            try:
                await send.send(_settings_copy(snapshot))
            except Exception:
                logger.exception("Failed to notify settings subscriber")
