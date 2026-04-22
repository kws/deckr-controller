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
    assert document.controller.log_level == "info"
    assert document.controller.device_config is not None
    assert document.controller.device_config.file is not None
    assert document.controller.device_config.file.path == (tmp_path / "settings").resolve()
    assert document.controller.settings is not None
    assert document.controller.settings.file is not None
    assert document.controller.settings.file.path == Path(
        PlatformDirs("deckr", "deckr", version="1.0").user_data_dir
    ).resolve()
    assert document.controller.plugin_hosts is not None
    assert document.controller.plugin_hosts.local is not None
    assert document.controller.plugin_hosts.local.mode == "auto"
    assert document.controller.local_drivers is not None
    assert document.controller.local_drivers.mode == "all_installed"


def test_load_config_document_resolves_relative_paths_and_namespaces(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[controller]
id = "controller-main"
log_level = "debug"

[controller.device_config.file]
path = "configs"

[controller.settings.file]
path = "state"

[controller.plugin_hosts.local]
mode = "enabled"
host_id = "living-room"

[controller.plugin_hosts.local.runtime]
descriptor_roots = ["plugins/runtime"]

[controller.local_drivers]
mode = "explicit"
names = ["mqtt"]

[controller.remote_hardware.websocket_server]
host = "127.0.0.1"
port = 8765

[plugin.openhab]
url = "http://openhab.local:8080"
api_key = "secret"

[driver.mqtt.broker]
hostname = "mqtt.local"
port = 1884

[host.pluginhost-main]
kind = "reserved"
""".strip()
    )

    document = load_config_document(config_path)

    assert document.source_path == config_path.resolve()
    assert document.controller.id == "controller-main"
    assert document.controller.device_config is not None
    assert document.controller.device_config.file is not None
    assert document.controller.device_config.file.path == (tmp_path / "configs").resolve()
    assert document.controller.settings is not None
    assert document.controller.settings.file is not None
    assert document.controller.settings.file.path == (tmp_path / "state").resolve()
    assert document.controller.plugin_hosts is not None
    assert document.controller.plugin_hosts.local is not None
    assert document.controller.plugin_hosts.local.runtime.descriptor_roots == (
        (tmp_path / "plugins/runtime").resolve(),
    )
    assert document.namespace("plugin.openhab") == document.plugin_config("openhab")
    assert document.plugin_config("openhab")["url"] == "http://openhab.local:8080"
    assert document.driver_config("mqtt")["broker"]["hostname"] == "mqtt.local"
    assert document.host_config("pluginhost-main")["kind"] == "reserved"


def test_explicit_config_requires_controller_table(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[plugin.openhab]\nurl = 'http://openhab.local:8080'\n")

    with pytest.raises(ValueError, match=r"\[controller\]"):
        load_config_document(config_path)


def test_auto_loads_local_deckr_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[controller]
log_level = "warning"

[controller.local_drivers]
mode = "explicit"
names = ["virtual"]
""".strip()
    )
    monkeypatch.chdir(tmp_path)

    document = load_config_document(None)

    assert document.source_path == config_path.resolve()
    assert document.controller.log_level == "warning"
    assert document.controller.local_drivers is not None
    assert document.controller.local_drivers.names == ("virtual",)


def test_default_config_document_text_contains_controller_table() -> None:
    assert "[controller]" in default_config_document_text()
