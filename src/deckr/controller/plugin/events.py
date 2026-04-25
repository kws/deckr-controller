"""Internal controller events for plugin action state."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionsChangedEvent:
    """Emitted by ActionRegistry when registered action availability changes."""

    registered: list[str]  # qualified IDs now available: host_id::action_uuid
    unregistered: list[str]  # qualified IDs no longer available: host_id::action_uuid
