"""Per-context declaration store. No frames, no rendered bytes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from deckr.plugin.manifest import StateOverride, TitleOptions


@dataclass
class TransientOverlay:
    """Temporary overlay (showAlert / showOk); cleared on expiry."""

    type: Literal["alert", "ok"]
    expires_at: float  # time.monotonic() deadline


class ControlStateStore:
    """In-memory declarations for one control context."""

    def __init__(self, context_id: str):
        self.context_id = context_id
        self.state_index: int = 0
        self.overrides: dict[int, StateOverride] = {}
        self.overlay: TransientOverlay | None = None
        self.settings: dict = {}
        self.title_options: TitleOptions | None = None
