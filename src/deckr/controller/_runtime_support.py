from __future__ import annotations

from deckr.controller._config_document import ControllerRuntimeConfig
from deckr.controller.config import (
    FileBackedDeviceConfigService,
    NullDeviceConfigService,
)
from deckr.controller.settings import InMemorySettingsService


def build_config_service(config: ControllerRuntimeConfig):
    device_config = config.device_config
    if device_config is None or device_config.file is None:
        return NullDeviceConfigService()
    return FileBackedDeviceConfigService(config_dir=device_config.file.path)


def build_settings_service(config: ControllerRuntimeConfig):
    return InMemorySettingsService()
