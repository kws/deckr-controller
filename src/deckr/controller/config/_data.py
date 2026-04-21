from typing import Literal

from pydantic import BaseModel


class TitleOptions(BaseModel):
    """Font and styling options for title rendering (from config)."""

    font_family: str | None = None
    font_size: int | str | None = None
    font_style: Literal["", "Bold Italic", "Bold", "Italic", "Regular"] | None = None
    title_color: str | None = None
    title_alignment: Literal["top", "middle", "bottom"] | None = None


class Control(BaseModel):
    slot: str
    action: str
    settings: dict
    title_options: TitleOptions | None = None


class Page(BaseModel):
    controls: list[Control]
    widget_timeout_ms: int | None = None


class Profile(BaseModel):
    name: str
    pages: list[Page]
    widget_timeout_ms: int | None = None


class DeviceConfig(BaseModel):
    id: str
    name: str
    profiles: list[Profile]
