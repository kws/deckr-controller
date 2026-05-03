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
    assert document.children("deckr.action_providers") == {}
    assert document.children("deckr.drivers") == {}


def test_load_config_document_resolves_relative_paths_and_namespaces(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.controller]
id = "controller-main"

[deckr.controller.device_config.file]
path = "configs"

[deckr.action_providers.python.instances.main]
provider_id = "clock"
provider_instance_id = "living-room"
controller_id = "controller-main"

[deckr.action_providers.python.instances.main.labels]
room = "living"

[deckr.drivers.mqtt.broker]
hostname = "mqtt.local"
port = 1884
""".strip()
    )

    document = load_config_document(config_path)
    controller = controller_config_from_document(document)

    assert document.source_path == config_path.resolve()
    assert document.base_dir == tmp_path.resolve()
    assert controller.id == "controller-main"
    assert controller.device_config is not None
    assert controller.device_config.file is not None
    assert controller.device_config.file.path == (tmp_path / "configs").resolve()
    action_provider = document.namespace("deckr.action_providers.python")
    assert action_provider is not None
    assert action_provider["instances"]["main"]["provider_instance_id"] == (
        "living-room"
    )
    assert action_provider["instances"]["main"]["provider_id"] == "clock"
    assert action_provider["instances"]["main"]["labels"]["room"] == "living"
    manager_config = document.namespace("deckr.drivers.mqtt")
    assert manager_config is not None
    assert manager_config["broker"]["hostname"] == "mqtt.local"


def test_explicit_config_allows_missing_controller_table(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        "[deckr.action_providers.openhab]\nurl = 'http://openhab.local:8080'\n"
    )

    document = load_config_document(config_path)
    assert document.namespace("deckr.action_providers.openhab") == {
        "url": "http://openhab.local:8080"
    }


def test_auto_loads_local_deckr_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.controller]

[deckr.action_providers.python.instances.main]
enabled = false
""".strip()
    )
    monkeypatch.chdir(tmp_path)

    document = load_config_document(None)

    assert document.source_path == config_path.resolve()
    action_provider = document.namespace("deckr.action_providers.python")
    assert action_provider is not None
    assert action_provider["instances"]["main"]["enabled"] is False


def test_default_config_document_text_contains_controller_table() -> None:
    assert "[deckr.controller]" in default_config_document_text()
