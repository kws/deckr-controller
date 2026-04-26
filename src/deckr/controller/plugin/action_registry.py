"""ActionRegistry: receives actionsRegistered/actionsUnregistered, provides get_action(address)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from deckr.contracts.messages import (
    RESERVED_BUILTIN_PROVIDER_IDS,
    DeckrMessage,
    parse_host_address,
)
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

from deckr.controller.plugin.builtin import (
    BUILTIN_ACTION_PROVIDER_ID,
    BuiltinRegistry,
)
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
        self._builtin_action_registry: dict[str, ActionDescriptor] = {}
        self._action_registry: dict[str, tuple[str, ActionDescriptor]] = {}

    async def get_action(self, address: str) -> ActionMetadata | None:
        """Resolve plugin-host actions and internal builtin provider actions."""
        if "::" in address:
            provider_id, _, action_uuid = address.partition("::")
            if provider_id in RESERVED_BUILTIN_PROVIDER_IDS:
                return self._builtin_action_metadata(action_uuid)
            plugin_entry = self._action_registry.get(address)
            if plugin_entry is None:
                return None
            host_id, descriptor = plugin_entry
            return ActionMetadata(
                uuid=descriptor.uuid,
                host_id=host_id,
                name=descriptor.name,
                plugin_uuid=descriptor.plugin_uuid,
            )
        builtin = self._builtin_action_metadata(address)
        if builtin is not None:
            return builtin
        for key, (host_id, descriptor) in self._action_registry.items():
            if key.endswith(f"::{address}"):
                return ActionMetadata(
                    uuid=descriptor.uuid,
                    host_id=host_id,
                    name=descriptor.name,
                    plugin_uuid=descriptor.plugin_uuid,
                )
        return None

    def _builtin_action_metadata(self, action_uuid: str) -> ActionMetadata | None:
        descriptor = self._builtin_action_registry.get(action_uuid)
        if descriptor is None:
            return None
        return ActionMetadata(
            uuid=descriptor.uuid,
            host_id=BUILTIN_ACTION_PROVIDER_ID,
            name=descriptor.name,
            plugin_uuid=descriptor.plugin_uuid,
        )

    def get_builtin_action(self, uuid: str) -> PluginAction | None:
        """Return builtin action instance for direct dispatch."""
        return self._builtin_registry.get_action(uuid)

    async def _publish_actions_changed(self, event: ActionsChangedEvent) -> None:
        if self._on_actions_changed is not None:
            await self._on_actions_changed(event)

    def _host_id_from_sender(
        self,
        msg: DeckrMessage,
        payload: Mapping[str, object],
        *,
        message_type: str,
    ) -> str | None:
        host_id = parse_host_address(msg.sender)
        if host_id is None:
            logger.warning(
                "Ignoring %s from invalid host address %s",
                message_type,
                msg.sender,
            )
            return None
        if host_id in RESERVED_BUILTIN_PROVIDER_IDS:
            logger.warning(
                "Ignoring %s from route-owned host using reserved provider id %s",
                message_type,
                host_id,
            )
            return None
        payload_host_id = payload.get("hostId")
        if payload_host_id is not None and payload_host_id != host_id:
            logger.warning(
                "Ignoring %s from %s with mismatched payload hostId %r",
                message_type,
                msg.sender,
                payload_host_id,
            )
            return None
        return host_id

    async def _handle_actions_registered(self, msg: DeckrMessage) -> None:
        """Handle actionsRegistered. Add actions to registry."""
        payload = plugin_payload(msg)
        host_id = self._host_id_from_sender(
            msg,
            payload,
            message_type=ACTIONS_REGISTERED,
        )
        if not host_id:
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
        host_id = self._host_id_from_sender(
            msg,
            payload,
            message_type=ACTIONS_UNREGISTERED,
        )
        if not host_id:
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

    async def _remove_host_actions(self, host_id: str, *, reason: str) -> None:
        removed = [
            qualified
            for qualified, (entry_host_id, _) in self._action_registry.items()
            if entry_host_id == host_id
        ]
        for qualified in removed:
            del self._action_registry[qualified]
        if removed:
            logger.warning(
                "Host %s became unavailable via %s; removing %d actions",
                host_id,
                reason,
                len(removed),
            )
            await self._publish_actions_changed(
                ActionsChangedEvent(registered=[], unregistered=removed)
            )

    async def _handle_host_offline(self, msg: DeckrMessage) -> None:
        """Handle graceful hostOffline as a lifecycle hint."""
        payload = plugin_payload(msg)
        host_id = self._host_id_from_sender(
            msg,
            payload,
            message_type=HOST_OFFLINE,
        )
        if not host_id:
            return
        await self._remove_host_actions(host_id, reason="hostOffline")

    async def start(self, ctx: RunContext) -> None:
        start_soon = getattr(ctx.tg, "start_soon", None)
        if start_soon is None:
            raise RuntimeError("ActionRegistry requires start_soon in RunContext")

        self._builtin_action_registry.clear()
        for action_uuid in self._builtin_registry.provides_actions():
            descriptor = self._builtin_registry.get_action_descriptor(action_uuid)
            if descriptor:
                self._builtin_action_registry[action_uuid] = descriptor

        start_soon(self._subscription_loop)
        start_soon(self._route_event_loop)

    async def _route_event_loop(self) -> None:
        async with self._event_bus.route_table.subscribe() as stream:
            async for event in stream:
                if event.event_type != "endpointUnreachable" or event.endpoint is None:
                    continue
                if event.lane != self._event_bus.lane:
                    continue
                host_id = parse_host_address(event.endpoint)
                if host_id is None:
                    continue
                await self._remove_host_actions(
                    host_id,
                    reason=event.reason or "routeLoss",
                )

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
        self._builtin_action_registry.clear()
