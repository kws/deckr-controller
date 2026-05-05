from __future__ import annotations

from pathlib import Path

import pytest

from deckr.controller import (
    controller_config_from_document,
    default_config_document_text,
    load_config_document,
)


def test_default_config_document_matches_builtin_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    document = load_config_document(None)
    controller = controller_config_from_document(document)

    assert document.source_path is None
    assert document.base_dir == tmp_path.resolve()
    assert controller.device_config is not None
    assert controller.device_config.file is not None
    assert controller.device_config.file.path == (tmp_path / "settings").resolve()
    assert "controller_main" in document.children("deckr.components.instances")


def test_load_config_document_resolves_relative_paths_and_namespaces(
    tmp_path: Path,
) -> None:
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

[deckr.components.instances.clock_actions]
component = "dev.deckr.action_provider_runtime.python"
instance_id = "clock-main"

[deckr.components.instances.clock_actions.endpoints]
action_provider = "living-room"

[deckr.components.instances.clock_actions.config]
provider_id = "dev.deckr.clock"

[deckr.components.instances.clock_actions.config.labels]
room = "living"

[deckr.components.instances.mqtt_main]
component = "dev.deckr.hardware.mqtt"
instance_id = "main"

[deckr.components.instances.mqtt_main.endpoints]
hardware_manager = "mqtt-main"

[deckr.components.instances.mqtt_main.config.broker]
hostname = "mqtt.local"
port = 1884
""".strip()
    )

    document = load_config_document(config_path)
    controller = controller_config_from_document(document)

    assert document.source_path == config_path.resolve()
    assert document.base_dir == tmp_path.resolve()
    assert controller.device_config is not None
    assert controller.device_config.file is not None
    assert controller.device_config.file.path == (tmp_path / "configs").resolve()
    action_provider = document.namespace(
        "deckr.components.instances.clock_actions.config"
    )
    assert action_provider is not None
    assert action_provider["provider_id"] == "dev.deckr.clock"
    assert action_provider["labels"]["room"] == "living"
    manager_config = document.namespace("deckr.components.instances.mqtt_main.config")
    assert manager_config is not None
    assert manager_config["broker"]["hostname"] == "mqtt.local"


def test_explicit_config_allows_missing_controller_table(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[deckr]\n")

    document = load_config_document(config_path)
    assert controller_config_from_document(document).device_config is None


def test_load_config_document_accepts_materialized_device_config(
    tmp_path: Path,
) -> None:
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

    controller = controller_config_from_document(load_config_document(config_path))

    assert controller.device_config is not None
    assert controller.device_config.materialized is not None
    assert controller.device_config.materialized.bucket == "dev_deckr_controller_config_v1"


def test_load_config_document_rejects_multiple_device_config_sources(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.file]
path = "settings"

[deckr.components.instances.controller_main.config.device_config.materialized]
bucket = "dev_deckr_controller_config_v1"
""".strip()
    )

    with pytest.raises(ValueError, match="either file or materialized"):
        load_config_document(config_path)


def test_load_config_document_rejects_invalid_materialized_bucket(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.materialized]
bucket = "dev.deckr.controller.config.v1"
""".strip()
    )

    with pytest.raises(ValueError, match="JetStream-safe underscore"):
        load_config_document(config_path)


def test_auto_loads_local_deckr_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    monkeypatch.chdir(tmp_path)

    document = load_config_document(None)

    assert document.source_path == config_path.resolve()
    assert "controller_main" in document.children("deckr.components.instances")


def test_default_config_document_text_contains_controller_table() -> None:
    assert "[deckr.components.instances.controller_main]" in default_config_document_text()
