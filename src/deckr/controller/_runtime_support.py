from __future__ import annotations

from deckr.controller._config_document import ControllerRuntimeConfig
from deckr.controller.config import (
    FileBackedDeviceConfigService,
    MaterializedDeviceConfigService,
    NullDeviceConfigService,
)
from deckr.controller.settings import ConfigBackedSettingsService


def build_config_service(
    config: ControllerRuntimeConfig,
    *,
    controller_id: str | None = None,
    materialized_bucket=None,
):
    device_config = config.device_config
    if device_config is None:
        return NullDeviceConfigService()
    if device_config.materialized is not None:
        if controller_id is None or materialized_bucket is None:
            raise ValueError(
                "materialized device config requires controller_id and materialized_bucket"
            )
        return MaterializedDeviceConfigService(
            controller_id=controller_id,
            bucket=materialized_bucket,
        )
    if device_config.file is None:
        return NullDeviceConfigService()
    return FileBackedDeviceConfigService(config_dir=device_config.file.path)


def build_settings_service(
    config: ControllerRuntimeConfig,
    *,
    controller_id: str,
    config_service,
    action_provider=None,
    availability_service=None,
):
    del config
    return ConfigBackedSettingsService(
        controller_id=controller_id,
        config_service=config_service,
        action_provider=action_provider,
        availability_service=availability_service,
    )
