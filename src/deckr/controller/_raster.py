"""Controller raster encoding value shared by layout and render execution."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RasterFormat:
    width: int
    height: int
    rotation: int = 0
    format: str = "JPEG"
