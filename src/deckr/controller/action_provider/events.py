"""Internal controller events for action-provider state."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionsChangedEvent:
    """Emitted by ActionRegistry when registered action availability changes."""

    registered: list[str]  # qualified IDs now available: provider_instance::action
    unregistered: list[str]  # qualified IDs no longer available
