from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from deckr.core.config import ConfigDocument
from deckr.core.config import load_config_document as load_core_config
from platformdirs import PlatformDirs
from pydantic import BaseModel, ConfigDict

_EMPTY_MAPPING = MappingProxyType({})
_SETTINGS_DIRS = PlatformDirs("deckr", "deckr", version="1.0")

_DEFAULT_CONFIG_DOCUMENT_TEXT = """# Deckr configuration document
#
# Reserved top-level namespaces:
#   [deckr.controller]
#   [deckr.drivers.<instance>]
#   [deckr.plugin_hosts.<instance>]
#   [deckr.plugins.<plugin_id>]

[deckr.controller]
log_level = "info"

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
    id: str | None = None
    log_level: str = "info"
    device_config: DeviceConfigSection | None = None
    settings: SettingsSection | None = None


@dataclass(frozen=True, slots=True)
class DeckrConfigDocument:
    controller: ControllerRuntimeConfig
    raw: Mapping[str, Any]
    source_path: Path | None = None
    base_dir: Path = Path.cwd()

    def _normalize_namespace(self, path: str) -> str:
        if not path:
            return "deckr"
        if path == "deckr" or path.startswith("deckr."):
            return path
        return f"deckr.{path}"

    def namespace(self, path: str) -> Mapping[str, Any] | None:
        current: Any = self.raw
        normalized = self._normalize_namespace(path)
        for segment in normalized.split("."):
            if not isinstance(current, Mapping):
                return None
            current = current.get(segment)
        return current if isinstance(current, Mapping) else None

    def children(self, path: str) -> dict[str, Mapping[str, Any]]:
        namespace = self.namespace(path)
        if namespace is None:
            return {}
        return {
            str(name): value
            for name, value in namespace.items()
            if isinstance(value, Mapping)
        }

    def plugin_config(self, plugin_id: str) -> Mapping[str, Any]:
        return self.namespace(f"plugins.{plugin_id}") or _EMPTY_MAPPING

    def driver_config(self, name: str) -> Mapping[str, Any]:
        return self.namespace(f"drivers.{name}") or _EMPTY_MAPPING

    def plugin_host_config(self, name: str) -> Mapping[str, Any]:
        return self.namespace(f"plugin_hosts.{name}") or _EMPTY_MAPPING

    @property
    def core_document(self) -> ConfigDocument:
        return ConfigDocument(
            raw=self.raw,
            source_path=self.source_path,
            base_dir=self.base_dir,
        )


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


def load_config_document(path: Path | None) -> DeckrConfigDocument:
    core_document = load_core_config(
        path,
        default_text=default_config_document_text(),
    )

    controller_payload = core_document.namespace("deckr.controller")
    if controller_payload is None:
        raise ValueError(
            "Configuration document must define a [deckr.controller] table"
        )
    if not isinstance(controller_payload, Mapping):
        raise ValueError("[deckr.controller] must be a table")

    controller = ControllerRuntimeConfig.model_validate(controller_payload)
    controller = _resolve_controller_paths(controller, base_dir=core_document.base_dir)

    return DeckrConfigDocument(
        controller=controller,
        raw=core_document.raw,
        source_path=core_document.source_path,
        base_dir=core_document.base_dir,
    )
