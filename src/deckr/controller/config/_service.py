"""Device config service protocol and file-backed implementation."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import anyio
import yaml
from deckr.core.component import BaseComponent, RunContext
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

    def subscribe(self, device_id: str) -> AsyncIterator[DeviceConfig | None]:
        """Subscribe to config for a device.

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
        self._path_to_device: dict[Path, str] = {}
        self._device_to_config: dict[str, DeviceConfig] = {}
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

    def subscribe(self, device_id: str) -> AsyncIterator[DeviceConfig | None]:
        """Subscribe to config for a device. First emission is current; subsequent are updates."""
        return self._subscribe_impl(device_id)

    async def _subscribe_impl(
        self, device_id: str
    ) -> AsyncIterator[DeviceConfig | None]:
        send, receive = anyio.create_memory_object_stream[DeviceConfig | None](
            max_buffer_size=32
        )
        async with self._lock:
            if device_id not in self._subscribers:
                self._subscribers[device_id] = set()
            self._subscribers[device_id].add(send)

        try:
            # Send initial config
            cfg = await self._load_device_config(device_id)
            await send.send(cfg)

            async for value in receive:
                yield value
        finally:
            async with self._lock:
                subs = self._subscribers.get(device_id)
                if subs is not None:
                    subs.discard(send)
                    if not subs:
                        del self._subscribers[device_id]
            await send.aclose()

    async def _load_device_config(self, device_id: str) -> DeviceConfig | None:
        """Load config for device from cache or by scanning files."""
        async with self._lock:
            if device_id in self._device_to_config:
                return self._device_to_config[device_id]
        await self._scan_configs()
        async with self._lock:
            return self._device_to_config.get(device_id)

    async def _scan_configs(self) -> None:
        """Scan config_dir for .yml files and update caches."""
        path_to_device: dict[Path, str] = {}
        device_to_config: dict[str, DeviceConfig] = {}
        for path in self._config_dir.glob("*.yml"):
            cfg = _load_config_file(path)
            if cfg is not None:
                path_to_device[path] = cfg.id
                device_to_config[cfg.id] = cfg
        for path in self._config_dir.glob("*.yaml"):
            cfg = _load_config_file(path)
            if cfg is not None:
                path_to_device[path] = cfg.id
                device_to_config[cfg.id] = cfg
        async with self._lock:
            self._path_to_device = path_to_device
            self._device_to_config = device_to_config

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
            affected_device_ids: set[str] = set()
            for change, path_str in changes:
                path = Path(path_str)
                if change == Change.deleted:
                    device_id = self._path_to_device.pop(path, None)
                    if device_id is not None:
                        self._device_to_config.pop(device_id, None)
                        affected_device_ids.add(device_id)
                else:
                    # added or modified
                    old_device_id = self._path_to_device.get(path)
                    cfg = _load_config_file(path)
                    if cfg is not None:
                        if old_device_id is not None and old_device_id != cfg.id:
                            affected_device_ids.add(old_device_id)
                        self._path_to_device[path] = cfg.id
                        self._device_to_config[cfg.id] = cfg
                        affected_device_ids.add(cfg.id)
                    elif old_device_id is not None:
                        # Parse error: keep previous config, do not emit
                        pass

            for device_id in affected_device_ids:
                config = self._device_to_config.get(device_id)
                subs = self._subscribers.get(device_id, set())
                if subs:
                    to_send.append((config, set(subs)))

        for config, subs in to_send:
            for send in subs:
                try:
                    await send.send(config)
                except Exception:
                    logger.exception("Failed to send config update to subscriber")


# Backward-compatible aliases for existing imports.
ConfigService = DeviceConfigService
FileSystemConfigService = FileBackedDeviceConfigService


class NullDeviceConfigService(BaseComponent):
    """DeviceConfigService that always yields no config and performs no I/O."""

    def __init__(self) -> None:
        super().__init__(name="NullDeviceConfigService")

    async def start(self, ctx: RunContext) -> None:
        return

    async def stop(self) -> None:
        return

    def subscribe(self, device_id: str) -> AsyncIterator[DeviceConfig | None]:
        return self._subscribe_impl()

    async def _subscribe_impl(self) -> AsyncIterator[DeviceConfig | None]:
        yield None
