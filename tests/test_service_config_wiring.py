from __future__ import annotations

from pathlib import Path

from deckr.controller._config_document import load_config_document
from deckr.controller._service import (
    _build_config_service,
    _build_settings_service,
    _resolve_local_driver_names,
    _resolved_driver_configs,
)
from deckr.controller.config import (
    FileBackedDeviceConfigService,
    NullDeviceConfigService,
)
from deckr.controller.settings import FileBackedSettingsService, InMemorySettingsService


def test_build_services_disable_when_sections_are_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[controller]\n")
    document = load_config_document(config_path)

    assert isinstance(_build_config_service(document), NullDeviceConfigService)
    assert isinstance(_build_settings_service(document), InMemorySettingsService)
    assert _resolve_local_driver_names(document.controller) is None


def test_build_services_enable_when_sections_are_present(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[controller]

[controller.device_config.file]
path = "configs"

[controller.settings.file]
path = "state"

[controller.local_drivers]
mode = "explicit"
names = ["mqtt"]

[driver.mqtt.broker]
hostname = "mqtt.local"
""".strip()
    )
    document = load_config_document(config_path)

    config_service = _build_config_service(document)
    settings_service = _build_settings_service(document)

    assert isinstance(config_service, FileBackedDeviceConfigService)
    assert config_service._config_dir == (tmp_path / "configs").resolve()
    assert isinstance(settings_service, FileBackedSettingsService)
    assert settings_service._settings_dir == (tmp_path / "state").resolve()
    assert _resolve_local_driver_names(document.controller) == ["mqtt"]


def test_resolved_driver_configs_inherit_controller_device_config_path(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[controller]

[controller.device_config.file]
path = "configs"

[controller.local_drivers]
mode = "explicit"
names = ["mqtt"]

[driver.mqtt.broker]
hostname = "mqtt.local"
port = 1884
""".strip()
    )
    document = load_config_document(config_path)

    resolved = _resolved_driver_configs(document)

    assert resolved["mqtt"]["config_path"] == str((tmp_path / "configs").resolve())
    assert resolved["mqtt"]["broker"]["hostname"] == "mqtt.local"
    assert resolved["mqtt"]["broker"]["port"] == 1884
