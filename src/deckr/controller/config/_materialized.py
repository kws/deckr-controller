from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import anyio
from deckr.components import BaseComponent, RunContext
from deckr.contracts.keys import encode_key_token
from deckr.contracts.models import DeckrModel
from deckr.substrates.nats_kv import KvBucketPolicy, KvEntry, KvUnavailable
from pydantic import Field, field_serializer, field_validator

from deckr.controller.config._data import DeviceConfig

logger = logging.getLogger(__name__)

CONFIG_STATE_BUCKET = "dev_deckr_controller_config_v1"
CONFIG_PROJECTION_SCHEMA = "dev.deckr.controller.config.materialized.v1"
CONFIG_RESULT_SCHEMA = "dev.deckr.controller.config.result.v1"
CONFIG_WATCH_RETRY_SECONDS = 1.0


def _labels_match(
    actual: Mapping[str, str],
    required: Mapping[str, str],
) -> bool:
    return all(actual.get(key) == value for key, value in required.items())


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
        self._subscribers: dict[
            str, set[anyio.abc.ObjectSendStream[DeviceConfig | None]]
        ] = {}
        self._lock = anyio.Lock()
        self._stopping: anyio.Event | None = None

    async def start(self, ctx: RunContext) -> None:
        self._stopping = ctx.stopping
        await self._reconcile(reason="startup")
        ctx.tg.start_soon(self._watch_loop)

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()

    async def match_device(
        self,
        *,
        fingerprint: str,
        labels: Mapping[str, str],
    ) -> DeviceConfig | None:
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
        async with self._lock:
            return self._config_by_id.get(config_id)

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        async with self._lock:
            configs = dict(self._config_by_id)
            configs[config.id] = config
        publisher = MaterializedConfigPublisher(
            controller_id=self._controller_id,
            bucket=self._bucket,
            producer=MaterializedConfigProducer(
                id=f"controller:{self._controller_id}",
                kind="controller_runtime",
            ),
        )
        entry = await publisher.publish_configs(tuple(configs.values()))
        await self._activate_configs(
            tuple(sorted(configs.values(), key=lambda item: item.id)),
            source_revision=entry.revision,
        )
        return config

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        return self._subscribe_impl(config_id)

    async def _subscribe_impl(
        self,
        config_id: str,
    ) -> AsyncIterator[DeviceConfig | None]:
        send, receive = anyio.create_memory_object_stream[DeviceConfig | None](
            max_buffer_size=32
        )
        async with self._lock:
            self._subscribers.setdefault(config_id, set()).add(send)
            initial = self._config_by_id.get(config_id)
        try:
            await send.send(initial)
            async for value in receive:
                yield value
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(config_id)
                if subscribers is not None:
                    subscribers.discard(send)
                    if not subscribers:
                        self._subscribers.pop(config_id, None)
            await send.aclose()

    async def _watch_loop(self) -> None:
        while self._stopping is None or not self._stopping.is_set():
            try:
                async with self._bucket.watch(self._config_key) as stream:
                    async for change in stream:
                        if change is None:
                            continue
                        if change.key != self._config_key:
                            continue
                        await self._reconcile(
                            reason=f"watch {change.operation} {change.key}"
                        )
            except KvUnavailable:
                logger.warning("Materialized config state unavailable; retrying")
                await anyio.sleep(CONFIG_WATCH_RETRY_SECONDS)

    async def _reconcile(self, *, reason: str) -> None:
        try:
            entry = await self._bucket.get(self._config_key)
        except KvUnavailable:
            logger.warning("Could not read materialized config during %s", reason)
            raise
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
            affected_ids = set(old_configs) | set(configs)
            self._config_by_id = dict(configs)
            to_send = [
                (
                    self._config_by_id.get(config_id),
                    set(self._subscribers.get(config_id, set())),
                )
                for config_id in affected_ids
                if old_configs.get(config_id) != self._config_by_id.get(config_id)
            ]
        for value, subscribers in to_send:
            for send in subscribers:
                try:
                    await send.send(value)
                except Exception:
                    logger.exception("Failed to send materialized config update")

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
