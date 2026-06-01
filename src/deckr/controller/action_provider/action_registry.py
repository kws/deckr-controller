"""Action registry backed by Beacon action-provider advertisements."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import anyio
from deckr.actions.endpoints import (
    BUILTIN_ACTION_PROVIDER_ID,
    RESERVED_BUILTIN_PROVIDER_IDS,
)
from deckr.actions.messages import ActionDescriptor
from deckr.beacon import BeaconService, Candidate
from deckr.components import BaseComponent, RunContext
from deckr.contracts.models import thaw_json
from deckr.core.util.anyio import CoalescedTrigger
from deckr.profiles import (
    ACTIONS_FEATURE_ID,
    ActionsBeaconPayload,
    actions_payload_from_advertisement,
)
from deckr.state import (
    DEFAULT_STATE_NOTIFICATION_BATCH_SECONDS,
    DEFAULT_STATE_RECONCILE_SECONDS,
    StateUnavailable,
)

from deckr.controller.action_provider.builtin import (
    BuiltinAction,
    BuiltinRegistry,
)
from deckr.controller.action_provider.events import ActionsChangedEvent
from deckr.controller.action_provider.provider import ActionMetadata

logger = logging.getLogger(__name__)

_STATE_RECONCILE_SECONDS = DEFAULT_STATE_RECONCILE_SECONDS
_STATE_NOTIFICATION_BATCH_SECONDS = DEFAULT_STATE_NOTIFICATION_BATCH_SECONDS
_WATCH_RETRY_SECONDS = 1.0


def _qualified_id(provider_instance_id: str, action_uuid: str) -> str:
    return f"{provider_instance_id}::{action_uuid}"


@dataclass(frozen=True, slots=True)
class _BeaconAction:
    provider_instance_id: str
    provider_id: str
    session_id: str
    labels: Mapping[str, str]
    descriptor: ActionDescriptor


class ActionRegistry(BaseComponent):
    """Resolve builtin actions and provider actions advertised through Beacon."""

    def __init__(
        self,
        beacon: BeaconService,
        *,
        controller_id: str,
        on_actions_changed: Callable[[ActionsChangedEvent], Awaitable[None]] | None = None,
        notification_batch_interval: float = _STATE_NOTIFICATION_BATCH_SECONDS,
    ):
        super().__init__(name="ActionRegistry")
        del controller_id
        self._beacon = beacon
        self._on_actions_changed = on_actions_changed
        self._builtin_registry = BuiltinRegistry()
        self._builtin_action_registry: dict[str, ActionDescriptor] = {}
        self._action_registry: dict[str, _BeaconAction] = {}
        self._advertisements: dict[str, ActionsBeaconPayload] = {}
        self._reconcile_lock = anyio.Lock()
        self._reconcile_notifications = CoalescedTrigger(
            batch_interval=notification_batch_interval
        )

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
        advertisement = self._advertisements.get(provider_instance_id)
        return advertisement.session_id if advertisement is not None else None

    def _builtin_action_metadata(self, action_uuid: str) -> ActionMetadata | None:
        descriptor = self._builtin_action_registry.get(action_uuid)
        if descriptor is None:
            return None
        return _metadata(
            _BeaconAction(
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

        start_soon(self._advertisement_loop)
        start_soon(self._notification_reconciliation_loop)
        start_soon(self._reconciliation_loop)

    async def stop(self) -> None:
        self._action_registry.clear()
        self._builtin_action_registry.clear()
        self._advertisements.clear()
        await self._reconcile_notifications.aclose()

    async def _advertisement_loop(self) -> None:
        while True:
            try:
                async with self._beacon.watch_feature(ACTIONS_FEATURE_ID) as stream:
                    async for event in stream:
                        await self._reconcile_notifications.request(
                            f"actions beacon {event.event_type.value} {event.key}"
                        )
            except StateUnavailable:
                logger.warning(
                    "Action Beacon advertisements unavailable; retrying",
                    exc_info=True,
                )
                await anyio.sleep(_WATCH_RETRY_SECONDS)

    async def _reconciliation_loop(self) -> None:
        while True:
            try:
                await self._reconcile_current_state(reason="broker snapshot")
            except StateUnavailable:
                logger.warning(
                    "Action Beacon advertisements unavailable; reconciliation will retry",
                    exc_info=True,
                )
            await anyio.sleep(_STATE_RECONCILE_SECONDS)

    async def _notification_reconciliation_loop(self) -> None:
        await self._reconcile_notifications.run(
            self._reconcile_notification,
            reason_prefix="action beacon notifications",
        )

    async def _reconcile_notification(self, reason: str) -> None:
        try:
            await self._reconcile_current_state(reason=reason)
        except StateUnavailable:
            logger.warning(
                "Action Beacon advertisements unavailable; notification will retry",
                exc_info=True,
            )

    async def _reconcile_current_state(self, *, reason: str) -> None:
        async with self._reconcile_lock:
            await self._reconcile_current_state_locked(reason=reason)

    async def _reconcile_current_state_locked(self, *, reason: str) -> None:
        candidates = await self._beacon.find(ACTIONS_FEATURE_ID)
        next_candidates: dict[str, tuple[Candidate, ActionsBeaconPayload]] = {}

        for candidate in candidates:
            payload = _valid_actions_payload(candidate)
            if payload is None:
                continue
            provider_instance_id = payload.provider_instance_id
            if not _is_allowed_provider_instance_id(provider_instance_id):
                continue
            current = next_candidates.get(provider_instance_id)
            if current is None or candidate.revision > current[0].revision:
                next_candidates[provider_instance_id] = (candidate, payload)

        next_advertisements = {
            provider_instance_id: payload
            for provider_instance_id, (_candidate, payload) in next_candidates.items()
        }

        affected_providers = set(self._advertisements) | set(next_advertisements)
        self._advertisements = next_advertisements
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
        registered = sorted(changed)
        unregistered = sorted((set(existing) - set(desired)) | changed)

        for qualified in unregistered:
            self._action_registry.pop(qualified, None)
        self._action_registry.update(desired)

        if registered or unregistered:
            logger.debug(
                "Action provider %s Beacon advertisement changed via %s: +%s -%s",
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
    ) -> dict[str, _BeaconAction]:
        advertisement = self._advertisements.get(provider_instance_id)
        if advertisement is None:
            return {}
        return {
            _qualified_id(provider_instance_id, descriptor.action_id): _BeaconAction(
                provider_instance_id=provider_instance_id,
                provider_id=descriptor.provider_id or advertisement.provider_id,
                session_id=advertisement.session_id,
                labels=dict(advertisement.labels),
                descriptor=descriptor,
            )
            for descriptor in advertisement.actions.values()
            if descriptor.action_id
        }

    async def _publish_actions_changed(self, event: ActionsChangedEvent) -> None:
        if self._on_actions_changed is not None:
            await self._on_actions_changed(event)


def _metadata(entry: _BeaconAction) -> ActionMetadata:
    descriptor = entry.descriptor
    return ActionMetadata(
        uuid=descriptor.action_id,
        provider_instance_id=entry.provider_instance_id,
        provider_id=entry.provider_id,
        name=descriptor.name,
        provider_session_id=entry.session_id,
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


def _valid_actions_payload(candidate: Candidate) -> ActionsBeaconPayload | None:
    try:
        return actions_payload_from_advertisement(candidate.advertisement)
    except ValueError:
        logger.warning("Ignoring invalid actions Beacon advertisement %s", candidate.key)
        return None
