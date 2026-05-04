"""Action registry backed by action-provider current-state catalogs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import anyio
from deckr.actions.endpoints import (
    BUILTIN_ACTION_PROVIDER_ID,
    RESERVED_BUILTIN_PROVIDER_IDS,
    action_provider_address,
)
from deckr.actions.messages import ActionDescriptor, ActionProviderCatalog
from deckr.actions.state import (
    action_provider_catalog_key,
    parse_action_provider_catalog_key,
)
from deckr.components import BaseComponent, RunContext
from deckr.contracts.messages import ACTIONS_LANE
from deckr.contracts.models import thaw_json
from deckr.state import (
    EndpointPresence,
    StateEntry,
    StateStore,
    StateUnavailable,
    encode_key_token,
    observe_prefix_current,
    parse_presence_endpoint_key,
    presence_endpoint_key,
)

from deckr.controller.action_provider.builtin import (
    BuiltinAction,
    BuiltinRegistry,
)
from deckr.controller.action_provider.events import ActionsChangedEvent
from deckr.controller.action_provider.provider import ActionMetadata

logger = logging.getLogger(__name__)

_ACTION_PROVIDER_CATALOG_PREFIX = "catalog.actions.providers."
_ACTION_PROVIDER_PRESENCE_PREFIX = ".".join(
    (
        "presence",
        "endpoint",
        encode_key_token(ACTIONS_LANE),
        encode_key_token("action_provider"),
        "",
    )
)
_STATE_RECONCILE_SECONDS = 1.0
_WATCH_RETRY_SECONDS = 1.0


def _qualified_id(provider_instance_id: str, action_uuid: str) -> str:
    return f"{provider_instance_id}::{action_uuid}"


@dataclass(frozen=True, slots=True)
class _CatalogAction:
    provider_instance_id: str
    provider_id: str
    session_id: str
    labels: Mapping[str, str]
    descriptor: ActionDescriptor


class ActionRegistry(BaseComponent):
    """Resolve builtin actions and provider actions advertised in current state."""

    def __init__(
        self,
        lease_state: StateStore,
        discovery_state: StateStore,
        *,
        controller_id: str,
        on_actions_changed: Callable[[ActionsChangedEvent], Awaitable[None]] | None = None,
    ):
        super().__init__(name="ActionRegistry")
        self._lease_state = lease_state
        self._discovery_state = discovery_state
        self._controller_id = controller_id
        self._on_actions_changed = on_actions_changed
        self._builtin_registry = BuiltinRegistry()
        self._builtin_action_registry: dict[str, ActionDescriptor] = {}
        self._action_registry: dict[str, _CatalogAction] = {}
        self._catalogs: dict[str, ActionProviderCatalog] = {}
        self._provider_presence_sessions: dict[str, str] = {}
        self._reconcile_lock = anyio.Lock()

    async def get_action(
        self,
        address: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
    ) -> ActionMetadata | None:
        if "::" in address:
            qualified_provider_id, _, action_uuid = address.partition("::")
            if (
                provider_instance_id is not None
                and qualified_provider_id != provider_instance_id
            ):
                return None
            if qualified_provider_id in RESERVED_BUILTIN_PROVIDER_IDS:
                if provider_labels:
                    return None
                return self._builtin_action_metadata(action_uuid)
            entry = self._action_registry.get(address)
            if entry is None or not _labels_match(entry.labels, provider_labels):
                return None
            return _metadata(entry)

        if provider_instance_id in RESERVED_BUILTIN_PROVIDER_IDS:
            return (
                None
                if provider_labels
                else self._builtin_action_metadata(address)
            )
        if provider_instance_id is None and not provider_labels:
            builtin = self._builtin_action_metadata(address)
            if builtin is not None:
                return builtin

        candidates = [
            entry
            for qualified, entry in self._action_registry.items()
            if qualified.endswith(f"::{address}")
            and (
                provider_instance_id is None
                or entry.provider_instance_id == provider_instance_id
            )
            and _labels_match(entry.labels, provider_labels)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda entry: entry.provider_instance_id)
        return _metadata(candidates[0])

    async def get_action_descriptor(
        self,
        address: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
    ) -> ActionDescriptor | None:
        meta = await self.get_action(
            address,
            provider_instance_id=provider_instance_id,
            provider_labels=provider_labels,
        )
        if meta is None:
            return None
        if meta.provider_instance_id in RESERVED_BUILTIN_PROVIDER_IDS:
            return self._builtin_action_registry.get(meta.uuid)
        entry = self._action_registry.get(
            _qualified_id(meta.provider_instance_id, meta.uuid)
        )
        return entry.descriptor if entry is not None else None

    def provider_instance_provides_provider(
        self,
        provider_instance_id: str,
        provider_id: str,
    ) -> bool:
        provider_instance_id = provider_instance_id.strip()
        provider_id = provider_id.strip()
        if not provider_instance_id or not provider_id:
            return False
        if provider_instance_id in RESERVED_BUILTIN_PROVIDER_IDS:
            return False
        return any(
            entry.provider_instance_id == provider_instance_id
            and entry.provider_id == provider_id
            for entry in self._action_registry.values()
        )

    def provider_session_id(self, provider_instance_id: str) -> str | None:
        catalog = self._catalogs.get(provider_instance_id)
        if catalog is None:
            return None
        if (
            self._provider_presence_sessions.get(provider_instance_id)
            != catalog.session_id
        ):
            return None
        return catalog.session_id

    def _builtin_action_metadata(self, action_uuid: str) -> ActionMetadata | None:
        descriptor = self._builtin_action_registry.get(action_uuid)
        if descriptor is None:
            return None
        return _metadata(
            _CatalogAction(
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
                provider_id=descriptor.provider_id or BUILTIN_ACTION_PROVIDER_ID,
                session_id="controller",
                labels={},
                descriptor=descriptor,
            )
        )

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

        start_soon(self._catalog_loop)
        start_soon(self._provider_presence_loop)
        start_soon(self._reconciliation_loop)

    async def stop(self) -> None:
        self._action_registry.clear()
        self._builtin_action_registry.clear()
        self._catalogs.clear()
        self._provider_presence_sessions.clear()

    async def _catalog_loop(self) -> None:
        while True:
            try:
                async with self._discovery_state.watch(
                    _ACTION_PROVIDER_CATALOG_PREFIX
                ) as stream:
                    async for change in stream:
                        provider_instance_id = parse_action_provider_catalog_key(
                            change.key
                        )
                        if provider_instance_id is None or not (
                            _is_allowed_provider_instance_id(provider_instance_id)
                        ):
                            continue
                        await self._reconcile_current_state(
                            reason=(
                                f"catalog watch {change.operation} {change.key}"
                            )
                        )
            except StateUnavailable:
                logger.warning(
                    "Action provider catalog state unavailable; retrying",
                    exc_info=True,
                )
                await anyio.sleep(_WATCH_RETRY_SECONDS)

    async def _provider_presence_loop(self) -> None:
        while True:
            try:
                async with self._lease_state.watch(
                    _ACTION_PROVIDER_PRESENCE_PREFIX
                ) as stream:
                    async for change in stream:
                        parsed = parse_presence_endpoint_key(change.key)
                        if parsed is None:
                            continue
                        lane, endpoint = parsed
                        if lane != ACTIONS_LANE or endpoint.family != "action_provider":
                            continue
                        if not _is_allowed_provider_instance_id(endpoint.endpoint_id):
                            continue
                        await self._reconcile_current_state(
                            reason=(
                                f"provider presence watch {change.operation} "
                                f"{change.key}"
                            )
                        )
            except StateUnavailable:
                logger.warning(
                    "Action provider presence state unavailable; retrying",
                    exc_info=True,
                )
                await anyio.sleep(_WATCH_RETRY_SECONDS)

    async def _reconciliation_loop(self) -> None:
        while True:
            try:
                await self._reconcile_current_state(reason="broker snapshot")
            except StateUnavailable:
                logger.warning(
                    "Action provider current state unavailable; reconciliation will retry",
                    exc_info=True,
                )
            await anyio.sleep(_STATE_RECONCILE_SECONDS)

    async def _reconcile_current_state(self, *, reason: str) -> None:
        async with self._reconcile_lock:
            await self._reconcile_current_state_locked(reason=reason)

    async def _reconcile_current_state_locked(self, *, reason: str) -> None:
        catalog_observation = await observe_prefix_current(
            self._discovery_state,
            _ACTION_PROVIDER_CATALOG_PREFIX,
            known_keys=(
                action_provider_catalog_key(provider_instance_id)
                for provider_instance_id in self._catalogs
            ),
        )
        presence_observation = await observe_prefix_current(
            self._lease_state,
            _ACTION_PROVIDER_PRESENCE_PREFIX,
            known_keys=(
                presence_endpoint_key(
                    lane=ACTIONS_LANE,
                    endpoint=action_provider_address(provider_instance_id),
                )
                for provider_instance_id in self._provider_presence_sessions
            ),
        )

        affected_providers = set(self._catalogs)
        next_catalogs = dict(self._catalogs)

        for key in catalog_observation.confirmed_missing:
            provider_instance_id = parse_action_provider_catalog_key(key)
            if provider_instance_id is not None:
                affected_providers.add(provider_instance_id)
                next_catalogs.pop(provider_instance_id, None)

        for entry in catalog_observation.entries:
            provider_instance_id = parse_action_provider_catalog_key(entry.key)
            if provider_instance_id is None or not (
                _is_allowed_provider_instance_id(provider_instance_id)
            ):
                continue
            affected_providers.add(provider_instance_id)
            catalog = _valid_catalog(entry, provider_instance_id=provider_instance_id)
            if catalog is not None:
                next_catalogs[provider_instance_id] = catalog
            else:
                next_catalogs.pop(provider_instance_id, None)

        next_presence_sessions = dict(self._provider_presence_sessions)
        for key in presence_observation.confirmed_missing:
            parsed = parse_presence_endpoint_key(key)
            if parsed is None:
                continue
            lane, endpoint = parsed
            if lane == ACTIONS_LANE and endpoint.family == "action_provider":
                affected_providers.add(endpoint.endpoint_id)
                next_presence_sessions.pop(endpoint.endpoint_id, None)

        for entry in presence_observation.entries:
            parsed = parse_presence_endpoint_key(entry.key)
            if parsed is None:
                continue
            lane, endpoint = parsed
            if lane != ACTIONS_LANE or endpoint.family != "action_provider":
                continue
            if not _is_allowed_provider_instance_id(endpoint.endpoint_id):
                continue
            affected_providers.add(endpoint.endpoint_id)
            presence = _valid_provider_presence(entry)
            if presence is None:
                next_presence_sessions.pop(endpoint.endpoint_id, None)
            else:
                next_presence_sessions[endpoint.endpoint_id] = presence.session_id

        self._catalogs = next_catalogs
        self._provider_presence_sessions = next_presence_sessions
        for provider_instance_id in sorted(affected_providers):
            await self._reconcile_provider(provider_instance_id, reason=reason)

    async def _reconcile_provider(
        self,
        provider_instance_id: str,
        *,
        reason: str,
    ) -> None:
        desired = self._desired_provider_actions(provider_instance_id)
        existing = {
            qualified: entry
            for qualified, entry in self._action_registry.items()
            if entry.provider_instance_id == provider_instance_id
        }
        changed = {
            qualified
            for qualified, entry in desired.items()
            if existing.get(qualified) != entry
        }
        registered = sorted(
            qualified
            for qualified in changed
        )
        unregistered = sorted((set(existing) - set(desired)) | changed)

        for qualified in unregistered:
            self._action_registry.pop(qualified, None)
        self._action_registry.update(desired)

        if registered or unregistered:
            logger.info(
                "Action provider %s catalog changed via %s: +%s -%s",
                provider_instance_id,
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

    def _desired_provider_actions(
        self,
        provider_instance_id: str,
    ) -> dict[str, _CatalogAction]:
        catalog = self._catalogs.get(provider_instance_id)
        if catalog is None:
            return {}
        if (
            self._provider_presence_sessions.get(provider_instance_id)
            != catalog.session_id
        ):
            return {}
        return {
            _qualified_id(provider_instance_id, descriptor.action_id): _CatalogAction(
                provider_instance_id=provider_instance_id,
                provider_id=descriptor.provider_id or catalog.provider_id,
                session_id=catalog.session_id,
                labels=dict(catalog.labels),
                descriptor=descriptor,
            )
            for descriptor in catalog.actions.values()
            if descriptor.action_id
        }

    async def _publish_actions_changed(self, event: ActionsChangedEvent) -> None:
        if self._on_actions_changed is not None:
            await self._on_actions_changed(event)


def _metadata(entry: _CatalogAction) -> ActionMetadata:
    descriptor = entry.descriptor
    return ActionMetadata(
        uuid=descriptor.action_id,
        provider_instance_id=entry.provider_instance_id,
        provider_id=entry.provider_id,
        name=descriptor.name,
        catalog_session_id=entry.session_id,
        provider_labels=dict(entry.labels),
        settings_schema=(
            thaw_json(descriptor.settings_schema)
            if descriptor.settings_schema is not None
            else None
        ),
        provider_settings_schema=(
            thaw_json(descriptor.provider_settings_schema)
            if descriptor.provider_settings_schema is not None
            else None
        ),
    )


def _labels_match(
    actual: Mapping[str, str],
    required: Mapping[str, str] | None,
) -> bool:
    if not required:
        return True
    return all(actual.get(key) == value for key, value in required.items())


def _is_allowed_provider_instance_id(provider_instance_id: str) -> bool:
    if provider_instance_id in RESERVED_BUILTIN_PROVIDER_IDS:
        logger.warning(
            "Ignoring external action provider using reserved provider id %s",
            provider_instance_id,
        )
        return False
    return True


def _valid_catalog(
    entry: StateEntry,
    *,
    provider_instance_id: str,
) -> ActionProviderCatalog | None:
    try:
        catalog = ActionProviderCatalog.model_validate(entry.value)
    except ValueError:
        logger.warning("Ignoring invalid action provider catalog %s", entry.key)
        return None
    if entry.key != action_provider_catalog_key(provider_instance_id):
        logger.warning("Ignoring action provider catalog with mismatched key %s", entry.key)
        return None
    expected_endpoint = action_provider_address(provider_instance_id)
    if (
        catalog.provider_instance_id != provider_instance_id
        or catalog.provider_endpoint != expected_endpoint
    ):
        logger.warning(
            "Ignoring action provider catalog %s with mismatched payload "
            "provider=%s endpoint=%s",
            entry.key,
            catalog.provider_instance_id,
            catalog.provider_endpoint,
        )
        return None
    return catalog


def _valid_provider_presence(entry: StateEntry) -> EndpointPresence | None:
    parsed = parse_presence_endpoint_key(entry.key)
    if parsed is None:
        return None
    lane, endpoint = parsed
    try:
        presence = EndpointPresence.model_validate(entry.value)
    except ValueError:
        logger.warning("Ignoring invalid action provider presence %s", entry.key)
        return None
    if (
        lane != ACTIONS_LANE
        or endpoint.family != "action_provider"
        or presence.lane != lane
        or presence.endpoint != endpoint
    ):
        logger.warning(
            "Ignoring action provider presence %s with mismatched payload",
            entry.key,
        )
        return None
    return presence
