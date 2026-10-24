from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import anyio
from deckr.components import BaseComponent, RunContext
from deckr.contracts.keys import encode_key_token
from deckr.contracts.models import DeckrModel
from deckr.core.kv import (
    KvBucketPolicy,
    KvConflict,
    KvEntry,
    KvUnavailable,
)
from deckr.core.kv_source import KvMaterializedSource
from deckr.core.util.anyio import CoalescedStateBroadcaster
from pydantic import Field, field_serializer, field_validator

from deckr.controller.config._data import DeviceConfig
from deckr.controller.config._selection import copy_config, select_device_config

logger = logging.getLogger(__name__)

CONFIG_STATE_BUCKET = "dev_deckr_controller_config_v1"
CONFIG_PROJECTION_SCHEMA = "dev.deckr.controller.config.materialized.v1"
CONFIG_RESULT_SCHEMA = "dev.deckr.controller.config.result.v1"


def materialized_config_key(controller_id: str) -> str:
    return ".".join(
        (
            "config",
            "controllers",
            encode_key_token(controller_id),
            "materialized",
        )
    )


def materialized_config_result_key(controller_id: str) -> str:
    return ".".join(
        (
            "result",
            "controllers",
            encode_key_token(controller_id),
            "materialized",
        )
    )


def materialized_config_bucket_policy(
    bucket: str = CONFIG_STATE_BUCKET,
) -> KvBucketPolicy:
    return KvBucketPolicy(
        bucket=bucket,
        ttl_seconds=None,
        description="Deckr controller materialized config KV",
    )


class MaterializedConfigProducer(DeckrModel):
    id: str | None = None
    kind: str | None = None
    diagnostics: Mapping[str, Any] = Field(default_factory=dict)


class MaterializedConfigProjection(DeckrModel):
    schema_id: Literal[CONFIG_PROJECTION_SCHEMA] = Field(
        default=CONFIG_PROJECTION_SCHEMA,
        alias="schema",
    )
    controller_id: str = Field(alias="controllerId")
    timestamp: datetime
    device_configs: tuple[DeviceConfig, ...] = Field(
        default_factory=tuple,
        alias="deviceConfigs",
    )
    producer: MaterializedConfigProducer | None = None

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @field_serializer("device_configs")
    def _serialize_device_configs(
        self,
        value: tuple[DeviceConfig, ...],
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(by_alias=True, exclude_none=True, mode="json")
            for item in value
        ]

    @field_validator("device_configs", mode="after")
    @classmethod
    def _reject_duplicate_config_ids(
        cls,
        value: tuple[DeviceConfig, ...],
    ) -> tuple[DeviceConfig, ...]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for config in value:
            if config.id in seen:
                duplicates.add(config.id)
            seen.add(config.id)
        if duplicates:
            raise ValueError(
                "materialized config projection contains duplicate config ids: "
                + ", ".join(sorted(duplicates))
            )
        return value


class MaterializedConfigDiagnostic(DeckrModel):
    severity: Literal["info", "warning", "error"]
    code: str
    message: str
    path: str | None = None


class MaterializedConfigResult(DeckrModel):
    schema_id: Literal[CONFIG_RESULT_SCHEMA] = Field(
        default=CONFIG_RESULT_SCHEMA,
        alias="schema",
    )
    controller_id: str = Field(alias="controllerId")
    timestamp: datetime
    status: Literal["active", "missing", "rejected"]
    source_revision: int | None = Field(default=None, alias="sourceRevision")
    active_config_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="activeConfigIds",
    )
    diagnostics: tuple[MaterializedConfigDiagnostic, ...] = ()

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class MaterializedConfigPublisher:
    def __init__(
        self,
        *,
        controller_id: str,
        bucket,
        producer: MaterializedConfigProducer | None = None,
    ) -> None:
        self.controller_id = controller_id
        self.bucket = bucket
        self.key = materialized_config_key(controller_id)
        self.producer = producer

    async def publish_configs(
        self,
        configs: Sequence[DeviceConfig],
    ) -> KvEntry:
        projection = MaterializedConfigProjection(
            controllerId=self.controller_id,
            timestamp=datetime.now(UTC),
            deviceConfigs=tuple(sorted(configs, key=lambda item: item.id)),
            producer=self.producer,
        )
        return await self.bucket.put(self.key, projection)


