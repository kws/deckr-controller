"""ActionRegistry: receives actionsRegistered/actionsUnregistered, provides get_action(address)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from deckr.core.component import BaseComponent, RunContext
from deckr.core.messaging import EventBus
from deckr.plugin.messages import (
    ACTIONS_REGISTERED,
    ACTIONS_UNREGISTERED,
    HOST_OFFLINE,
    ActionsChangedEvent,
    HostMessage,
    parse_host_address,
)

from deckr.controller.plugin.builtin import BuiltinRegistry
from deckr.controller.plugin.provider import ActionMetadata

if TYPE_CHECKING:
    from deckr.plugin.interface import PluginAction

logger = logging.getLogger(__name__)


def _qualified_id(host_id: str, action_uuid: str) -> str:
    """Build fully qualified action ID: host_id::action_uuid."""
    return f"{host_id}::{action_uuid}"


class ActionRegistry(BaseComponent):
    """Standalone component: subscribes to actionsRegistered/actionsUnregistered, provides get_action(address).

    Registry stores actions by qualified ID (host_id::action_uuid). Resolution supports:
    - host_id::action_id: host-specific lookup
    - action_id: host-agnostic (first available match)
    """

    def __init__(self, event_bus: EventBus, *, controller_id: str):
        super().__init__(name="ActionRegistry")
        self._event_bus = event_bus
        self._controller_id = controller_id
        self._builtin_registry = BuiltinRegistry()
        self._action_registry: dict[str, tuple[str, str, dict]] = {}

    async def get_action(self, address: str) -> ActionMetadata | None:
        """Resolve by address: host_id::action_id (host-specific) or action_id (host-agnostic)."""
        if "::" in address:
            entry = self._action_registry.get(address)
            if entry is None:
                return None
            host_id, action_uuid, meta = entry
            return ActionMetadata(
                uuid=action_uuid,
                host_id=host_id,
                name=meta.get("name"),
                plugin_uuid=meta.get("pluginUuid"),
                controllers=meta.get("controllers"),
                property_inspector_path=meta.get("propertyInspectorPath"),
                manifest_defaults=meta.get("manifestDefaults"),
            )
        for key, (host_id, action_uuid, meta) in self._action_registry.items():
            if key.endswith(f"::{address}"):
                return ActionMetadata(
                    uuid=action_uuid,
                    host_id=host_id,
                    name=meta.get("name"),
                    plugin_uuid=meta.get("pluginUuid"),
                    controllers=meta.get("controllers"),
                    property_inspector_path=meta.get("propertyInspectorPath"),
                    manifest_defaults=meta.get("manifestDefaults"),
                )
        return None

    def get_builtin_action(self, uuid: str) -> PluginAction | None:
        """Return builtin action instance for direct dispatch."""
        return self._builtin_registry.get_action(uuid)

    async def _handle_actions_registered(self, msg: HostMessage) -> None:
        """Handle actionsRegistered. Add actions to registry."""
        payload = msg.payload
        host_id = (
            payload.get("hostId") or parse_host_address(msg.from_id) or msg.from_id
        )
        touched: list[str] = []
        seen: set[str] = set()
        actions = payload.get("actions", [])
        for a in actions:
            action_uuid = a.get("uuid")
            if action_uuid:
                qualified = _qualified_id(host_id, action_uuid)
                self._action_registry[qualified] = (
                    host_id,
                    action_uuid,
                    {
                        "controllers": a.get("controllers"),
                        "manifestDefaults": a.get("manifestDefaults"),
                        "name": a.get("name"),
                        "pluginUuid": a.get("pluginUuid"),
                        "propertyInspectorPath": a.get("propertyInspectorPath"),
                    },
                )
                if qualified not in seen:
                    touched.append(qualified)
                    seen.add(qualified)
        action_uuids = payload.get("actionUuids", [])
        for action_uuid in action_uuids:
            qualified = _qualified_id(host_id, action_uuid)
            if qualified not in self._action_registry:
                self._action_registry[qualified] = (host_id, action_uuid, {})
            if qualified not in seen:
                touched.append(qualified)
                seen.add(qualified)
        if touched:
            logger.info(
                "Registered %d action(s) from host %s: %s",
                len(touched),
                host_id,
                touched,
            )
            await self._event_bus.send(
                ActionsChangedEvent(registered=touched, unregistered=[])
            )

    async def _handle_actions_unregistered(self, msg: HostMessage) -> None:
        """Handle actionsUnregistered. Remove actions from registry."""
        payload = msg.payload
        host_id = (
            payload.get("hostId") or parse_host_address(msg.from_id) or msg.from_id
        )
        action_uuids = payload.get("actionUuids", [])
        removed: list[str] = []
        for action_uuid in action_uuids:
            qualified = _qualified_id(host_id, action_uuid)
            if qualified in self._action_registry:
                del self._action_registry[qualified]
                removed.append(qualified)
        if removed:
            logger.info(
                "Unregistered %d action(s) from host %s: %s",
                len(removed),
                host_id,
                removed,
            )
            await self._event_bus.send(
                ActionsChangedEvent(registered=[], unregistered=removed)
            )

    async def _handle_host_offline(self, msg: HostMessage) -> None:
        """Remove all actions for a host when the transport reports it offline."""
        payload = msg.payload
        host_id = (
            payload.get("hostId") or parse_host_address(msg.from_id) or msg.from_id
        )
        removed = [
            qualified
            for qualified, (entry_host_id, _, _) in self._action_registry.items()
            if entry_host_id == host_id
        ]
        for qualified in removed:
            del self._action_registry[qualified]
        if removed:
            logger.warning(
                "Host %s went offline; removing %d actions", host_id, len(removed)
            )
            await self._event_bus.send(
                ActionsChangedEvent(registered=[], unregistered=removed)
            )

    async def start(self, ctx: RunContext) -> None:
        start_soon = getattr(ctx.tg, "start_soon", None)
        if start_soon is None:
            raise RuntimeError("ActionRegistry requires start_soon in RunContext")

        # Register builtin actions first
        for action_uuid in self._builtin_registry.provides_actions():
            meta = self._builtin_registry.get_metadata(action_uuid)
            if meta:
                qualified = _qualified_id("builtin", action_uuid)
                self._action_registry[qualified] = (
                    "builtin",
                    action_uuid,
                    {
                        "controllers": meta.get("controllers"),
                        "manifestDefaults": meta.get("manifest_defaults"),
                        "name": meta.get("name"),
                        "pluginUuid": meta.get("plugin_uuid"),
                        "propertyInspectorPath": meta.get(
                            "property_inspector_path"
                        ),
                    },
                )

        start_soon(self._subscription_loop)

    async def _subscription_loop(self) -> None:
        async with self._event_bus.subscribe() as stream:
            async for event in stream:
                if not isinstance(event, HostMessage):
                    continue
                if not event.for_controller(self._controller_id):
                    continue
                try:
                    if event.type == ACTIONS_REGISTERED:
                        await self._handle_actions_registered(event)
                    elif event.type == ACTIONS_UNREGISTERED:
                        await self._handle_actions_unregistered(event)
                    elif event.type == HOST_OFFLINE:
                        await self._handle_host_offline(event)
                except Exception:
                    logger.exception(
                        "Error handling message %s from %s",
                        event.type,
                        event.from_id,
                    )

    async def stop(self) -> None:
        self._action_registry.clear()
