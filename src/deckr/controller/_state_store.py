"""Per-context declaration store. No frames, no rendered bytes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from deckr.pluginhost.messages import TitleOptions


@dataclass
class RenderContent:
    """Current render declaration for one control context."""

    title: str | None = None
    image: str | None = None
    title_options: TitleOptions | None = None


@dataclass
class TransientOverlay:
    """Temporary overlay (showAlert / showOk); cleared on expiry."""

    type: Literal["alert", "ok"]
    expires_at: float  # time.monotonic() deadline


class ControlStateStore:
    """In-memory declarations for one control context."""

    def __init__(self, context_id: str, binding_id: str | None = None):
        self.context_id = context_id
        self.binding_id = binding_id
        self.content = RenderContent()
        self.overlay: TransientOverlay | None = None
        self.settings: dict = {}
        self.default_title_options: TitleOptions | None = None
