from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from deckr.core.config import ConfigDocument
from deckr.core.config import load_config_document as load_core_config
from pydantic import BaseModel, ConfigDict, field_validator

from deckr.controller.config._materialized import CONFIG_STATE_BUCKET

_DEFAULT_CONFIG_DOCUMENT_TEXT = """# Deckr configuration document
#
# Reserved top-level namespaces:
#   [deckr.runtime.substrate]
#   [deckr.components.instances.<instance>]

[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.file]
path = "settings"
"""

_STATE_BUCKET_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def default_config_document_text() -> str:
    return _DEFAULT_CONFIG_DOCUMENT_TEXT


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceConfigFileSection(_StrictModel):
    path: Path = Path("settings")


class DeviceConfigMaterializedSection(_StrictModel):
    bucket: str = CONFIG_STATE_BUCKET

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str) -> str:
        bucket = value.strip()
        if not bucket:
            raise ValueError("materialized bucket must not be empty")
        if not _STATE_BUCKET_RE.fullmatch(bucket):
            raise ValueError(
                "materialized bucket must use JetStream-safe underscore tokens"
            )
        return bucket


class DeviceConfigSection(_StrictModel):
    file: DeviceConfigFileSection | None = None
    materialized: DeviceConfigMaterializedSection | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.file is not None and self.materialized is not None:
            raise ValueError("device_config may configure either file or materialized")


class RenderObservationSection(_StrictModel):
    enabled: bool = False
    sink: Literal["jsonl"] = "jsonl"
    path: Path | None = None
    include_graph: bool = False
    include_context: bool = False

    def model_post_init(self, __context: Any) -> None:
        if self.enabled and self.path is None:
            raise ValueError("render observation path is required when enabled")


class RenderSection(_StrictModel):
    backend: Literal["process_pool", "thread"] = "process_pool"
    observation: RenderObservationSection | None = None


class ControllerRuntimeConfig(_StrictModel):
    device_config: DeviceConfigSection | None = None
    render: RenderSection | None = None


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
    if (
        controller.render is not None
        and controller.render.observation is not None
        and controller.render.observation.path is not None
    ):
        controller.render.observation.path = _resolve_path(
            controller.render.observation.path,
            base_dir=base_dir,
        )

    return controller


def controller_payload_from_document(document: ConfigDocument) -> Mapping[str, Any]:
    instances = document.children("deckr.components.instances")
    matches = [
        source.get("config", {})
        for source in instances.values()
        if source.get("component") == "dev.deckr.controller"
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


def controller_config_from_document(
    document: ConfigDocument,
) -> ControllerRuntimeConfig:
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
