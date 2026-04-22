from __future__ import annotations

from pathlib import Path

import pytest
from platformdirs import PlatformDirs

from deckr.controller import default_config_document_text, load_config_document


def test_default_config_document_matches_builtin_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    document = load_config_document(None)

    assert document.source_path is None
    assert document.base_dir == tmp_path.resolve()
    assert document.controller.log_level == "info"
    assert document.controller.device_config is not None
    assert document.controller.device_config.file is not None
    assert document.controller.device_config.file.path == (tmp_path / "settings").resolve()
    assert document.controller.settings is not None
    assert document.controller.settings.file is not None
    assert document.controller.settings.file.path == Path(
        PlatformDirs("deckr", "deckr", version="1.0").user_data_dir
    ).resolve()
    assert document.children("plugin_hosts") == {}
    assert document.children("drivers") == {}


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

[deckr.plugin_hosts.python]
host_id = "living-room"

[deckr.plugin_hosts.python.runtime]
descriptor_roots = ["plugins/runtime"]

[deckr.plugins.openhab]
url = "http://openhab.local:8080"
api_key = "secret"

[deckr.drivers.mqtt.broker]
hostname = "mqtt.local"
port = 1884
""".strip()
    )

    document = load_config_document(config_path)

    assert document.source_path == config_path.resolve()
    assert document.base_dir == tmp_path.resolve()
    assert document.controller.id == "controller-main"
    assert document.controller.device_config is not None
    assert document.controller.device_config.file is not None
    assert document.controller.device_config.file.path == (tmp_path / "configs").resolve()
    assert document.controller.settings is not None
    assert document.controller.settings.file is not None
    assert document.controller.settings.file.path == (tmp_path / "state").resolve()
    assert document.plugin_host_config("python")["host_id"] == "living-room"
    assert document.plugin_host_config("python")["runtime"]["descriptor_roots"] == (
        "plugins/runtime",
    )
    assert document.namespace("plugins.openhab") == document.plugin_config("openhab")
    assert document.plugin_config("openhab")["url"] == "http://openhab.local:8080"
    assert document.driver_config("mqtt")["broker"]["hostname"] == "mqtt.local"


def test_explicit_config_requires_deckr_controller_table(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[deckr.plugins.openhab]\nurl = 'http://openhab.local:8080'\n")

    with pytest.raises(ValueError, match=r"\[deckr\.controller\]"):
        load_config_document(config_path)


def test_auto_loads_local_deckr_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.controller]
log_level = "warning"

[deckr.plugin_hosts.python]
enabled = false
""".strip()
    )
    monkeypatch.chdir(tmp_path)

    document = load_config_document(None)

    assert document.source_path == config_path.resolve()
    assert document.controller.log_level == "warning"
    assert document.plugin_host_config("python")["enabled"] is False


def test_default_config_document_text_contains_controller_table() -> None:
    assert "[deckr.controller]" in default_config_document_text()
