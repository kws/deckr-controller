"""Action instance and provider runtime lifecycle handling for bindings."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType

from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_INSTANCE_DESTROYED,
    PAGE_SESSION_CLOSED,
    PAGE_SESSION_OPENED,
    ActionInstanceLifecycleBody,
    ActionInstanceMetadata,
    PageSessionLifecycleBody,
)

from deckr.controller._actions import (
    ActionMetadata,
    ProviderSessionKey,
    provider_session_key,
)
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._context import RuntimeMessageSender
from deckr.controller._bindings._lifecycle_rejections import LifecycleRejections
from deckr.controller._bindings._lifecycle_state import (
    ActionInstanceRecord,
    ActionInstanceSnapshot,
    BindingLifecycleHost,
    _action_instance_matches_action,
    _action_instance_snapshot,
)
from deckr.controller._bindings._ports import LifecycleAvailabilityRecorder
from deckr.controller._pages import DynamicPageSession
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID

logger = logging.getLogger(__name__)


class ActionInstanceLifecycleService:
    def __init__(
        self,
        *,
        config_id: str,
        runtime_sender: RuntimeMessageSender,
        availability_recorder: LifecycleAvailabilityRecorder,
        host: BindingLifecycleHost,
        clock: Callable[[], float],
        notification_sender: RuntimeMessageSender | None = None,
        schedule_recovery: Callable[[BindingLease], Awaitable[None]],
    ) -> None:
        self._schedule_recovery = schedule_recovery
        self._config_id = config_id
        self._runtime_sender = runtime_sender
        self._notification_sender = notification_sender or runtime_sender
        self._availability_recorder = availability_recorder
        self._host = host
        self._clock = clock
        self._instances: dict[str, ActionInstanceRecord] = {}
        self.rejections = LifecycleRejections(
            config_id=config_id,
            instances=self,
            host=host,
            availability_recorder=availability_recorder,
            clock=clock,
        )

    def snapshot_action_instances(self) -> Mapping[str, ActionInstanceSnapshot]:
        return MappingProxyType(
            {
                action_instance_id: _action_instance_snapshot(record.metadata)
                for action_instance_id, record in self._instances.items()
            }
        )

    def provider_session_keys(self) -> Mapping[str, ProviderSessionKey | None]:
        return MappingProxyType(
            {
                key: record.provider_session_key
                for key, record in self._instances.items()
            }
        )

    def has_action_instance(self, action_instance_id: str) -> bool:
        return action_instance_id in self._instances

    def context_id_for_action_instance(
        self,
        *,
        action_meta: ActionMetadata,
        action_instance_id: str,
    ) -> str | None:
        existing = self.instance_metadata(action_instance_id)
        if existing is not None and _action_instance_matches_action(
            existing,
            action_meta,
            config_id=self._config_id,
        ):
            return existing.context_id
        return None

    def reserve_action_instance(
        self,
        *,
        action_meta: ActionMetadata,
        action_instance_id: str,
        context_id: str,
    ) -> ActionInstanceMetadata:
        """Record desired instance identity before starting adapter work."""
        existing = self.instance_metadata(action_instance_id)
        key = (
            None
            if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            else provider_session_key(action_meta)
        )
        if (
            existing is not None
            and _action_instance_matches_action(
                existing, action_meta, config_id=self._config_id
            )
            and self._instances[action_instance_id].provider_session_key == key
            and self._instances[action_instance_id].contract
            == self._host.current_contract(key)
        ):
            return existing
        metadata = ActionInstanceMetadata(
            providerInstanceId=action_meta.provider_instance_id,
            providerId=action_meta.provider_id,
            actionId=action_meta.action_id,
            actionInstanceId=action_instance_id,
            configId=self._config_id,
            contextId=context_id,
        )
        self._instances[action_instance_id] = ActionInstanceRecord(
            metadata=metadata,
            provider_session_key=key,
            contract=self._host.current_contract(key),
        )
        return metadata

    def instance_metadata(
        self, action_instance_id: str
    ) -> ActionInstanceMetadata | None:
        record = self._instances.get(action_instance_id)
        return record.metadata if record is not None else None

    def instance_is_current(self, metadata, key) -> bool:
        record = self._instances.get(metadata.action_instance_id)
        return bool(
            record is not None
            and record.metadata is metadata
            and record.provider_session_key == key
            and record.contract == self._host.current_contract(key)
        )

    def instance_started(self, metadata) -> bool:
        record = self._instances.get(metadata.action_instance_id)
        return bool(
            record is not None and record.metadata is metadata and record.started
        )

    def invalidate_instance_started(self, metadata) -> None:
        record = self._instances.get(metadata.action_instance_id)
        if record is not None and record.metadata is metadata:
            record.started = False

    def accept_instance_started(self, metadata, key) -> None:
        if self.instance_is_current(metadata, key):
            self._instances[metadata.action_instance_id].started = True

    async def start_reserved_instance(self, metadata, key) -> bool:
        """Execute only the exact reserved identity; acceptance is a separate transition."""
        if not self.instance_is_current(metadata, key):
            return False
        record = self._instances[metadata.action_instance_id]
        if metadata.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return True
        return await self._runtime_sender.send_action_runtime_message(
            provider_session_key=key,
            contract=record.contract,
            message_type=ACTION_INSTANCE_CREATED,
            body=ActionInstanceLifecycleBody(metadata=metadata),
        )

    async def destroy_action_instance(
        self,
        action_instance_id: str,
        *,
        reason: str,
        notify_provider: bool = True,
    ) -> None:
        record = self._instances.pop(action_instance_id, None)
        if record is None or not notify_provider:
            return
        metadata, contract = record.metadata, record.contract
        provider_instance_id = metadata.provider_instance_id
        provider_session_key_for_action = record.provider_session_key
        if provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        if (
            contract is None
            or self._host.current_contract(provider_session_key_for_action) != contract
        ):
            logger.warning(
                "Skipping action instance destroy without live provider-session "
                "contract config=%s actionInstance=%s provider=%s",
                self._config_id,
                metadata.action_instance_id,
                provider_instance_id,
            )
            return
        sent = await self._notification_sender.send_action_runtime_message(
            provider_session_key=provider_session_key_for_action,
            contract=contract,
            message_type=ACTION_INSTANCE_DESTROYED,
            body=ActionInstanceLifecycleBody(metadata=metadata, reason=reason),
        )
        if not sent:
            logger.warning(
                "Skipping action instance destroy without live Action Runtime lease "
                "config=%s actionInstance=%s provider=%s",
                self._config_id,
                metadata.action_instance_id,
                provider_instance_id,
            )

    async def destroy_all_action_instances(self, *, reason: str) -> None:
        for action_instance_id in tuple(self._instances):
            await self.destroy_action_instance(action_instance_id, reason=reason)

    async def recover_binding_lifecycle(
        self,
        lease: BindingLease,
        *,
        reason: str,
    ) -> None:
        if lease.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        if lease.stale_lifecycle_recoveries >= 1:
            logger.info(
                "Ignoring repeated stale lifecycle rejection config=%s control=%s "
                "action=%s provider=%s binding=%s reason=%s",
                self._config_id,
                lease.control_id,
                lease.action_id,
                lease.provider_instance_id,
                lease.binding_id,
                reason,
            )
            return
        metadata = self.instance_metadata(lease.action_instance_id)
        if metadata is None:
            return
        lease.stale_lifecycle_recoveries += 1
        logger.info(
            "Recovering binding lifecycle config=%s control=%s action=%s "
            "provider=%s binding=%s reason=%s",
            self._config_id,
            lease.control_id,
            lease.action_id,
            lease.provider_instance_id,
            lease.binding_id,
            reason,
        )
        await self._schedule_recovery(lease)

    async def emit_page_opened(
        self,
        session: DynamicPageSession,
        *,
        causation_id: str | None = None,
    ) -> None:
        if session.owner_provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        session_key = self._host.provider_session_key_for_session(
            provider_instance_id=session.owner_provider_instance_id,
            provider_id=session.owner_provider_id,
            provider_session_id=session.owner_provider_session_id,
        )
        contract = session.owner_contract
        if contract is None:
            logger.warning(
                "Skipping page open without live provider-session contract "
                "config=%s pageSession=%s provider=%s",
                self._config_id,
                session.page_session_id,
                session.owner_provider_instance_id,
            )
            return
        try:
            await self._notification_sender.send_action_runtime_message(
                provider_session_key=session_key,
                contract=contract,
                causation_id=causation_id,
                message_type=PAGE_SESSION_OPENED,
                body=PageSessionLifecycleBody(
                    pageSession=self._host.page_session_metadata(session)
                ),
            )
        except Exception:
            logger.exception(
                "Error notifying provider of page open config=%s pageSession=%s",
                self._config_id,
                session.page_session_id,
            )

    async def emit_page_closed(
        self,
        session: DynamicPageSession,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> None:
        if session.owner_provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        session_key = self._host.provider_session_key_for_session(
            provider_instance_id=session.owner_provider_instance_id,
            provider_id=session.owner_provider_id,
            provider_session_id=session.owner_provider_session_id,
        )
        contract = session.owner_contract
        if contract is None:
            logger.warning(
                "Skipping page close without live provider-session contract "
                "config=%s pageSession=%s provider=%s",
                self._config_id,
                session.page_session_id,
                session.owner_provider_instance_id,
            )
            return
        try:
            await self._notification_sender.send_action_runtime_message(
                provider_session_key=session_key,
                contract=contract,
                causation_id=causation_id,
                message_type=PAGE_SESSION_CLOSED,
                body=PageSessionLifecycleBody(
                    pageSession=self._host.page_session_metadata(session),
                    reason=reason,
                ),
            )
        except Exception:
            logger.exception(
                "Error notifying provider of page close config=%s pageSession=%s reason=%s",
                self._config_id,
                session.page_session_id,
                reason,
            )
