"""Device config service protocol and file-backed implementation."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import anyio
import yaml
from deckr.components import BaseComponent, RunContext
from decouple import config as decouple_config
from watchfiles import Change, awatch

from deckr.controller.config._data import DeviceConfig

logger = logging.getLogger(__name__)


def resolve_default_config_dir() -> Path:
    """Resolve the default device-config directory from env."""

    return Path(
        decouple_config(
            "DECKR_CONFIG_DIR",
            default=decouple_config("CONFIG_DIR", default="settings"),
        )
    ).resolve()


def _yaml_filter(change: Change, path: str) -> bool:
    """Only include .yml and .yaml files."""
    return path.endswith(".yml") or path.endswith(".yaml")


def _load_config_file(path: Path) -> DeviceConfig | None:
    """Load and validate DeviceConfig from a file. Returns None on error."""
    try:
        content = path.read_text()
        data = yaml.safe_load(content)
        if data is None:
            return None
        return DeviceConfig.model_validate(data)
    except Exception:
        logger.exception("Error loading config from %s", path)
        return None


class DeviceConfigService(Protocol):
    """Configuration service: subscribe to receive config and change notifications."""

    async def match_device(
        self,
        *,
        fingerprint: str,
        manager_id: str,
    ) -> DeviceConfig | None:
        """Return the best controller config for a live hardware device."""
        ...

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        """Subscribe to config by controller-local config id.

        First emission: current config (or None if not found).
        Subsequent emissions: full config on each change (or None if removed).

        Exit the iterator to unsubscribe.
        """
        ...


class FileBackedDeviceConfigService(BaseComponent):
    """DeviceConfigService implementation backed by a watched YAML directory."""

    def __init__(self, config_dir: Path | None = None):
        super().__init__(name="FileBackedDeviceConfigService")
        self._config_dir = (
            config_dir if config_dir is not None else resolve_default_config_dir()
        )
        self._path_to_config: dict[Path, str] = {}
        self._config_by_id: dict[str, DeviceConfig] = {}
        self._subscribers: dict[
            str, set[anyio.abc.ObjectSendStream[DeviceConfig | None]]
        ] = {}
        self._lock = anyio.Lock()
        self._stop_event: anyio.Event | None = None

    async def start(self, ctx: RunContext) -> None:
        logger.warning(
            "Using device config path %s",
            self._config_dir,
        )
        self._stop_event = anyio.Event()
        ctx.tg.start_soon(self._watch_loop)

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def match_device(
        self,
        *,
        fingerprint: str,
        manager_id: str,
    ) -> DeviceConfig | None:
        await self._scan_configs()
        async with self._lock:
            candidates = [
                config
                for config in self._config_by_id.values()
                if config.enabled
                and config.match.fingerprint == fingerprint
                and config.match.manager_id in {None, manager_id}
            ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda config: config.match.manager_id is not None,
            reverse=True,
        )
        best_specificity = candidates[0].match.manager_id is not None
        best = [
            config
            for config in candidates
            if (config.match.manager_id is not None) == best_specificity
        ]
        if len(best) > 1:
            ids = ", ".join(sorted(config.id for config in best))
            raise ValueError(
                f"Ambiguous device config match for fingerprint {fingerprint!r} "
                f"manager {manager_id!r}: {ids}"
            )
        return best[0]

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        return self._subscribe_impl(config_id)

    async def _subscribe_impl(
        self, config_id: str
    ) -> AsyncIterator[DeviceConfig | None]:
        send, receive = anyio.create_memory_object_stream[DeviceConfig | None](
            max_buffer_size=32
        )
        async with self._lock:
            if config_id not in self._subscribers:
                self._subscribers[config_id] = set()
            self._subscribers[config_id].add(send)

        try:
            # Send initial config
            cfg = await self._load_config(config_id)
            await send.send(cfg)

            async for value in receive:
                yield value
        finally:
            async with self._lock:
                subs = self._subscribers.get(config_id)
                if subs is not None:
                    subs.discard(send)
                    if not subs:
                        del self._subscribers[config_id]
            await send.aclose()

    async def _load_config(self, config_id: str) -> DeviceConfig | None:
        async with self._lock:
            if config_id in self._config_by_id:
                return self._config_by_id[config_id]
        await self._scan_configs()
        async with self._lock:
            return self._config_by_id.get(config_id)

    async def _scan_configs(self) -> None:
        """Scan config_dir for .yml files and update caches."""
        path_to_config: dict[Path, str] = {}
        config_by_id: dict[str, DeviceConfig] = {}
        for path in self._config_dir.glob("*.yml"):
            cfg = _load_config_file(path)
            if cfg is not None:
                path_to_config[path] = cfg.id
                config_by_id[cfg.id] = cfg
        for path in self._config_dir.glob("*.yaml"):
            cfg = _load_config_file(path)
            if cfg is not None:
                path_to_config[path] = cfg.id
                config_by_id[cfg.id] = cfg
        async with self._lock:
            self._path_to_config = path_to_config
            self._config_by_id = config_by_id

    async def _watch_loop(self) -> None:
        """Background task: watch config_dir and emit to subscribers on changes."""
        await self._scan_configs()
        if self._stop_event is None:
            return
        try:
            async for changes in awatch(
                self._config_dir,
                watch_filter=_yaml_filter,
                recursive=False,
                stop_event=self._stop_event,
            ):
                await self._process_changes(changes)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:
            logger.exception("Config watch loop failed")

    async def _process_changes(self, changes: set[tuple[Change, str]]) -> None:
        """Process file changes and notify affected subscribers."""
        to_send: list[
            tuple[
                DeviceConfig | None,
                set[anyio.abc.ObjectSendStream[DeviceConfig | None]],
            ]
        ] = []
        async with self._lock:
            affected_config_ids: set[str] = set()
            for change, path_str in changes:
                path = Path(path_str)
                if change == Change.deleted:
                    config_id = self._path_to_config.pop(path, None)
                    if config_id is not None:
                        self._config_by_id.pop(config_id, None)
                        affected_config_ids.add(config_id)
                else:
                    # added or modified
                    old_config_id = self._path_to_config.get(path)
                    cfg = _load_config_file(path)
                    if cfg is not None:
                        if old_config_id is not None and old_config_id != cfg.id:
                            affected_config_ids.add(old_config_id)
                        self._path_to_config[path] = cfg.id
                        self._config_by_id[cfg.id] = cfg
                        affected_config_ids.add(cfg.id)
                    elif old_config_id is not None:
                        # Parse error: keep previous config, do not emit
                        pass

            for config_id in affected_config_ids:
                config = self._config_by_id.get(config_id)
                subs = self._subscribers.get(config_id, set())
                if subs:
                    to_send.append((config, set(subs)))

        for config, subs in to_send:
            for send in subs:
                try:
                    await send.send(config)
                except Exception:
                    logger.exception("Failed to send config update to subscriber")


class NullDeviceConfigService(BaseComponent):
    """DeviceConfigService that always yields no config and performs no I/O."""

    def __init__(self) -> None:
        super().__init__(name="NullDeviceConfigService")

    async def start(self, ctx: RunContext) -> None:
        return

    async def stop(self) -> None:
        return

    async def match_device(
        self,
        *,
        fingerprint: str,
        manager_id: str,
    ) -> DeviceConfig | None:
        del fingerprint, manager_id
        return None

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        del config_id
        return self._subscribe_impl()

    async def _subscribe_impl(self) -> AsyncIterator[DeviceConfig | None]:
        yield None
