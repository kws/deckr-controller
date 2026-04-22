from __future__ import annotations

from pathlib import Path

from deckr.controller._config_document import load_config_document
from deckr.controller._service import (
    _build_config_service,
    _build_settings_service,
)
from deckr.controller.config import (
    FileBackedDeviceConfigService,
    NullDeviceConfigService,
)
from deckr.controller.settings import FileBackedSettingsService, InMemorySettingsService


def test_build_services_disable_when_sections_are_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[deckr.controller]\n")
    document = load_config_document(config_path)

    assert isinstance(_build_config_service(document), NullDeviceConfigService)
    assert isinstance(_build_settings_service(document), InMemorySettingsService)


def test_build_services_enable_when_sections_are_present(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.controller]

[deckr.controller.device_config.file]
path = "configs"

[deckr.controller.settings.file]
path = "state"
""".strip()
    )
    document = load_config_document(config_path)

    config_service = _build_config_service(document)
    settings_service = _build_settings_service(document)

    assert isinstance(config_service, FileBackedDeviceConfigService)
    assert config_service._config_dir == (tmp_path / "configs").resolve()
    assert isinstance(settings_service, FileBackedSettingsService)
    assert settings_service._settings_dir == (tmp_path / "state").resolve()
