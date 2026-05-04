from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from deckr.core.config import ConfigDocument
from deckr.core.config import load_config_document as load_core_config
from pydantic import BaseModel, ConfigDict

_DEFAULT_CONFIG_DOCUMENT_TEXT = """# Deckr configuration document
#
# Reserved top-level namespaces:
#   [deckr.runtime.substrate]
#   [deckr.components.instances.<instance>]

[deckr.components.instances.controller_main]
component = "com.k-si.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.file]
path = "settings"
"""


def default_config_document_text() -> str:
    return _DEFAULT_CONFIG_DOCUMENT_TEXT


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceConfigFileSection(_StrictModel):
    path: Path = Path("settings")


class DeviceConfigSection(_StrictModel):
    file: DeviceConfigFileSection | None = None


class ControllerRuntimeConfig(_StrictModel):
    device_config: DeviceConfigSection | None = None


def _resolve_path(path: Path, *, base_dir: Path) -> Path:
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


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

    return controller


def controller_payload_from_document(document: ConfigDocument) -> Mapping[str, Any]:
    instances = document.children("deckr.components.instances")
    matches = [
        source.get("config", {})
        for source in instances.values()
        if source.get("component") == "com.k-si.deckr.controller"
    ]
    if not matches:
        return {}
    if len(matches) > 1:
        raise ValueError("Configuration defines more than one Deckr controller")
    payload = matches[0]
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError("Deckr controller component config must be a table")
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
