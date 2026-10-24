"""Device config service protocol and file-backed implementation."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import Protocol

import anyio
from deckr.components import BaseComponent, RunContext
from deckr.core.util.anyio import CoalescedStateBroadcaster
from decouple import config as decouple_config
from watchfiles import Change, awatch

from deckr.controller.config._data import DeviceConfig
from deckr.controller.config._file_storage import read_snapshot, write_config
from deckr.controller.config._materialized import MaterializedConfigPublisher
from deckr.controller.config._selection import copy_config, select_device_config

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


class DeviceConfigService(Protocol):
    """Configuration service: subscribe to receive config and change notifications."""

    async def match_device(
        self,
        *,
        fingerprint: str,
        labels: Mapping[str, str],
    ) -> DeviceConfig | None:
        """Return the best controller config for a live hardware device."""
        ...

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        """Return config by controller-local id."""
        ...

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        """Persist a complete config and publish the updated value."""
        ...

    def subscribe(self, config_id: str) -> AsyncGenerator[DeviceConfig | None, None]:
        """Subscribe to config by controller-local config id.

        First emission: current config (or None if not found).
        Subsequent emissions: full config on each change (or None if removed).

        Close the generator to unsubscribe, including when setup exits early.
        """
        ...


class FileBackedDeviceConfigService(BaseComponent):
    """DeviceConfigService implementation backed by a watched YAML directory."""

    def __init__(
        self,
        config_dir: Path | None = None,
        *,
        materialized_publisher: MaterializedConfigPublisher | None = None,
    ):
        super().__init__(name="FileBackedDeviceConfigService")
        self._config_dir = (
            config_dir if config_dir is not None else resolve_default_config_dir()
        )
        self._materialized_publisher = materialized_publisher
        self._path_to_config: dict[Path, str] = {}
        self._config_by_id: dict[str, DeviceConfig] = {}
        self._documents: dict[Path, str] = {}
        self._state = CoalescedStateBroadcaster[str](current=True)
        self._lock = self._state.lock
        self._publish_lock = anyio.Lock()
        self._scan_lock = anyio.Lock()
        self._write_lock = anyio.Lock()
        self._published_version: int | None = None
        self._has_snapshot = False
        self._stop_event: anyio.Event | None = None
        self.source_available = True

    async def start(self, ctx: RunContext) -> None:
        logger.debug(
            "Using device config path %s",
            self._config_dir,
        )
        self._stop_event = anyio.Event()
        ctx.tg.start_soon(self._watch_loop)

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        await self._state.aclose()

    async def match_device(
        self, *, fingerprint: str, labels: Mapping[str, str]
    ) -> DeviceConfig | None:
        if not self._has_snapshot:
            await self._scan_configs()
        async with self._lock:
            return copy_config(
                select_device_config(
                    self._config_by_id.values(), fingerprint=fingerprint, labels=labels
                )
            )

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        return await self._load_config(config_id)

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        async with self._write_lock:
            await self._scan_configs()
            async with self._lock:
                path = next(
                    (
                        path
                        for path, config_id in self._path_to_config.items()
                        if config_id == config.id
                    ),
                    self._config_dir / f"{config.id}.yml",
                )
            await anyio.to_thread.run_sync(write_config, self._config_dir, path, config)
            await self.refresh()
        return config

    async def refresh(self) -> None:
        await self._scan_configs()
        await self._publish_materialized_snapshot()

    def subscribe(self, config_id: str) -> AsyncGenerator[DeviceConfig | None, None]:
        return self._subscribe_impl(config_id)

    async def _subscribe_impl(
        self, config_id: str
    ) -> AsyncGenerator[DeviceConfig | None, None]:
        await self._scan_configs()
        async with self._state.subscribe(
            lambda _version, _current: copy_config(self._config_by_id.get(config_id))
        ) as subscription:
            yield subscription.initial
            async for wakeup in subscription:
                if not wakeup.resnapshot_required and config_id not in wakeup.changed:
                    continue
                try:
                    yield await self._state.capture(
                        lambda _version, _current: copy_config(
                            self._config_by_id.get(config_id)
                        )
                    )
                except anyio.ClosedResourceError:
                    return

    async def _load_config(self, config_id: str) -> DeviceConfig | None:
        async with self._lock:
            if config_id in self._config_by_id:
                return self._config_by_id[config_id].model_copy(deep=True)
        if not self._has_snapshot:
            await self._scan_configs()
        async with self._lock:
            return copy_config(self._config_by_id.get(config_id))

    async def _scan_configs(self) -> None:
        async with self._scan_lock:
            async with self._lock:
                previous_paths = dict(self._path_to_config)
                previous_configs = dict(self._config_by_id)
            try:
                paths, configs, documents = await anyio.to_thread.run_sync(
                    read_snapshot,
                    self._config_dir,
                    previous_paths,
                    previous_configs,
                    self._documents,
                )
            except (OSError, ValueError):
                self.source_available = False
                logger.warning(
                    "Config source invalid or unavailable; retaining validated snapshots",
                    exc_info=True,
                )
                return
            async with self._lock:
                changed = {
                    key
                    for key in self._config_by_id.keys() | configs.keys()
                    if self._config_by_id.get(key) != configs.get(key)
                }
                self._path_to_config, self._config_by_id = paths, configs
                self._documents = documents
                self._has_snapshot = self.source_available = True
                if changed:
                    self._state.publish_locked(changed)

    async def _watch_loop(self) -> None:
        """Background task: watch config_dir and emit to subscribers on changes."""
        await self.refresh()
        if self._stop_event is None:
            return
        while not self._stop_event.is_set():
            try:
                async for changes in awatch(
                    self._config_dir,
                    watch_filter=_yaml_filter,
                    recursive=False,
                    stop_event=self._stop_event,
                    yield_on_timeout=True,
                    rust_timeout=500,
                ):
                    await self._process_changes(changes)
            except anyio.get_cancelled_exc_class():
                raise
            except OSError:
                # A missing mount/directory ends a native watch. Keep the last
                # snapshot and retry so restoration needs no controller restart.
                await self.refresh()
                await anyio.sleep(0.5)

    async def _process_changes(self, changes: set[tuple[Change, str]]) -> None:
        """Treat notifications as hints; a successful scan establishes deletion.

        Periodic scans also cover events lost while a native watch starts or
        reconnects. Invalid files and an unavailable source retain prior values.
        """
        await self.refresh()

    async def _publish_materialized_snapshot(self) -> None:
        if self._materialized_publisher is None:
            return
        # Serialize writes so a slow, older snapshot cannot replace a newer one.
        # Acknowledge only successful publication; periodic refresh retries failures,
        # including the initial empty snapshot that removes stale remote configs.
        async with self._publish_lock:
            async with self._lock:
                version = self._state.version
                if not self._has_snapshot or version == self._published_version:
                    return
                configs = tuple(self._config_by_id.values())
            try:
                await self._materialized_publisher.publish_configs(configs)
            except Exception:
                logger.exception("Failed to publish materialized config snapshot")
            else:
                self._published_version = version


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
        labels: Mapping[str, str],
    ) -> DeviceConfig | None:
        del fingerprint, labels
        return None

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        del config_id
        return None

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        del config
        raise RuntimeError("No device config service is configured")

    def subscribe(self, config_id: str) -> AsyncGenerator[DeviceConfig | None, None]:
        del config_id
        return self._subscribe_impl()

    async def _subscribe_impl(self) -> AsyncGenerator[DeviceConfig | None, None]:
        yield None
