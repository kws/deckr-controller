from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from deckr.components import (
    BaseComponent,
    ComponentContext,
    ComponentDefinition,
    ComponentManifest,
    RunContext,
)
from deckr.state import PERSISTENT_STATE_STORE_POLICY
from pydantic import BaseModel, ConfigDict, field_validator

from deckr.controller.config import (
    CONFIG_STATE_BUCKET,
    FileBackedDeviceConfigService,
    MaterializedConfigProducer,
    MaterializedConfigPublisher,
)

CONFIG_WATCHER_COMPONENT_ID = "dev.deckr.controller.config_watcher"
_STATE_BUCKET_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigWatcherConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path = Path("settings")
    target_controller_id: str
    bucket: str = CONFIG_STATE_BUCKET
    producer_id: str | None = None

    @field_validator("target_controller_id")
    @classmethod
    def _validate_target_controller_id(cls, value: str) -> str:
        resolved = value.strip()
        if not resolved:
            raise ValueError("targetControllerId must not be empty")
        return resolved

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str) -> str:
        bucket = value.strip()
        if not bucket:
            raise ValueError("bucket must not be empty")
        if not _STATE_BUCKET_RE.fullmatch(bucket):
            raise ValueError("bucket must use JetStream-safe underscore tokens")
        return bucket


class MaterializedConfigWatcherComponent(BaseComponent):
    def __init__(
        self,
        *,
        runtime_name: str,
        config: ConfigWatcherConfig,
        config_dir: Path,
        publisher: MaterializedConfigPublisher,
    ) -> None:
        super().__init__(name=runtime_name)
        del config
        self._config_service = FileBackedDeviceConfigService(
            config_dir=config_dir,
            materialized_publisher=publisher,
        )

    async def start(self, ctx: RunContext) -> None:
        await self._config_service.start(ctx)

    async def stop(self) -> None:
        await self._config_service.stop()


def component_factory(context: ComponentContext):
    config = _parse_config(context.config)
    config_dir = (
        context.base_dir / config.path
        if not config.path.is_absolute()
        else config.path
    )
    config_dir = config_dir.resolve()
    state = context.state(config.bucket, policy=PERSISTENT_STATE_STORE_POLICY)
    publisher = MaterializedConfigPublisher(
        controller_id=config.target_controller_id,
        state=state,
        producer=MaterializedConfigProducer(
            id=config.producer_id or context.runtime_name,
            kind="file_watcher",
        ),
    )
    return MaterializedConfigWatcherComponent(
        runtime_name=context.runtime_name,
        config=config,
        config_dir=config_dir,
        publisher=publisher,
    )


def _parse_config(source: Mapping[str, Any]) -> ConfigWatcherConfig:
    return ConfigWatcherConfig.model_validate(dict(source))


component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id=CONFIG_WATCHER_COMPONENT_ID,
        role="controller_config_producer",
    ),
    factory=component_factory,
)