class MaterializedDeviceConfigService(BaseComponent):
    def __init__(
        self,
        *,
        controller_id: str,
        bucket,
    ) -> None:
        super().__init__(name="MaterializedDeviceConfigService")
        self._controller_id = controller_id
        self._bucket = bucket
        self._config_key = materialized_config_key(controller_id)
        self._result_key = materialized_config_result_key(controller_id)
        self._config_by_id: dict[str, DeviceConfig] = {}
        self._source = KvMaterializedSource(bucket=bucket, key_prefix=self._config_key)
        self._started = False
        self._watch_started = False
        self._watch_done = anyio.Event()
        self._reconcile_lock = anyio.Lock()
        self._write_lock = anyio.Lock()
        self._state = CoalescedStateBroadcaster[str](current=False)
        self._lock = self._state.lock
        self._stopping: anyio.Event | None = None

    async def start(self, ctx: RunContext) -> None:
        self._stopping = ctx.stopping
        self._started = True
        self._source.start(ctx.tg)
        await self._source.wait_current()
        await self._reconcile(reason="startup")
        self._watch_started = True
        ctx.tg.start_soon(self._watch_loop)

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        await self._source.aclose()
        if self._watch_started:
            await self._watch_done.wait()
        await self._state.aclose()

    @property
    def is_current(self) -> bool:
        return self._source.is_current()

    def watch_state(self):
        """Expose versioned projection snapshots, including freshness changes."""
        return self._source.subscribe()

    async def match_device(
        self, *, fingerprint: str, labels: Mapping[str, str]
    ) -> DeviceConfig | None:
        async with self._lock:
            return copy_config(
                select_device_config(
                    self._config_by_id.values(), fingerprint=fingerprint, labels=labels
                )
            )

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        async with self._lock:
            return copy_config(self._config_by_id.get(config_id))

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        async with self._write_lock:
            for attempt in range(8):
                current = await self._bucket.get(self._config_key)
                if current is None:
                    configs = {}
                else:
                    projection = MaterializedConfigProjection.model_validate(
                        current.value
                    )
                    if projection.controller_id != self._controller_id:
                        raise ValueError("Projection belongs to another controller")
                    configs = {item.id: item for item in projection.device_configs}
                configs[config.id] = config
                projection = MaterializedConfigProjection(
                    controllerId=self._controller_id,
                    timestamp=datetime.now(UTC),
                    deviceConfigs=tuple(
                        sorted(configs.values(), key=lambda item: item.id)
                    ),
                    producer=MaterializedConfigProducer(
                        id=f"controller:{self._controller_id}",
                        kind="controller_runtime",
                    ),
                )
                try:
                    if current is None:
                        entry = await self._bucket.create(self._config_key, projection)
                    else:
                        entry = await self._bucket.update(
                            self._config_key, projection, revision=current.revision
                        )
                except KvConflict:
                    if attempt == 7:
                        raise
                    await anyio.sleep(0)
                    continue
                break
            if self._started:
                await self._source.wait_for_revision(self._config_key, entry.revision)
                await self._reconcile(reason="local write observed")
            else:
                await self._activate_configs(
                    projection.device_configs, source_revision=entry.revision
                )
        return config

    def subscribe(self, config_id: str) -> AsyncGenerator[DeviceConfig | None, None]:
        return self._subscribe_impl(config_id)

    async def _subscribe_impl(
        self,
        config_id: str,
    ) -> AsyncGenerator[DeviceConfig | None, None]:
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

    async def _watch_loop(self) -> None:
        try:
            async with self._source.subscribe() as changes:
                async for _change in changes:
                    await self._reconcile(reason="materialized projection changed")
        finally:
            self._watch_done.set()

    async def _reconcile(self, *, reason: str) -> None:
        async with self._reconcile_lock:
            await self._state.publish((), current=self._source.is_current())
            if not self._source.is_current():
                return
            await self._apply_entry(self._source.get_cached(self._config_key))

    async def _apply_entry(self, entry: KvEntry | None) -> None:
        if entry is None:
            await self._clear_configs()
            await self._write_result(
                status="missing",
                source_revision=None,
                active_config_ids=(),
                diagnostics=(
                    MaterializedConfigDiagnostic(
                        severity="warning",
                        code="projection_missing",
                        message="No materialized controller config projection is available",
                    ),
                ),
            )
            return
        try:
            projection = MaterializedConfigProjection.model_validate(entry.value)
            if projection.controller_id != self._controller_id:
                raise ValueError(
                    "materialized config projection controllerId "
                    f"{projection.controller_id!r} does not match target "
                    f"{self._controller_id!r}"
                )
        except ValueError as exc:
            logger.warning(
                "Rejected materialized config projection %s revision %s",
                self._config_key,
                entry.revision,
                exc_info=True,
            )
            await self._write_result(
                status="rejected",
                source_revision=entry.revision,
                active_config_ids=tuple(sorted(self._config_by_id)),
                diagnostics=(
                    MaterializedConfigDiagnostic(
                        severity="error",
                        code="invalid_projection",
                        message=str(exc),
                    ),
                ),
            )
            return
        await self._activate_configs(
            projection.device_configs,
            source_revision=entry.revision,
        )

    async def _activate_configs(
        self,
        configs: Sequence[DeviceConfig],
        *,
        source_revision: int,
    ) -> None:
        next_configs = {config.id: config for config in configs}
        await self._replace_configs(next_configs)
        await self._write_result(
            status="active",
            source_revision=source_revision,
            active_config_ids=tuple(sorted(next_configs)),
            diagnostics=(),
        )

    async def _clear_configs(self) -> None:
        await self._replace_configs({})

    async def _replace_configs(self, configs: Mapping[str, DeviceConfig]) -> None:
        async with self._lock:
            old_configs = self._config_by_id
            affected_ids = {
                config_id
                for config_id in old_configs.keys() | configs.keys()
                if old_configs.get(config_id) != configs.get(config_id)
            }
            self._config_by_id = dict(configs)
            if affected_ids:
                self._state.publish_locked(affected_ids)

    async def _write_result(
        self,
        *,
        status: Literal["active", "missing", "rejected"],
        source_revision: int | None,
        active_config_ids: tuple[str, ...],
        diagnostics: tuple[MaterializedConfigDiagnostic, ...],
    ) -> None:
        result = MaterializedConfigResult(
            controllerId=self._controller_id,
            timestamp=datetime.now(UTC),
            status=status,
            sourceRevision=source_revision,
            activeConfigIds=active_config_ids,
            diagnostics=diagnostics,
        )
        try:
            await self._bucket.put(self._result_key, result)
        except KvUnavailable:
            logger.warning("Could not write materialized config result")
