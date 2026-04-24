from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from deckr.core.config import ConfigDocument
from deckr.core.config import load_config_document as load_core_config
from platformdirs import PlatformDirs
from pydantic import BaseModel, ConfigDict

_SETTINGS_DIRS = PlatformDirs("deckr", "deckr", version="1.0")

_DEFAULT_CONFIG_DOCUMENT_TEXT = """# Deckr configuration document
#
# Reserved top-level namespaces:
#   [deckr.controller]
#   [deckr.bridges.<component>.instances.<instance>]
#   [deckr.plugin_hosts.<component>.instances.<instance>]
#   [deckr.drivers.<component>]

[deckr.controller]

[deckr.controller.device_config.file]
path = "settings"

[deckr.controller.settings.file]
"""


def default_config_document_text() -> str:
    return _DEFAULT_CONFIG_DOCUMENT_TEXT


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceConfigFileSection(_StrictModel):
    path: Path = Path("settings")


class DeviceConfigSection(_StrictModel):
    file: DeviceConfigFileSection | None = None


class SettingsFileSection(_StrictModel):
    path: Path | None = None


class SettingsSection(_StrictModel):
    file: SettingsFileSection | None = None


class ControllerRuntimeConfig(_StrictModel):
    enabled: bool = True
    id: str | None = None
    device_config: DeviceConfigSection | None = None
    settings: SettingsSection | None = None


def _resolve_path(path: Path, *, base_dir: Path) -> Path:
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _default_settings_dir() -> Path:
    return Path(_SETTINGS_DIRS.user_data_dir).resolve()


def _resolve_controller_paths(
    controller: ControllerRuntimeConfig,
    *,
    base_dir: Path,
) -> ControllerRuntimeConfig:
    if controller.device_config and controller.device_config.file:
        controller.device_config.file.path = _resolve_path(
            controller.device_config.file.path,
            base_dir=base_dir,
        )

    if controller.settings and controller.settings.file:
        settings_path = controller.settings.file.path
        if settings_path is None:
            controller.settings.file.path = _default_settings_dir()
        else:
            controller.settings.file.path = _resolve_path(settings_path, base_dir=base_dir)

    return controller


def controller_payload_from_document(document: ConfigDocument) -> Mapping[str, Any]:
    payload = document.namespace("deckr.controller")
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError("[deckr.controller] must be a table")
    return payload


def parse_controller_config(
    payload: Mapping[str, Any] | None,
    *,
    base_dir: Path,
) -> ControllerRuntimeConfig:
    controller = ControllerRuntimeConfig.model_validate(dict(payload or {}))
    return _resolve_controller_paths(controller, base_dir=base_dir)


def controller_config_from_document(document: ConfigDocument) -> ControllerRuntimeConfig:
    return parse_controller_config(
        controller_payload_from_document(document),
        base_dir=document.base_dir,
    )


def load_config_document(path: Path | None) -> ConfigDocument:
    core_document = load_core_config(
        path,
        default_text=default_config_document_text(),
    )
    controller_config_from_document(core_document)
    return core_document
