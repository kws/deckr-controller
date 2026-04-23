from __future__ import annotations

from pathlib import Path

import pytest
from platformdirs import PlatformDirs

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
    assert controller.log_level == "info"
    assert controller.device_config is not None
    assert controller.device_config.file is not None
    assert controller.device_config.file.path == (tmp_path / "settings").resolve()
    assert controller.settings is not None
    assert controller.settings.file is not None
    assert controller.settings.file.path == Path(
        PlatformDirs("deckr", "deckr", version="1.0").user_data_dir
    ).resolve()
    assert document.children("deckr.plugin_hosts") == {}
    assert document.children("deckr.drivers") == {}


def test_load_config_document_resolves_relative_paths_and_namespaces(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.controller]
id = "controller-main"
log_level = "debug"

[deckr.controller.device_config.file]
path = "configs"

[deckr.controller.settings.file]
path = "state"

[deckr.plugin_hosts.python.instances.main]
host_id = "living-room"

[deckr.plugin_hosts.python.instances.main.runtime]
descriptor_roots = ["plugins/runtime"]

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
    assert controller.settings is not None
    assert controller.settings.file is not None
    assert controller.settings.file.path == (tmp_path / "state").resolve()
    plugin_host = document.namespace("deckr.plugin_hosts.python")
    assert plugin_host is not None
    assert plugin_host["instances"]["main"]["host_id"] == (
        "living-room"
    )
    assert plugin_host["instances"]["main"]["runtime"]["descriptor_roots"] == (
        "plugins/runtime",
    )
    driver = document.namespace("deckr.drivers.mqtt")
    assert driver is not None
    assert driver["broker"]["hostname"] == "mqtt.local"


def test_explicit_config_allows_missing_controller_table(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[deckr.plugins.openhab]\nurl = 'http://openhab.local:8080'\n")

    document = load_config_document(config_path)
    controller = controller_config_from_document(document)

    assert controller.log_level == "info"
    assert document.namespace("deckr.plugins.openhab") == {
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
log_level = "warning"

[deckr.plugin_hosts.python.instances.main]
enabled = false
""".strip()
    )
    monkeypatch.chdir(tmp_path)

    document = load_config_document(None)
    controller = controller_config_from_document(document)

    assert document.source_path == config_path.resolve()
    assert controller.log_level == "warning"
    plugin_host = document.namespace("deckr.plugin_hosts.python")
    assert plugin_host is not None
    assert plugin_host["instances"]["main"]["enabled"] is False


def test_default_config_document_text_contains_controller_table() -> None:
    assert "[deckr.controller]" in default_config_document_text()
