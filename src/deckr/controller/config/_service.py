"""Device config service protocol and file-backed implementation."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Protocol

import anyio
import yaml
from deckr.components import BaseComponent, RunContext
from deckr.core.util.anyio import CoalescedStateBroadcaster
from decouple import config as decouple_config
from watchfiles import Change, awatch

from deckr.controller.config._data import DeviceConfig
from deckr.controller.config._materialized import MaterializedConfigPublisher

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


def _labels_match(
    actual: Mapping[str, str],
    required: Mapping[str, str],
) -> bool:
    return all(actual.get(key) == value for key, value in required.items())


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

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        """Subscribe to config by controller-local config id.

        First emission: current config (or None if not found).
        Subsequent emissions: full config on each change (or None if removed).

        Exit the iterator to unsubscribe.
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
        self._state = CoalescedStateBroadcaster[str](current=True)
        self._lock = self._state.lock
        self._publish_lock = anyio.Lock()
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
        self,
        *,
        fingerprint: str,
        labels: Mapping[str, str],
    ) -> DeviceConfig | None:
        await self._scan_configs()
        async with self._lock:
            candidates = [
                config
                for config in self._config_by_id.values()
                if config.enabled
                and config.match.fingerprint == fingerprint
                and _labels_match(labels, config.match.labels)
            ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda config: len(config.match.labels),
            reverse=True,
        )
        best_specificity = len(candidates[0].match.labels)
        best = [
            config
            for config in candidates
            if len(config.match.labels) == best_specificity
        ]
        if len(best) > 1:
            ids = ", ".join(sorted(config.id for config in best))
            raise ValueError(
                f"Ambiguous device config match for fingerprint {fingerprint!r} "
                f"labels {dict(labels)!r}: {ids}"
            )
        return best[0]

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        return await self._load_config(config_id)

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        await self._scan_configs()
        async with self._lock:
            path = next(
                (
                    candidate
                    for candidate, config_id in self._path_to_config.items()
                    if config_id == config.id
                ),
                self._config_dir / f"{config.id}.yml",
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        data = config.model_dump(by_alias=True, exclude_none=True, mode="json")
        tmp_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        os.replace(tmp_path, path)
        await self.refresh()
        return config

    async def refresh(self) -> None:
        await self._scan_configs()
        await self._publish_materialized_snapshot()

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        return self._subscribe_impl(config_id)

    async def _subscribe_impl(
        self, config_id: str
    ) -> AsyncIterator[DeviceConfig | None]:
        await self._scan_configs()
        async with self._state.subscribe(
            lambda _version, _current: self._config_by_id.get(config_id)
        ) as subscription:
            yield subscription.initial
            async for wakeup in subscription:
                if not wakeup.resnapshot_required and config_id not in wakeup.changed:
                    continue
                try:
                    yield await self._state.capture(
                        lambda _version, _current: self._config_by_id.get(config_id)
                    )
                except anyio.ClosedResourceError:
                    return

    async def _load_config(self, config_id: str) -> DeviceConfig | None:
        async with self._lock:
            if config_id in self._config_by_id:
                return self._config_by_id[config_id]
        await self._scan_configs()
        async with self._lock:
            return self._config_by_id.get(config_id)

    async def _cache_and_notify(self, path: Path, config: DeviceConfig) -> None:
        async with self._lock:
            old_config_id = self._path_to_config.get(path)
            affected_config_ids = {config.id}
            if old_config_id is not None and old_config_id != config.id:
                affected_config_ids.add(old_config_id)
                self._config_by_id.pop(old_config_id, None)
            self._path_to_config[path] = config.id
            self._config_by_id[config.id] = config
            self._state.publish_locked(affected_config_ids)

    async def _scan_configs(self) -> None:
        """Scan config_dir for .yml files and update caches."""
        # A failed read is not a deletion. Only a successful directory listing
        # can establish that a previously validated file has been removed.
        try:
            paths = tuple(
                path
                for path in self._config_dir.iterdir()
                if path.suffix in {".yml", ".yaml"}
            )
        except OSError:
            self.source_available = False
            logger.warning(
                "Config source unavailable; retaining validated snapshots",
                exc_info=True,
            )
            return
        self.source_available = True
        path_to_config: dict[Path, str] = {}
        config_by_id: dict[str, DeviceConfig] = {}
        for path in paths:
            cfg = _load_config_file(path)
            if cfg is None:
                previous_id = self._path_to_config.get(path)
                cfg = (
                    self._config_by_id.get(previous_id)
                    if previous_id is not None
                    else None
                )
            if cfg is not None:
                path_to_config[path] = cfg.id
                config_by_id[cfg.id] = cfg
        async with self._lock:
            affected_config_ids = {
                config_id
                for config_id in self._config_by_id.keys() | config_by_id.keys()
                if self._config_by_id.get(config_id) != config_by_id.get(config_id)
            }
            self._path_to_config = path_to_config
            self._config_by_id = config_by_id
            self._has_snapshot = True
            if affected_config_ids:
                self._state.publish_locked(affected_config_ids)

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

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        del config_id
        return self._subscribe_impl()

    async def _subscribe_impl(self) -> AsyncIterator[DeviceConfig | None]:
        yield None
