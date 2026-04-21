"""Plugin controller protocols used for action lookup and dispatch metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ActionMetadata:
    """Metadata for an action, from host registry or hereIsAction. Replaces PluginAction for message-based dispatch."""

    uuid: str
    host_id: str
    manifest_defaults: dict | None = None


class PluginManager(Protocol):
    """Active manager interface consumed by DeviceManager."""

    async def get_action(self, uuid: str) -> ActionMetadata | None: ...
