from __future__ import annotations

from dataclasses import dataclass

from deckr.controller.action_provider.provider import ActionMetadata


@dataclass(frozen=True, slots=True)
class SettingsActionMetadata:
    action: ActionMetadata | None
    stale: bool
