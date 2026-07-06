from __future__ import annotations

import logging

import yaml

from deckr.controller.config import _reader
from deckr.controller.config._data import DeviceConfig


def _config_data(config_id: str = "desk") -> dict:
    return {
        "id": config_id,
        "name": "Desk",
        "match": {"fingerprint": "fingerprint:desk"},
        "profiles": [
            {
                "name": "default",
                "pages": [
                    {
                        "controls": [
                            {
                                "selector": {"control_id": "0,0"},
                                "action": "action.clock",
                            }
                        ]
                    }
                ],
            }
        ],
    }


def test_load_config_returns_device_config_for_valid_yaml(tmp_path) -> None:
    path = tmp_path / "desk.yml"
    path.write_text(yaml.safe_dump(_config_data()), encoding="utf-8")

    config = _reader.load_config(path)

    assert isinstance(config, DeviceConfig)
    assert config.id == "desk"
    assert config.profiles[0].pages[0].controls[0].action == "action.clock"


def test_load_config_returns_none_and_logs_for_invalid_data(tmp_path, caplog) -> None:
    caplog.set_level(logging.ERROR, logger="deckr.controller.config._reader")
    path = tmp_path / "bad.yml"
    path.write_text("id: missing-required-fields\n", encoding="utf-8")

    assert _reader.load_config(path) is None
    assert "Error loading config from" in caplog.text


def test_load_all_configs_loads_only_yml_from_config_dir(tmp_path, monkeypatch) -> None:
    (tmp_path / "desk.yml").write_text(
        yaml.safe_dump(_config_data("desk")),
        encoding="utf-8",
    )
    (tmp_path / "ignored.yaml").write_text(
        yaml.safe_dump(_config_data("ignored")),
        encoding="utf-8",
    )
    (tmp_path / "ignored.txt").write_text("not yaml", encoding="utf-8")
    monkeypatch.setattr(_reader, "CONFIG_DIR", tmp_path)

    configs = tuple(_reader.load_all_configs())

    assert [config.id for config in configs if config is not None] == ["desk"]


def test_get_config_returns_matching_config_and_none_on_miss(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "desk.yml").write_text(
        yaml.safe_dump(_config_data("desk")),
        encoding="utf-8",
    )
    (tmp_path / "pad.yml").write_text(
        yaml.safe_dump(_config_data("pad")),
        encoding="utf-8",
    )
    monkeypatch.setattr(_reader, "CONFIG_DIR", tmp_path)

    assert _reader.get_config("pad").id == "pad"
    assert _reader.get_config("missing") is None
