"""Action instance and provider runtime lifecycle handling for bindings."""

from __future__ import annotations

import logging

from deckr.actions.messages import (
    ActionInstanceMetadata,
    ActionLifecycleRejectedBody,
)
from deckr.contracts.messages import DeckrMessage

from deckr.controller._actions import (
    ActionIntentKey,
    ProviderActionKey,
)
from deckr.controller._bindings._attachments import (
    AuthorizedCommandTarget,
    BindingLease,
)
from deckr.controller._bindings._attachments import (
    binding_body_matches_lease as _binding_body_matches_lease,
)
from deckr.controller._bindings._lifecycle_state import (
    _action_instance_matches_metadata,
    _page_session_matches_metadata,
)
from deckr.controller._pages import DynamicPageSession

logger = logging.getLogger(__name__)

_TERMINAL_LIFECYCLE_REJECTION_REASONS = frozenset(
    {
        "invalid_settings",
        "unsupported_capability",
        "permission_denied",
    }
)


class LifecycleRejections:
    """Authorize rejection targets and apply controller recovery policy."""

    def __init__(self, *, config_id, instances, host, availability_recorder, clock):
        self._config_id = config_id
        self._instances = instances
        self._host = host
        self._availability_recorder = availability_recorder
        self._clock = clock

    async def handle(
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
        await self._instances.recover_binding_lifecycle(
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
        stored = self._instances.instance_metadata(metadata.action_instance_id)
        if stored is None or not _action_instance_matches_metadata(stored, metadata):
            return False
        key = self._instances.provider_session_keys().get(metadata.action_instance_id)
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
            action_id=lease.action_id,
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
        session_key = self._instances.provider_session_keys().get(
            metadata.action_instance_id
        )
        return self._availability_recorder.record_lifecycle_unavailable(
            provider_instance_id=metadata.provider_instance_id,
            provider_id=metadata.provider_id,
            action_id=metadata.action_id,
            provider_session_id=(
                session_key.provider_session_id if session_key is not None else None
            ),
            reason=reason,
            intent=ActionIntentKey(
                action_id=metadata.action_id,
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
            action_id=session.owner_action_id,
            provider_session_id=session.owner_provider_session_id,
            reason=reason,
            intent=ActionIntentKey(
                action_id=session.owner_action_id,
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
        await self._instances.destroy_action_instance(
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
