"""Per-context declaration store. No frames, no rendered bytes."""

from __future__ import annotations

from dataclasses import dataclass, field

from deckr.contracts.models import JsonObject


@dataclass
class RenderContent:
    """Current render declaration for one control context."""

    title: str | None = None
    image: str | None = None


@dataclass
class RenderOverlay:
    """Current transient overlay declaration for one control context."""

    template: str
    title: str | None = None
    params: JsonObject = field(default_factory=dict)
    overlay_id: str | None = None
    generation: int = 0


class ControlStateStore:
    """In-memory declarations for one control context."""

    def __init__(self, context_id: str, binding_id: str | None = None):
        self.context_id = context_id
        self.binding_id = binding_id
        self.content = RenderContent()
        self.overlay: RenderOverlay | None = None
        self.base_output_generation = 0
        self.overlay_generation = 0
        self.settings: dict = {}
