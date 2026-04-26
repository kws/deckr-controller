"""ActionRegistry: receives actionsRegistered/actionsUnregistered, provides get_action(address)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from deckr.contracts.messages import DeckrMessage, parse_host_address
from deckr.core.component import BaseComponent, RunContext
from deckr.pluginhost.messages import (
    ACTIONS_REGISTERED,
    ACTIONS_UNREGISTERED,
    HOST_OFFLINE,
    ActionDescriptor,
    plugin_message_for_controller,
    plugin_payload,
)
from deckr.transports.bus import EventBus

from deckr.controller.plugin.builtin import BuiltinRegistry
from deckr.controller.plugin.events import ActionsChangedEvent
from deckr.controller.plugin.provider import ActionMetadata

if TYPE_CHECKING:
    from deckr.python_plugin.interface import PluginAction

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

    def __init__(
        self,
        event_bus: EventBus,
        *,
        controller_id: str,
        on_actions_changed: Callable[[ActionsChangedEvent], Awaitable[None]] | None = None,
    ):
        super().__init__(name="ActionRegistry")
        self._event_bus = event_bus
        self._controller_id = controller_id
        self._on_actions_changed = on_actions_changed
        self._builtin_registry = BuiltinRegistry()
        self._action_registry: dict[str, tuple[str, ActionDescriptor]] = {}

    async def get_action(self, address: str) -> ActionMetadata | None:
        """Resolve by address: host_id::action_id (host-specific) or action_id (host-agnostic)."""
        if "::" in address:
            entry = self._action_registry.get(address)
            if entry is None:
                return None
            host_id, descriptor = entry
            return ActionMetadata(
                uuid=descriptor.uuid,
                host_id=host_id,
                name=descriptor.name,
                plugin_uuid=descriptor.plugin_uuid,
            )
        for key, (host_id, descriptor) in self._action_registry.items():
            if key.endswith(f"::{address}"):
                return ActionMetadata(
                    uuid=descriptor.uuid,
                    host_id=host_id,
                    name=descriptor.name,
                    plugin_uuid=descriptor.plugin_uuid,
                )
        return None

    def get_builtin_action(self, uuid: str) -> PluginAction | None:
        """Return builtin action instance for direct dispatch."""
        return self._builtin_registry.get_action(uuid)

    async def _publish_actions_changed(self, event: ActionsChangedEvent) -> None:
        if self._on_actions_changed is not None:
            await self._on_actions_changed(event)

    async def _handle_actions_registered(self, msg: DeckrMessage) -> None:
        """Handle actionsRegistered. Add actions to registry."""
        payload = plugin_payload(msg)
        host_id = payload.get("hostId") or parse_host_address(msg.sender)
        if not host_id:
            logger.warning("Ignoring actionsRegistered from invalid host address %s", msg.sender)
            return
        touched: list[str] = []
        seen: set[str] = set()
        actions = payload.get("actions", [])
        for a in actions:
            try:
                descriptor = ActionDescriptor.model_validate(a)
            except ValueError:
                logger.warning(
                    "Ignoring invalid action descriptor from host %s: %r",
                    host_id,
                    a,
                )
                continue
            action_uuid = descriptor.uuid
            if action_uuid:
                qualified = _qualified_id(host_id, action_uuid)
                self._action_registry[qualified] = (
                    host_id,
                    descriptor,
                )
                if qualified not in seen:
                    touched.append(qualified)
                    seen.add(qualified)
        action_uuids = payload.get("actionUuids", [])
        for action_uuid in action_uuids:
            qualified = _qualified_id(host_id, action_uuid)
            if qualified not in self._action_registry:
                self._action_registry[qualified] = (
                    host_id,
                    ActionDescriptor(uuid=action_uuid),
                )
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
            await self._publish_actions_changed(
                ActionsChangedEvent(registered=touched, unregistered=[])
            )

    async def _handle_actions_unregistered(self, msg: DeckrMessage) -> None:
        """Handle actionsUnregistered. Remove actions from registry."""
        payload = plugin_payload(msg)
        host_id = payload.get("hostId") or parse_host_address(msg.sender)
        if not host_id:
            logger.warning(
                "Ignoring actionsUnregistered from invalid host address %s",
                msg.sender,
            )
            return
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
            await self._publish_actions_changed(
                ActionsChangedEvent(registered=[], unregistered=removed)
            )

    async def _handle_host_offline(self, msg: DeckrMessage) -> None:
        """Remove all actions for a host when the transport reports it offline."""
        payload = plugin_payload(msg)
        host_id = payload.get("hostId") or parse_host_address(msg.sender)
        if not host_id:
            logger.warning("Ignoring hostOffline from invalid host address %s", msg.sender)
            return
        removed = [
            qualified
            for qualified, (entry_host_id, _) in self._action_registry.items()
            if entry_host_id == host_id
        ]
        for qualified in removed:
            del self._action_registry[qualified]
        if removed:
            logger.warning(
                "Host %s went offline; removing %d actions", host_id, len(removed)
            )
            await self._publish_actions_changed(
                ActionsChangedEvent(registered=[], unregistered=removed)
            )

    async def start(self, ctx: RunContext) -> None:
        start_soon = getattr(ctx.tg, "start_soon", None)
        if start_soon is None:
            raise RuntimeError("ActionRegistry requires start_soon in RunContext")

        # Register builtin actions first
        for action_uuid in self._builtin_registry.provides_actions():
            descriptor = self._builtin_registry.get_action_descriptor(action_uuid)
            if descriptor:
                qualified = _qualified_id("builtin", action_uuid)
                self._action_registry[qualified] = (
                    "builtin",
                    descriptor,
                )

        start_soon(self._subscription_loop)

    async def _subscription_loop(self) -> None:
        async with self._event_bus.subscribe() as stream:
            async for event in stream:
                if not isinstance(event, DeckrMessage):
                    continue
                if not plugin_message_for_controller(event, self._controller_id):
                    continue
                try:
                    if event.message_type == ACTIONS_REGISTERED:
                        await self._handle_actions_registered(event)
                    elif event.message_type == ACTIONS_UNREGISTERED:
                        await self._handle_actions_unregistered(event)
                    elif event.message_type == HOST_OFFLINE:
                        await self._handle_host_offline(event)
                except Exception:
                    logger.exception(
                        "Error handling message %s from %s",
                        event.message_type,
                        event.sender,
                    )

    async def stop(self) -> None:
        self._action_registry.clear()
