from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from platformdirs import PlatformDirs
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG_FILENAME = "deckr.toml"
_EMPTY_MAPPING = MappingProxyType({})
_SETTINGS_DIRS = PlatformDirs("deckr", "deckr", version="1.0")

_DEFAULT_CONFIG_DOCUMENT_TEXT = """# Deckr configuration document
#
# Reserved top-level namespaces:
#   [controller]
#   [plugin.<plugin_id>]
#   [driver.<driver_name>]
#   [host.<host_name>]

[controller]
log_level = "info"

[controller.device_config.file]
path = "settings"

[controller.settings.file]

[controller.plugin_hosts.local]
mode = "auto"

[controller.plugin_hosts.local.runtime]
bind_host = "127.0.0.1"
bind_port = 0
claim_token_ttl = 30.0
session_token_ttl = 30.0
restart_backoff_initial = 0.25
restart_backoff_max = 5.0

[controller.local_drivers]
mode = "all_installed"
"""


def default_config_document_text() -> str:
    return _DEFAULT_CONFIG_DOCUMENT_TEXT


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(_freeze(item) for item in value)
    return value


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


class LocalRuntimeSection(_StrictModel):
    bind_host: str = "127.0.0.1"
    bind_port: int = 0
    public_base_url: str | None = None
    descriptor_roots: tuple[Path, ...] = ()
    docker_binary: str = "docker"
    python_executable: str | None = None
    claim_token_ttl: float = 30.0
    session_token_ttl: float = 30.0
    restart_backoff_initial: float = 0.25
    restart_backoff_max: float = 5.0
    log_level: str | None = None


class LocalPluginHostSection(_StrictModel):
    mode: Literal["auto", "enabled"] = "enabled"
    host_id: str | None = None
    runtime: LocalRuntimeSection = Field(default_factory=LocalRuntimeSection)


class MqttPluginHostSection(_StrictModel):
    hostname: str
    port: int = 1883
    topic: str
    username: str | None = None
    password: str | None = None


class PluginHostsSection(_StrictModel):
    local: LocalPluginHostSection | None = None
    mqtt: MqttPluginHostSection | None = None


class LocalDriversSection(_StrictModel):
    mode: Literal["all_installed", "explicit"] = "all_installed"
    names: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_mode(self) -> LocalDriversSection:
        if self.mode == "all_installed" and self.names:
            raise ValueError(
                "controller.local_drivers.names is only valid when mode='explicit'"
            )
        return self


class WebSocketServerSection(_StrictModel):
    host: str
    port: int = 8765


class RemoteHardwareSection(_StrictModel):
    websocket_server: WebSocketServerSection | None = None


class ControllerRuntimeConfig(_StrictModel):
    id: str | None = None
    log_level: str = "info"
    device_config: DeviceConfigSection | None = None
    settings: SettingsSection | None = None
    plugin_hosts: PluginHostsSection | None = None
    local_drivers: LocalDriversSection | None = None
    remote_hardware: RemoteHardwareSection | None = None


@dataclass(frozen=True, slots=True)
class DeckrConfigDocument:
    controller: ControllerRuntimeConfig
    raw: Mapping[str, Any]
    source_path: Path | None = None

    def namespace(self, path: str) -> Mapping[str, Any] | None:
        current: Any = self.raw
        if not path:
            return current if isinstance(current, Mapping) else None
        for segment in path.split("."):
            if not isinstance(current, Mapping):
                return None
            current = current.get(segment)
        return current if isinstance(current, Mapping) else None

    def plugin_config(self, plugin_id: str) -> Mapping[str, Any]:
        return self.namespace(f"plugin.{plugin_id}") or _EMPTY_MAPPING

    def driver_config(self, name: str) -> Mapping[str, Any]:
        return self.namespace(f"driver.{name}") or _EMPTY_MAPPING

    def host_config(self, name: str) -> Mapping[str, Any]:
        return self.namespace(f"host.{name}") or _EMPTY_MAPPING


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

    if (
        controller.plugin_hosts
        and controller.plugin_hosts.local
        and controller.plugin_hosts.local.runtime.descriptor_roots
    ):
        controller.plugin_hosts.local.runtime.descriptor_roots = tuple(
            _resolve_path(path, base_dir=base_dir)
            for path in controller.plugin_hosts.local.runtime.descriptor_roots
        )

    return controller


def _load_payload(
    path: Path | None,
) -> tuple[dict[str, Any], Path | None, bool]:
    if path is not None:
        payload = tomllib.loads(path.read_text())
        return payload, path.resolve(), True

    candidate = (Path.cwd() / DEFAULT_CONFIG_FILENAME).resolve()
    if candidate.exists():
        payload = tomllib.loads(candidate.read_text())
        return payload, candidate, True

    return tomllib.loads(default_config_document_text()), None, False


def load_config_document(path: Path | None) -> DeckrConfigDocument:
    payload, source_path, explicit_file = _load_payload(path)
    if not isinstance(payload, dict):
        raise ValueError("Configuration document root must be a table")

    controller_payload = payload.get("controller")
    if controller_payload is None:
        if explicit_file:
            raise ValueError("Explicit deckr.toml files must define a [controller] table")
        raise ValueError("Default configuration is missing the [controller] table")
    if not isinstance(controller_payload, dict):
        raise ValueError("[controller] must be a table")

    base_dir = source_path.parent if source_path is not None else Path.cwd()
    controller = ControllerRuntimeConfig.model_validate(controller_payload)
    controller = _resolve_controller_paths(controller, base_dir=base_dir)

    return DeckrConfigDocument(
        controller=controller,
        raw=_freeze(payload),
        source_path=source_path,
    )
