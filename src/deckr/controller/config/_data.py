from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TitleOptions(_ConfigModel):
    """Font and styling options for title rendering (from config)."""

    font_family: str | None = None
    font_size: int | str | None = None
    font_style: Literal["", "Bold Italic", "Bold", "Italic", "Regular"] | None = None
    title_color: str | None = None
    title_alignment: Literal["top", "middle", "bottom"] | None = None


class CapabilitySelector(_ConfigModel):
    """A required capability advertised by a device control."""

    capability_id: str | None = None
    family: str | None = None
    capability_type: str | None = Field(default=None, alias="type")
    direction: Literal["input", "output", "state", "command"] | None = None
    event_types: tuple[str, ...] = Field(default_factory=tuple)
    command_types: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _require_selector_field(self) -> "CapabilitySelector":
        if not any(
            (
                self.capability_id,
                self.family,
                self.capability_type,
                self.direction,
                self.event_types,
                self.command_types,
            )
        ):
            raise ValueError("capability selector must include at least one criterion")
        return self


class GeometrySelector(_ConfigModel):
    """Selector for modeled control geometry metadata."""

    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    unit: Literal["grid", "pixel", "normalized", "millimeter"] | None = None
    rotation: int | None = None
    layer: int | None = None

    @model_validator(mode="after")
    def _require_selector_field(self) -> "GeometrySelector":
        if not any(
            value is not None
            for value in (
                self.x,
                self.y,
                self.width,
                self.height,
                self.unit,
                self.rotation,
                self.layer,
            )
        ):
            raise ValueError("geometry selector must include at least one criterion")
        return self


class ControlSelector(_ConfigModel):
    """Descriptor-backed selector for one bindable control."""

    control_id: str | None = None
    kind: str | None = None
    group_id: str | None = None
    parent_control_id: str | None = None
    surface_id: str | None = None
    label: str | None = None
    geometry: GeometrySelector | None = None
    capabilities: tuple[CapabilitySelector, ...] = Field(default_factory=tuple)
    input_capabilities: tuple[CapabilitySelector, ...] = Field(
        default_factory=tuple,
        alias="input",
    )
    output_capabilities: tuple[CapabilitySelector, ...] = Field(
        default_factory=tuple,
        alias="output",
    )
    state_capabilities: tuple[CapabilitySelector, ...] = Field(
        default_factory=tuple,
        alias="state",
    )
    config_capabilities: tuple[CapabilitySelector, ...] = Field(
        default_factory=tuple,
        alias="config",
    )
    diagnostic_capabilities: tuple[CapabilitySelector, ...] = Field(
        default_factory=tuple,
        alias="diagnostic",
    )

    @model_validator(mode="after")
    def _require_selector_field(self) -> "ControlSelector":
        if not any(
            (
                self.control_id,
                self.kind,
                self.group_id,
                self.parent_control_id,
                self.surface_id,
                self.label,
                self.geometry,
                self.capabilities,
                self.input_capabilities,
                self.output_capabilities,
                self.state_capabilities,
                self.config_capabilities,
                self.diagnostic_capabilities,
            )
        ):
            raise ValueError("control selector must include at least one criterion")
        return self


class Control(_ConfigModel):
    selector: ControlSelector
    action: str
    settings: dict[str, Any] = Field(default_factory=dict)
    title_options: TitleOptions | None = None


class Page(_ConfigModel):
    controls: list[Control]
    widget_timeout_ms: int | None = None


class Profile(_ConfigModel):
    name: str
    pages: list[Page]
    widget_timeout_ms: int | None = None


class DeviceConfigMatch(_ConfigModel):
    fingerprint: str
    manager_id: str | None = None


class DeviceConfig(_ConfigModel):
    id: str
    name: str
    match: DeviceConfigMatch
    enabled: bool = True
    profiles: list[Profile]
