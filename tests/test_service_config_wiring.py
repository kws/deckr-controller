from __future__ import annotations

from pathlib import Path

from deckr.controller._config_document import (
    controller_config_from_document,
    load_config_document,
)
from deckr.controller._runtime_support import (
    build_config_service,
    build_settings_service,
)
from deckr.controller.config import (
    FileBackedDeviceConfigService,
    MaterializedDeviceConfigService,
    NullDeviceConfigService,
)
from deckr.controller.settings import ConfigBackedSettingsService


def test_build_services_disable_when_sections_are_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"
""".strip()
    )
    document = load_config_document(config_path)
    config = controller_config_from_document(document)

    assert isinstance(build_config_service(config), NullDeviceConfigService)
    config_service = build_config_service(config)
    settings_service = build_settings_service(
        config,
        controller_id="controller-main",
        config_service=config_service,
    )
    assert isinstance(settings_service, ConfigBackedSettingsService)


def test_build_services_enable_when_sections_are_present(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.file]
path = "configs"
""".strip()
    )
    document = load_config_document(config_path)
    config = controller_config_from_document(document)

    config_service = build_config_service(config)
    settings_service = build_settings_service(
        config,
        controller_id="controller-main",
        config_service=config_service,
    )

    assert isinstance(config_service, FileBackedDeviceConfigService)
    assert config_service._config_dir == (tmp_path / "configs").resolve()
    assert isinstance(settings_service, ConfigBackedSettingsService)


def test_build_services_enable_materialized_config(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.materialized]
bucket = "dev_deckr_controller_config_v1"
""".strip()
    )
    config = controller_config_from_document(load_config_document(config_path))

    class State:
        pass

    state = State()
    config_service = build_config_service(
        config,
        controller_id="controller-main",
        materialized_state=state,
    )

    assert isinstance(config_service, MaterializedDeviceConfigService)
    assert config_service._state is state
