"""Action registry backed by plugin-host current-state catalogs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping

import anyio
from deckr.components import BaseComponent, RunContext
from deckr.contracts.messages import RESERVED_BUILTIN_PROVIDER_IDS, host_address
from deckr.contracts.models import thaw_json
from deckr.pluginhost.messages import ActionDescriptor, PluginActionCatalog
from deckr.state import (
    EndpointPresence,
    StateEntry,
    StateStore,
    StateUnavailable,
    parse_plugin_action_catalog_key,
    parse_presence_endpoint_key,
)

from deckr.controller.plugin.builtin import (
    BUILTIN_ACTION_PROVIDER_ID,
    BuiltinAction,
    BuiltinRegistry,
)
from deckr.controller.plugin.events import ActionsChangedEvent
from deckr.controller.plugin.provider import ActionMetadata

logger = logging.getLogger(__name__)

_PLUGIN_CATALOG_PREFIX = "catalog.plugin."
_PLUGIN_HOST_PRESENCE_PREFIX = "presence.endpoint.plugin_messages.host."
_STATE_RECONCILE_SECONDS = 1.0
_WATCH_RETRY_SECONDS = 1.0


def _qualified_id(host_id: str, action_uuid: str) -> str:
    return f"{host_id}::{action_uuid}"


class ActionRegistry(BaseComponent):
    """Resolve builtin actions and plugin actions advertised in KV current state."""

    def __init__(
        self,
        state: StateStore,
        *,
        controller_id: str,
        on_actions_changed: Callable[[ActionsChangedEvent], Awaitable[None]] | None = None,
    ):
        super().__init__(name="ActionRegistry")
        self._state = state
        self._controller_id = controller_id
        self._on_actions_changed = on_actions_changed
        self._builtin_registry = BuiltinRegistry()
        self._builtin_action_registry: dict[str, ActionDescriptor] = {}
        self._action_registry: dict[str, tuple[str, ActionDescriptor]] = {}
        self._catalogs: dict[str, PluginActionCatalog] = {}
        self._host_presence_sessions: dict[str, str] = {}
        self._reconcile_lock = anyio.Lock()

    async def get_action(self, address: str) -> ActionMetadata | None:
        if "::" in address:
            provider_id, _, action_uuid = address.partition("::")
            if provider_id in RESERVED_BUILTIN_PROVIDER_IDS:
                return self._builtin_action_metadata(action_uuid)
            plugin_entry = self._action_registry.get(address)
            if plugin_entry is None:
                return None
            host_id, descriptor = plugin_entry
            return _metadata(host_id, descriptor)

        builtin = self._builtin_action_metadata(address)
        if builtin is not None:
            return builtin
        for key, (host_id, descriptor) in self._action_registry.items():
            if key.endswith(f"::{address}"):
                return _metadata(host_id, descriptor)
        return None

    async def get_action_descriptor(self, address: str) -> ActionDescriptor | None:
        if "::" in address:
            provider_id, _, action_uuid = address.partition("::")
            if provider_id in RESERVED_BUILTIN_PROVIDER_IDS:
                return self._builtin_action_registry.get(action_uuid)
            plugin_entry = self._action_registry.get(address)
            return plugin_entry[1] if plugin_entry is not None else None
        builtin = self._builtin_action_registry.get(address)
        if builtin is not None:
            return builtin
        for key, (_, descriptor) in self._action_registry.items():
            if key.endswith(f"::{address}"):
                return descriptor
        return None

    def host_provides_plugin(self, host_id: str, plugin_id: str) -> bool:
        host_id = host_id.strip()
        plugin_id = plugin_id.strip()
        if not host_id or not plugin_id:
            return False
        if host_id in RESERVED_BUILTIN_PROVIDER_IDS:
            return False
        if host_id not in self._host_presence_sessions:
            return False
        return any(
            entry_host_id == host_id and descriptor.plugin_uuid == plugin_id
            for entry_host_id, descriptor in self._action_registry.values()
        )

    def _builtin_action_metadata(self, action_uuid: str) -> ActionMetadata | None:
        descriptor = self._builtin_action_registry.get(action_uuid)
        if descriptor is None:
            return None
        return _metadata(BUILTIN_ACTION_PROVIDER_ID, descriptor)

    def get_builtin_action(self, uuid: str) -> BuiltinAction | None:
        return self._builtin_registry.get_action(uuid)

    async def start(self, ctx: RunContext) -> None:
        start_soon = getattr(ctx.tg, "start_soon", None)
        if start_soon is None:
            raise RuntimeError("ActionRegistry requires start_soon in RunContext")

        self._builtin_action_registry.clear()
        for action_uuid in self._builtin_registry.provides_actions():
            descriptor = self._builtin_registry.get_action_descriptor(action_uuid)
            if descriptor:
                self._builtin_action_registry[action_uuid] = descriptor

        start_soon(self._presence_loop)
        start_soon(self._catalog_loop)
        start_soon(self._reconciliation_loop)

    async def stop(self) -> None:
        self._action_registry.clear()
        self._builtin_action_registry.clear()
        self._catalogs.clear()
        self._host_presence_sessions.clear()

    async def _presence_loop(self) -> None:
        while True:
            try:
                async with self._state.watch(_PLUGIN_HOST_PRESENCE_PREFIX) as stream:
                    async for change in stream:
                        parsed = parse_presence_endpoint_key(change.key)
                        if parsed is None:
                            continue
                        lane, endpoint = parsed
                        if lane != "plugin_messages" or endpoint.family != "host":
                            continue
                        host_id = endpoint.endpoint_id
                        if not _is_allowed_host_id(host_id):
                            continue
                        await self._reconcile_current_state(
                            reason="host presence watch"
                        )
            except StateUnavailable:
                logger.warning(
                    "Plugin host presence state unavailable; retrying",
                    exc_info=True,
                )
                await anyio.sleep(_WATCH_RETRY_SECONDS)

    async def _catalog_loop(self) -> None:
        while True:
            try:
                async with self._state.watch(_PLUGIN_CATALOG_PREFIX) as stream:
                    async for change in stream:
                        host_id = parse_plugin_action_catalog_key(change.key)
                        if host_id is None or not _is_allowed_host_id(host_id):
                            continue
                        await self._reconcile_current_state(
                            reason="catalog watch"
                        )
            except StateUnavailable:
                logger.warning(
                    "Plugin action catalog state unavailable; retrying",
                    exc_info=True,
                )
                await anyio.sleep(_WATCH_RETRY_SECONDS)

    async def _reconciliation_loop(self) -> None:
        while True:
            try:
                await self._reconcile_current_state(reason="broker snapshot")
            except StateUnavailable:
                logger.warning(
                    "Plugin action current state unavailable; reconciliation will retry",
                    exc_info=True,
                )
            await anyio.sleep(_STATE_RECONCILE_SECONDS)

    async def _reconcile_current_state(self, *, reason: str) -> None:
        async with self._reconcile_lock:
            await self._reconcile_current_state_locked(reason=reason)

    async def _reconcile_current_state_locked(self, *, reason: str) -> None:
        presence_entries = await self._state.items(_PLUGIN_HOST_PRESENCE_PREFIX)
        catalog_entries = await self._state.items(_PLUGIN_CATALOG_PREFIX)

        next_presence_sessions: dict[str, str] = {}
        next_catalogs: dict[str, PluginActionCatalog] = {}
        affected_hosts = set(self._host_presence_sessions) | set(self._catalogs)

        for entry in presence_entries:
            parsed = parse_presence_endpoint_key(entry.key)
            if parsed is None:
                continue
            lane, endpoint = parsed
            if lane != "plugin_messages" or endpoint.family != "host":
                continue
            host_id = endpoint.endpoint_id
            if not _is_allowed_host_id(host_id):
                continue
            affected_hosts.add(host_id)
            presence = _valid_host_presence(entry, host_id=host_id)
            if presence is not None:
                next_presence_sessions[host_id] = presence.session_id

        for entry in catalog_entries:
            host_id = parse_plugin_action_catalog_key(entry.key)
            if host_id is None or not _is_allowed_host_id(host_id):
                continue
            affected_hosts.add(host_id)
            catalog = _valid_catalog(entry, host_id=host_id)
            if catalog is not None:
                next_catalogs[host_id] = catalog

        self._host_presence_sessions = next_presence_sessions
        self._catalogs = next_catalogs
        for host_id in sorted(affected_hosts):
            await self._reconcile_host(host_id, reason=reason)

    async def _reconcile_host(self, host_id: str, *, reason: str) -> None:
        desired = self._desired_host_actions(host_id)
        existing = {
            qualified: descriptor
            for qualified, (entry_host_id, descriptor) in self._action_registry.items()
            if entry_host_id == host_id
        }
        registered = sorted(
            qualified
            for qualified, descriptor in desired.items()
            if existing.get(qualified) != descriptor
        )
        unregistered = sorted(set(existing) - set(desired))

        for qualified in unregistered:
            self._action_registry.pop(qualified, None)
        for qualified, descriptor in desired.items():
            self._action_registry[qualified] = (host_id, descriptor)

        if registered or unregistered:
            logger.info(
                "Plugin host %s action catalog changed via %s: +%s -%s",
                host_id,
                reason,
                registered,
                unregistered,
            )
            await self._publish_actions_changed(
                ActionsChangedEvent(
                    registered=registered,
                    unregistered=unregistered,
                )
            )

    def _desired_host_actions(self, host_id: str) -> dict[str, ActionDescriptor]:
        catalog = self._catalogs.get(host_id)
        if catalog is None:
            return {}
        if self._host_presence_sessions.get(host_id) != catalog.session_id:
            return {}
        return {
            _qualified_id(host_id, descriptor.uuid): descriptor
            for descriptor in catalog.actions.values()
            if descriptor.uuid
        }

    async def _publish_actions_changed(self, event: ActionsChangedEvent) -> None:
        if self._on_actions_changed is not None:
            await self._on_actions_changed(event)


def _metadata(host_id: str, descriptor: ActionDescriptor) -> ActionMetadata:
    return ActionMetadata(
        uuid=descriptor.uuid,
        host_id=host_id,
        name=descriptor.name,
        plugin_uuid=descriptor.plugin_uuid,
        settings_schema=(
            thaw_json(descriptor.settings_schema)
            if descriptor.settings_schema is not None
            else None
        ),
        plugin_settings_schema=(
            thaw_json(descriptor.plugin_settings_schema)
            if descriptor.plugin_settings_schema is not None
            else None
        ),
    )


def _is_allowed_host_id(host_id: str) -> bool:
    if host_id in RESERVED_BUILTIN_PROVIDER_IDS:
        logger.warning("Ignoring plugin host using reserved provider id %s", host_id)
        return False
    return True


def _valid_host_presence(entry: StateEntry, *, host_id: str) -> EndpointPresence | None:
    try:
        presence = EndpointPresence.model_validate(entry.value)
    except ValueError:
        logger.warning("Ignoring invalid plugin host presence %s", entry.key)
        return None
    expected_endpoint = host_address(host_id)
    if presence.endpoint != expected_endpoint or presence.lane != "plugin_messages":
        logger.warning(
            "Ignoring plugin host presence %s with mismatched payload endpoint=%s lane=%s",
            entry.key,
            presence.endpoint,
            presence.lane,
        )
        return None
    return presence


def _valid_catalog(entry: StateEntry, *, host_id: str) -> PluginActionCatalog | None:
    try:
        catalog = PluginActionCatalog.model_validate(entry.value)
    except ValueError:
        logger.warning("Ignoring invalid plugin action catalog %s", entry.key)
        return None
    expected_endpoint = host_address(host_id)
    if catalog.host_id != host_id or catalog.host_endpoint != expected_endpoint:
        logger.warning(
            "Ignoring plugin action catalog %s with mismatched payload host=%s endpoint=%s",
            entry.key,
            catalog.host_id,
            catalog.host_endpoint,
        )
        return None
    valid_actions = _valid_catalog_actions(catalog.actions)
    if len(valid_actions) != len(catalog.actions):
        return PluginActionCatalog(
            hostId=catalog.host_id,
            hostEndpoint=catalog.host_endpoint,
            sessionId=catalog.session_id,
            timestamp=catalog.timestamp,
            ttlSeconds=catalog.ttl_seconds,
            actions=valid_actions,
        )
    return catalog


def _valid_catalog_actions(
    actions: Mapping[str, ActionDescriptor],
) -> dict[str, ActionDescriptor]:
    valid: dict[str, ActionDescriptor] = {}
    for action_uuid, descriptor in actions.items():
        if descriptor.uuid != action_uuid:
            logger.warning(
                "Ignoring plugin action catalog entry keyed %s with descriptor uuid %s",
                action_uuid,
                descriptor.uuid,
            )
            continue
        if not descriptor.uuid.strip():
            continue
        valid[action_uuid] = descriptor
    return valid
