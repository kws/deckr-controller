"""Action instance and provider runtime lifecycle handling for bindings."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

import anyio
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_INSTANCE_DESTROYED,
    PAGE_SESSION_CLOSED,
    PAGE_SESSION_OPENED,
    ActionInstanceLifecycleBody,
    ActionInstanceMetadata,
    ActionLifecycleRejectedBody,
    BindingMetadata,
    PageSessionLifecycleBody,
    PageSessionMetadata,
    subject_action_instance_id,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage

from deckr.controller._actions import (
    ActionIntentKey,
    ActionMetadata,
    ProviderActionKey,
    ProviderSessionKey,
    provider_session_key,
)
from deckr.controller._bindings._attachments import (
    AuthorizedCommandTarget,
    BindingLease,
)
from deckr.controller._bindings._context import RuntimeMessageSender
from deckr.controller._bindings._ports import LifecycleAvailabilityRecorder
from deckr.controller._pages import DynamicPageSession
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID

logger = logging.getLogger(__name__)

BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS = 1.0
_TERMINAL_LIFECYCLE_REJECTION_REASONS = frozenset(
    {
        "invalid_settings",
        "unsupported_capability",
        "permission_denied",
    }
)


@dataclass(frozen=True, slots=True)
class ActionInstanceSnapshot:
    provider_instance_id: str
    provider_id: str
    action_id: str
    action_instance_id: str
    config_id: str
    context_id: str


class BindingLifecycleHost(Protocol):
    config_id: str

    def current_contract(
        self,
        key: ProviderSessionKey | None,
    ) -> ContractPointer | None: ...

    def provider_session_key_for_session(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None,
    ) -> ProviderSessionKey | None: ...

    def page_session_metadata(
        self,
        session: DynamicPageSession,
    ) -> PageSessionMetadata: ...

    def active_page_session(self) -> DynamicPageSession | None: ...

    def binding_by_id(self, binding_id: str) -> BindingLease | None: ...

    def iter_binding_leases(self) -> Iterable[BindingLease]: ...

    def planned_intent_for_lease(self, lease: BindingLease) -> ActionIntentKey: ...

    async def message_contract_authorized(
        self,
        msg: DeckrMessage,
        key: ProviderSessionKey | None,
    ) -> bool: ...

    async def revoke_binding(
        self,
        binding_id: str,
        *,
        clear_output: bool = True,
        notify_provider: bool = True,
        reason: str = "detach",
        clear_held_input: bool = False,
    ) -> BindingLease | None: ...

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None: ...

    async def on_action_availability_changed(
        self,
        changed_keys: Iterable[ProviderActionKey] = (),
    ) -> None: ...


def _action_instance_snapshot(
    metadata: ActionInstanceMetadata,
) -> ActionInstanceSnapshot:
    return ActionInstanceSnapshot(
        provider_instance_id=metadata.provider_instance_id,
        provider_id=metadata.provider_id,
        action_id=metadata.action_id,
        action_instance_id=metadata.action_instance_id,
        config_id=metadata.config_id,
        context_id=metadata.context_id,
    )


def _action_instance_matches_metadata(
    stored: ActionInstanceMetadata,
    metadata: ActionInstanceMetadata,
) -> bool:
    return (
        stored.provider_instance_id == metadata.provider_instance_id
        and stored.provider_id == metadata.provider_id
        and stored.action_id == metadata.action_id
        and stored.action_instance_id == metadata.action_instance_id
        and stored.config_id == metadata.config_id
        and stored.context_id == metadata.context_id
    )


def _action_instance_matches_action(
    stored: ActionInstanceMetadata,
    action_meta: ActionMetadata,
    *,
    config_id: str,
) -> bool:
    return (
        stored.provider_instance_id == action_meta.provider_instance_id
        and stored.provider_id == action_meta.provider_id
        and stored.action_id == action_meta.uuid
        and stored.config_id == config_id
    )


def _binding_body_matches_lease(lease: BindingLease, binding: BindingMetadata) -> bool:
    return (
        binding.provider_instance_id == lease.provider_instance_id
        and binding.provider_id == lease.provider_id
        and binding.action_id == lease.action_uuid
        and binding.context_id == lease.context_id
        and binding.binding_id == lease.binding_id
        and binding.action_instance_id == lease.action_instance_id
    )


def _page_session_matches_metadata(
    session: DynamicPageSession,
    metadata: PageSessionMetadata,
) -> bool:
    return (
        metadata.provider_instance_id == session.owner_provider_instance_id
        and metadata.provider_id == session.owner_provider_id
        and metadata.action_instance_id == session.action_instance_id
        and metadata.page_id == session.page_id
        and metadata.page_session_id == session.page_session_id
        and metadata.context_id == session.context_id
        and metadata.owner_binding_id == session.owner_binding_id
    )


class ActionInstanceLifecycleService:
    def __init__(
        self,
        *,
        config_id: str,
        runtime_sender: RuntimeMessageSender,
        availability_recorder: LifecycleAvailabilityRecorder,
        host: BindingLifecycleHost,
        clock: Callable[[], float],
    ) -> None:
        self._config_id = config_id
        self._runtime_sender = runtime_sender
        self._availability_recorder = availability_recorder
        self._host = host
        self._clock = clock
        self._action_instances: dict[str, ActionInstanceMetadata] = {}
        self._action_instance_providers: dict[str, str] = {}
        self._action_instance_provider_sessions: dict[
            str,
            ProviderSessionKey | None,
        ] = {}

    def snapshot_action_instances(self) -> Mapping[str, ActionInstanceSnapshot]:
        return MappingProxyType(
            {
                action_instance_id: _action_instance_snapshot(metadata)
                for action_instance_id, metadata in self._action_instances.items()
            }
        )

    def provider_session_keys(self) -> Mapping[str, ProviderSessionKey | None]:
        return MappingProxyType(dict(self._action_instance_provider_sessions))

    def has_action_instance(self, action_instance_id: str) -> bool:
        return action_instance_id in self._action_instances

    def context_id_for_action_instance(
        self,
        *,
        action_meta: ActionMetadata,
        action_instance_id: str,
    ) -> str | None:
        existing = self._action_instances.get(action_instance_id)
        if existing is not None and _action_instance_matches_action(
            existing,
            action_meta,
            config_id=self._config_id,
        ):
            return existing.context_id
        return None

    def move_action_instance_provider_session(
        self,
        action_instance_id: str,
        action_meta: ActionMetadata,
    ) -> None:
        self._action_instance_provider_sessions[action_instance_id] = (
            None
            if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            else provider_session_key(action_meta)
        )

    async def ensure_action_instance(
        self,
        *,
        action_meta: ActionMetadata,
        action_instance_id: str,
        context_id: str,
    ) -> None:
        existing = self._action_instances.get(action_instance_id)
        had_existing = existing is not None
        if existing is not None and _action_instance_matches_action(
            existing,
            action_meta,
            config_id=self._config_id,
        ):
            return
        if existing is not None:
            await self.destroy_action_instance(
                action_instance_id,
                reason="action_instance_retargeted",
            )
        metadata = ActionInstanceMetadata(
            providerInstanceId=action_meta.provider_instance_id,
            providerId=action_meta.provider_id,
            actionId=action_meta.uuid,
            actionInstanceId=action_instance_id,
            configId=self._config_id,
            contextId=context_id,
        )
        self._action_instances[action_instance_id] = metadata
        self._action_instance_providers[action_instance_id] = (
            action_meta.provider_instance_id
        )
        provider_session_key_for_action = (
            None
            if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            else provider_session_key(action_meta)
        )
        self._action_instance_provider_sessions[action_instance_id] = (
            provider_session_key_for_action
        )
        try:
            await self._publish_action_instance_created(
                metadata=metadata,
                provider_session_key_for_action=provider_session_key_for_action,
            )
        except BaseException:
            if not had_existing:
                self._action_instances.pop(action_instance_id, None)
                self._action_instance_providers.pop(action_instance_id, None)
                self._action_instance_provider_sessions.pop(action_instance_id, None)
            raise

    async def _publish_action_instance_created(
        self,
        *,
        metadata: ActionInstanceMetadata,
        provider_session_key_for_action: ProviderSessionKey | None,
    ) -> None:
        if metadata.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        contract = self._host.current_contract(provider_session_key_for_action)
        if contract is None:
            logger.warning(
                "Skipping action instance create without live provider-session "
                "contract config=%s actionInstance=%s provider=%s",
                self._config_id,
                metadata.action_instance_id,
                metadata.provider_instance_id,
            )
            return
        sent = await self._runtime_sender.send_action_runtime_message(
            provider_session_key=provider_session_key_for_action,
            message_type=ACTION_INSTANCE_CREATED,
            body=ActionInstanceLifecycleBody(metadata=metadata),
        )
        if not sent:
            logger.warning(
                "Skipping action instance create without live Action Runtime lease "
                "config=%s actionInstance=%s provider=%s",
                self._config_id,
                metadata.action_instance_id,
                metadata.provider_instance_id,
            )

    async def destroy_action_instance(
        self,
        action_instance_id: str,
        *,
        reason: str,
        notify_provider: bool = True,
    ) -> None:
        metadata = self._action_instances.pop(action_instance_id, None)
        provider_instance_id = self._action_instance_providers.pop(
            action_instance_id,
            None,
        )
        provider_session_key_for_action = self._action_instance_provider_sessions.pop(
            action_instance_id,
            None,
        )
        if (
            metadata is None
            or provider_instance_id is None
            or provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            or not notify_provider
        ):
            return
        contract = self._host.current_contract(provider_session_key_for_action)
        if contract is None:
            logger.warning(
                "Skipping action instance destroy without live provider-session "
                "contract config=%s actionInstance=%s provider=%s",
                self._config_id,
                metadata.action_instance_id,
                provider_instance_id,
            )
            return
        sent = await self._runtime_sender.send_action_runtime_message(
            provider_session_key=provider_session_key_for_action,
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
        for action_instance_id in list(self._action_instances):
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
                lease.action_uuid,
                lease.provider_instance_id,
                lease.binding_id,
                reason,
            )
            return
        metadata = self._action_instances.get(lease.action_instance_id)
        if metadata is None:
            return
        lease.stale_lifecycle_recoveries += 1
        logger.info(
            "Recovering binding lifecycle config=%s control=%s action=%s "
            "provider=%s binding=%s reason=%s",
            self._config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
            reason,
        )
        await self._publish_action_instance_created(
            metadata=metadata,
            provider_session_key_for_action=lease.provider_session_key,
        )
        with anyio.move_on_after(BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS) as scope:
            await lease.context.on_binding_attached()
        if scope.cancel_called:
            logger.warning(
                "Binding lifecycle recovery timed out config=%s control=%s action=%s "
                "provider=%s binding=%s timeout=%ss",
                self._config_id,
                lease.control_id,
                lease.action_uuid,
                lease.provider_instance_id,
                lease.binding_id,
                BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS,
            )

    async def emit_page_opened(
        self,
        session: DynamicPageSession,
        *,
        causation_id: str | None = None,
    ) -> None:
        del causation_id
        if session.owner_provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        session_key = self._host.provider_session_key_for_session(
            provider_instance_id=session.owner_provider_instance_id,
            provider_id=session.owner_provider_id,
            provider_session_id=session.owner_provider_session_id,
        )
        contract = self._host.current_contract(session_key)
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
            await self._runtime_sender.send_action_runtime_message(
                provider_session_key=session_key,
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
        del causation_id
        if session.owner_provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        session_key = self._host.provider_session_key_for_session(
            provider_instance_id=session.owner_provider_instance_id,
            provider_id=session.owner_provider_id,
            provider_session_id=session.owner_provider_session_id,
        )
        contract = self._host.current_contract(session_key)
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
            await self._runtime_sender.send_action_runtime_message(
                provider_session_key=session_key,
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

    async def handle_lifecycle_rejected(
        self,
        msg: DeckrMessage,
        body: ActionLifecycleRejectedBody,
        *,
        authorization: AuthorizedCommandTarget | None,
        sender_provider_instance_id: str | None,
        context_id: str,
    ) -> None:
        if body.reason == "stale_lifecycle":
            await self._handle_stale_lifecycle_rejected(
                body,
                authorization=authorization,
            )
            return

        if sender_provider_instance_id is None:
            return

        if body.target_kind == "action_instance":
            metadata = body.action_instance
            if metadata is None:
                return
            if not await self._action_instance_rejection_authorized(
                msg,
                sender_provider_instance_id=sender_provider_instance_id,
                metadata=metadata,
                context_id=context_id,
            ):
                logger.warning(
                    "Ignoring unauthorized action lifecycle rejection for action instance %s",
                    metadata.action_instance_id,
                )
                return
            if self._lifecycle_rejection_is_terminal(body):
                await self._reject_action_instance(metadata, reason=body.reason)
                return
            key = self._record_lifecycle_unavailable_for_action_instance(
                metadata,
                reason=body.reason,
            )
            await self._handle_nondestructive_lifecycle_rejection(key)
            return

        if authorization is None:
            return

        if body.target_kind == "binding":
            lease = authorization.binding
            metadata = body.binding
            if (
                lease is None
                or metadata is None
                or metadata.config_id != self._config_id
                or not _binding_body_matches_lease(lease, metadata)
            ):
                logger.warning(
                    "Ignoring action lifecycle rejection for mismatched binding"
                )
                return
            if self._lifecycle_rejection_is_terminal(body):
                await self._host.revoke_binding(
                    lease.binding_id,
                    clear_output=True,
                    notify_provider=False,
                    reason=body.reason,
                    clear_held_input=True,
                )
                return
            key = self._record_lifecycle_unavailable_for_binding(
                lease,
                reason=body.reason,
            )
            await self._handle_nondestructive_lifecycle_rejection(key)
            return

        if body.target_kind == "page_session":
            session = authorization.page_session
            metadata = body.page_session
            if (
                session is None
                or metadata is None
                or metadata.config_id != self._config_id
                or not _page_session_matches_metadata(session, metadata)
            ):
                logger.warning(
                    "Ignoring action lifecycle rejection for mismatched page session"
                )
                return
            if self._lifecycle_rejection_is_terminal(body):
                await self._close_rejected_page_session(session, reason=body.reason)
                return
            key = self._record_lifecycle_unavailable_for_page_session(
                session,
                reason=body.reason,
            )
            await self._handle_nondestructive_lifecycle_rejection(key)

    async def _handle_stale_lifecycle_rejected(
        self,
        body: ActionLifecycleRejectedBody,
        *,
        authorization: AuthorizedCommandTarget | None,
    ) -> None:
        if body.target_kind != "binding":
            logger.info(
                "Ignoring stale action lifecycle rejection config=%s target=%s",
                self._config_id,
                body.target_kind,
            )
            return
        if authorization is None:
            return
        lease = authorization.binding
        metadata = body.binding
        if (
            lease is None
            or metadata is None
            or metadata.config_id != self._config_id
            or not _binding_body_matches_lease(lease, metadata)
        ):
            logger.warning("Ignoring stale lifecycle rejection for mismatched binding")
            return
        await self.recover_binding_lifecycle(
            lease,
            reason="stale_lifecycle_rejected",
        )

    async def _action_instance_rejection_authorized(
        self,
        msg: DeckrMessage,
        *,
        sender_provider_instance_id: str,
        metadata: ActionInstanceMetadata,
        context_id: str,
    ) -> bool:
        if metadata.config_id != self._config_id or metadata.context_id != context_id:
            return False
        if metadata.provider_instance_id != sender_provider_instance_id:
            return False
        action_instance_id = subject_action_instance_id(msg.subject)
        if (
            action_instance_id is not None
            and action_instance_id != metadata.action_instance_id
        ):
            return False
        stored = self._action_instances.get(metadata.action_instance_id)
        if stored is None or not _action_instance_matches_metadata(stored, metadata):
            return False
        key = self._action_instance_provider_sessions.get(metadata.action_instance_id)
        return (
            key is not None
            and msg.sender_session_id == key.provider_session_id
            and await self._host.message_contract_authorized(msg, key)
        )

    def _lifecycle_rejection_is_terminal(
        self,
        body: ActionLifecycleRejectedBody,
    ) -> bool:
        if body.reason == "stale_lifecycle":
            return False
        if body.retryable:
            return False
        return body.reason in _TERMINAL_LIFECYCLE_REJECTION_REASONS

    def _record_lifecycle_unavailable_for_binding(
        self,
        lease: BindingLease,
        *,
        reason: str,
    ) -> ProviderActionKey:
        return self._availability_recorder.record_lifecycle_unavailable(
            provider_instance_id=lease.provider_instance_id,
            provider_id=lease.provider_id,
            provider_session_id=lease.provider_session_id,
            action_uuid=lease.action_uuid,
            reason=reason,
            intent=self._planned_intent_for_lease(lease),
            now=self._clock(),
        )

    def _record_lifecycle_unavailable_for_action_instance(
        self,
        metadata: ActionInstanceMetadata,
        *,
        reason: str,
    ) -> ProviderActionKey:
        session_key = self._action_instance_provider_sessions.get(
            metadata.action_instance_id
        )
        return self._availability_recorder.record_lifecycle_unavailable(
            provider_instance_id=metadata.provider_instance_id,
            provider_id=metadata.provider_id,
            action_uuid=metadata.action_id,
            provider_session_id=(
                session_key.provider_session_id if session_key is not None else None
            ),
            reason=reason,
            intent=ActionIntentKey(
                action_uuid=metadata.action_id,
                provider_instance_id=metadata.provider_instance_id,
                provider_labels=(),
            ),
            now=self._clock(),
        )

    def _record_lifecycle_unavailable_for_page_session(
        self,
        session: DynamicPageSession,
        *,
        reason: str,
    ) -> ProviderActionKey:
        return self._availability_recorder.record_lifecycle_unavailable(
            provider_instance_id=session.owner_provider_instance_id,
            provider_id=session.owner_provider_id,
            action_uuid=session.owner_action_uuid,
            provider_session_id=session.owner_provider_session_id,
            reason=reason,
            intent=ActionIntentKey(
                action_uuid=session.owner_action_uuid,
                provider_instance_id=session.owner_provider_instance_id,
                provider_labels=(),
            ),
            now=self._clock(),
        )

    def _planned_intent_for_lease(self, lease: BindingLease) -> ActionIntentKey:
        return self._host.planned_intent_for_lease(lease)

    async def _handle_nondestructive_lifecycle_rejection(
        self,
        key: ProviderActionKey,
    ) -> None:
        await self._host.on_action_availability_changed(frozenset({key}))

    async def _reject_action_instance(
        self,
        metadata: ActionInstanceMetadata,
        *,
        reason: str,
    ) -> None:
        page_session = self._host.active_page_session()
        if (
            page_session is not None
            and page_session.action_instance_id == metadata.action_instance_id
        ):
            await self._close_rejected_page_session(page_session, reason=reason)

        for lease in tuple(self._host.iter_binding_leases()):
            if lease.action_instance_id == metadata.action_instance_id:
                await self._host.revoke_binding(
                    lease.binding_id,
                    notify_provider=False,
                    reason=reason,
                    clear_held_input=True,
                )
        await self.destroy_action_instance(
            metadata.action_instance_id,
            reason=reason,
            notify_provider=False,
        )

    async def _close_rejected_page_session(
        self,
        session: DynamicPageSession,
        *,
        reason: str,
    ) -> None:
        await self._host.close_page(context_id=session.context_id, reason=reason)
