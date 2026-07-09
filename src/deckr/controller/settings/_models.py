from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deckr.actions.messages import (
    SettingsProvenance,
    SettingsSchemaMetadata,
    SettingsTargetRef,
)
from deckr.contracts.models import DeckrModel, freeze_json, thaw_json
from pydantic import Field, field_serializer, field_validator


class SettingsSnapshot(DeckrModel):
    """Current controller settings value and metadata for a target."""

    target: SettingsTargetRef
    settings: Mapping[str, Any] = Field(default_factory=dict)
    provenance: tuple[SettingsProvenance, ...] = Field(default_factory=tuple)
    schema_metadata: SettingsSchemaMetadata = Field(
        default_factory=SettingsSchemaMetadata,
        alias="schemaMetadata",
    )

    @field_validator("settings", mode="before")
    @classmethod
    def _thaw_settings(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)
