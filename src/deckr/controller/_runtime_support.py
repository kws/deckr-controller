from __future__ import annotations

from deckr.controller._config_document import ControllerRuntimeConfig
from deckr.controller.config import (
    FileBackedDeviceConfigService,
    NullDeviceConfigService,
)
from deckr.controller.settings import ConfigBackedSettingsService


def build_config_service(config: ControllerRuntimeConfig):
    device_config = config.device_config
    if device_config is None or device_config.file is None:
        return NullDeviceConfigService()
    return FileBackedDeviceConfigService(config_dir=device_config.file.path)


def build_settings_service(
    config: ControllerRuntimeConfig,
    *,
    controller_id: str,
    config_service,
    action_descriptor_provider=None,
):
    del config
    return ConfigBackedSettingsService(
        controller_id=controller_id,
        config_service=config_service,
        action_descriptor_provider=action_descriptor_provider,
    )
