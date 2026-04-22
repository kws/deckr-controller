from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import anyio
from platformdirs import PlatformDirs
from tinydb import Query, TinyDB
from decouple import config as decouple_config

logger = logging.getLogger(__name__)

dirs = PlatformDirs("deckr", "deckr", version="1.0")

SettingsScope = Literal["context", "plugin_global"]


def resolve_default_settings_dir() -> Path:
    """Resolve the default settings storage directory from env."""

    return Path(
        decouple_config("DECKR_SETTINGS_DIR", default=dirs.user_data_dir)
    ).resolve()


def _safe_filename(value: str) -> str:
    return value.replace("/", "_").replace(":", "_")


def _store_key(target: "SettingsTarget") -> str:
    if target.scope == "plugin_global":
        return target.as_key()
    return f"controller={target.controller_id}|{target.as_key()}"


@dataclass(frozen=True, slots=True, kw_only=True)
class SettingsTarget:
    scope: SettingsScope
    controller_id: str
    device_id: str | None = None
    profile_id: str | None = None
    page_id: str | None = None
    slot_id: str | None = None
    action_uuid: str | None = None
    dynamic_page_uuid: str | None = None
    plugin_uuid: str | None = None
    legacy_context_id: str | None = None

    @classmethod
    def for_context(
        cls,
        *,
        controller_id: str,
        device_id: str,
        profile_id: str,
        page_id: str,
        slot_id: str,
        action_uuid: str,
        dynamic_page_uuid: str | None = None,
        plugin_uuid: str | None = None,
        legacy_context_id: str | None = None,
    ) -> "SettingsTarget":
        return cls(
            scope="context",
            controller_id=controller_id,
            device_id=device_id,
            profile_id=profile_id,
            page_id=page_id,
            slot_id=slot_id,
            action_uuid=action_uuid,
            dynamic_page_uuid=dynamic_page_uuid,
            plugin_uuid=plugin_uuid,
            legacy_context_id=legacy_context_id,
        )

    @classmethod
    def for_plugin_global(
        cls,
        *,
        controller_id: str,
        plugin_uuid: str,
    ) -> "SettingsTarget":
        return cls(
            scope="plugin_global",
            controller_id=controller_id,
            plugin_uuid=plugin_uuid,
        )

    def as_key(self) -> str:
        """Stable storage key for this target."""

        if self.scope == "plugin_global":
            if not self.plugin_uuid:
                raise ValueError("plugin_uuid is required for plugin-global settings")
            return f"controller={self.controller_id}|plugin={self.plugin_uuid}"

        required = {
            "device_id": self.device_id,
            "profile_id": self.profile_id,
            "page_id": self.page_id,
            "slot_id": self.slot_id,
            "action_uuid": self.action_uuid,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing context settings fields: {', '.join(missing)}")

        parts = [
            f"device={self.device_id}",
            f"profile={self.profile_id}",
            f"page={self.page_id}",
            f"slot={self.slot_id}",
            f"action={self.action_uuid}",
        ]
        if self.dynamic_page_uuid is not None:
            parts.append(f"dynamic_page={self.dynamic_page_uuid}")
        return "|".join(parts)


class SettingsService(Protocol):
    async def exists(self, target: SettingsTarget) -> bool: ...
    async def get(self, target: SettingsTarget) -> dict[str, Any]: ...
    async def merge(
        self, target: SettingsTarget, patch: dict[str, Any]
    ) -> dict[str, Any]: ...
    def subscribe(self, target: SettingsTarget) -> AsyncIterator[dict[str, Any]]: ...


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
            return dict(self._values.get(_store_key(target), {}))

    async def merge(
        self, target: SettingsTarget, patch: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            key = _store_key(target)
            current = dict(self._values.get(key, {}))
            current.update(dict(patch))
            self._values[key] = current
            self._targets_by_key[key] = target
            subscribers = set(self._subscribers.get(key, set()))
        await self._notify(subscribers, current)
        return dict(current)

    def subscribe(self, target: SettingsTarget) -> AsyncIterator[dict[str, Any]]:
        return self._subscribe_impl(target)

    async def prune_context_targets(
        self,
        *,
        controller_id: str,
        device_id: str,
        valid_keys: set[str],
    ) -> int:
        async with self._lock:
            to_remove = [
                key
                for key, target in self._targets_by_key.items()
                if target.scope == "context"
                and target.controller_id == controller_id
                and target.device_id == device_id
                and target.as_key() not in valid_keys
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
            current = dict(self._values.get(key, {}))
        send.send_nowait(current)

        try:
            async for value in receive:
                yield dict(value)
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
                await send.send(dict(snapshot))
            except Exception:
                logger.exception("Failed to notify settings subscriber")


class FileBackedSettingsService:
    """TinyDB-backed settings service with controller-friendly targets."""

    def __init__(self, settings_dir: Path | None = None) -> None:
        self._settings_dir = (
            settings_dir if settings_dir is not None else resolve_default_settings_dir()
        )
        self._settings_dir.mkdir(parents=True, exist_ok=True)
        self._db_by_path: dict[Path, TinyDB] = {}
        self._subscribers: dict[
            str, set[anyio.abc.ObjectSendStream[dict[str, Any]]]
        ] = {}
        self._lock = anyio.Lock()

    async def exists(self, target: SettingsTarget) -> bool:
        async with self._lock:
            db = self._db_for_path(self._db_path_for_target(target))
            row = db.get(self._row_query(target))
            if row is not None:
                return True
            if target.scope != "context" or not target.legacy_context_id:
                return False
            legacy = db.get(Query().key == target.legacy_context_id)
            return legacy is not None and legacy.get("kind") != "settings"

    async def get(self, target: SettingsTarget) -> dict[str, Any]:
        async with self._lock:
            return dict(self._read_value_locked(target))

    async def merge(
        self, target: SettingsTarget, patch: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            key = _store_key(target)
            current = self._read_value_locked(target)
            current.update(dict(patch))
            self._write_value_locked(target, current)
            subscribers = set(self._subscribers.get(key, set()))
        await self._notify(subscribers, current)
        return dict(current)

    def subscribe(self, target: SettingsTarget) -> AsyncIterator[dict[str, Any]]:
        return self._subscribe_impl(target)

    async def prune_context_targets(
        self,
        *,
        controller_id: str,
        device_id: str,
        valid_keys: set[str],
    ) -> int:
        path = self._db_path_for_context_device(device_id)
        if not path.exists():
            return 0

        async with self._lock:
            db = self._db_for_path(path)
            rows = db.search(
                (Query().kind == "settings")
                & (
                    (Query().controller_id == controller_id)
                    | (~Query().controller_id.exists())
                )
                & (Query().device_id == device_id)
            )
            stale_ids = [row.doc_id for row in rows if row.get("key") not in valid_keys]
            if not stale_ids:
                return 0
            removed = db.remove(doc_ids=stale_ids)
        return len(removed)

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
            current = dict(self._read_value_locked(target))
        send.send_nowait(current)

        try:
            async for value in receive:
                yield dict(value)
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(key)
                if subscribers is not None:
                    subscribers.discard(send)
                    if not subscribers:
                        self._subscribers.pop(key, None)
            await send.aclose()

    def _db_path_for_target(self, target: SettingsTarget) -> Path:
        if target.scope == "plugin_global":
            return self._settings_dir / f"{_safe_filename(target.controller_id)}.globals.json"
        assert target.device_id is not None
        return self._db_path_for_context_device(target.device_id)

    def _db_path_for_context_device(self, device_id: str) -> Path:
        return self._settings_dir / f"{_safe_filename(device_id)}.json"

    def _db_for_path(self, path: Path) -> TinyDB:
        db = self._db_by_path.get(path)
        if db is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            db = TinyDB(path)
            self._db_by_path[path] = db
        return db

    def _read_value_locked(self, target: SettingsTarget) -> dict[str, Any]:
        db = self._db_for_path(self._db_path_for_target(target))
        row = db.get(self._row_query(target))
        if row is not None:
            value = row.get("value")
            if isinstance(value, dict):
                return dict(value)
            return {}

        if target.scope == "context":
            return self._migrate_legacy_context_value_locked(db, target)
        return {}

    def _migrate_legacy_context_value_locked(
        self,
        db: TinyDB,
        target: SettingsTarget,
    ) -> dict[str, Any]:
        legacy_key = target.legacy_context_id
        if not legacy_key:
            return {}

        row = db.get(Query().key == legacy_key)
        if row is None or row.get("kind") == "settings":
            return {}

        value = row.get("value")
        if not isinstance(value, dict):
            return {}

        self._write_value_locked(target, value)
        db.remove(doc_ids=[row.doc_id])
        return dict(value)

    def _write_value_locked(self, target: SettingsTarget, value: dict[str, Any]) -> None:
        db = self._db_for_path(self._db_path_for_target(target))
        payload = {
            "kind": "settings" if target.scope == "context" else "global_settings",
            "key": target.as_key(),
            "value": dict(value),
            "controller_id": target.controller_id,
            "device_id": target.device_id,
            "profile_id": target.profile_id,
            "page_id": target.page_id,
            "slot_id": target.slot_id,
            "action_uuid": target.action_uuid,
            "dynamic_page_uuid": target.dynamic_page_uuid,
            "plugin_uuid": target.plugin_uuid,
        }
        db.upsert(payload, self._row_query(target))

    def _row_query(self, target: SettingsTarget):
        query = Query()
        kind = "settings" if target.scope == "context" else "global_settings"
        base = (query.kind == kind) & (query.key == target.as_key())
        if target.scope != "context":
            return base
        return base & (
            (query.controller_id == target.controller_id)
            | (~query.controller_id.exists())
        )

    async def _notify(
        self,
        subscribers: set[anyio.abc.ObjectSendStream[dict[str, Any]]],
        snapshot: dict[str, Any],
    ) -> None:
        for send in subscribers:
            try:
                await send.send(dict(snapshot))
            except Exception:
                logger.exception("Failed to notify settings subscriber")
