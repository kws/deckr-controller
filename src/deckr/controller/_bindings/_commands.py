"""Inbound action-provider command handling for controller bindings."""

from __future__ import annotations

import logging
from typing import Protocol

from deckr.action_runtime import action_runtime_provider_instance_id
from deckr.actions.endpoints import (
    RESERVED_BUILTIN_PROVIDER_IDS,
)
from deckr.actions.messages import (
    ActionLifecycleRejectedBody,
    BindingMetadata,
    BindingOutputBody,
    BindingOverlayBody,
    BindingOverlayClearBody,
    DynamicPageCommand,
    PageSessionMetadata,
)
from deckr.contracts.messages import DeckrMessage, parse_service_address

from deckr.controller._actions import ProviderSessionKey
from deckr.controller._actions._routing import ActionRuntimeCommand, CommandMetadata
from deckr.controller._bindings._action_lifecycle import (
    ActionInstanceLifecycleService,
)
from deckr.controller._bindings._attachments import (
    AuthorizedCommandTarget,
    BindingLease,
)
from deckr.controller._bindings._output_commands import BindingOutputCommands
from deckr.controller._pages import DynamicPageSession

logger = logging.getLogger(__name__)


class ProviderCommandHost(Protocol):
    config_id: str

    def binding_by_id(self, binding_id: str) -> BindingLease | None: ...

    def active_page_session(self) -> DynamicPageSession | None: ...

    def binding_command_authorized(self, lease: BindingLease) -> bool: ...

    def binding_output_authorized(self, lease: BindingLease) -> bool: ...

    def provider_session_key_for_session(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None,
    ) -> ProviderSessionKey | None: ...

    async def message_contract_authorized(
        self,
        msg: DeckrMessage,
        key: ProviderSessionKey | None,
    ) -> bool: ...

    async def recover_binding_provider_session_contract(
        self,
        lease: BindingLease,
        *,
        reason: str,
    ) -> None: ...

    async def render_direct_output(self, lease, body, message) -> None: ...

    async def open_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        binding_id: str | None = None,
        causation_id: str | None = None,
    ) -> DynamicPageSession | None: ...

    async def replace_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        causation_id: str | None = None,
    ) -> None: ...

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None: ...


class ProviderCommandIngress:
    def __init__(
        self,
        *,
        host: ProviderCommandHost,
        lifecycle: ActionInstanceLifecycleService,
        logger: logging.Logger = logger,
    ) -> None:
        self._outputs = BindingOutputCommands(host=host, logger=logger)
        self._host = host
        self._lifecycle = lifecycle
        self._logger = logger

    async def handle(self, command: ActionRuntimeCommand) -> None:
        """Dispatch a decoded command without changing or reparsing its envelope."""
        msg, body, metadata = command.envelope, command.body, command.metadata
        context_id = metadata.context_id if metadata is not None else ""
        if not context_id:
            return
        config_id = metadata.config_id if metadata is not None else None
        if config_id != self._host.config_id:
            return

        if isinstance(body, ActionLifecycleRejectedBody):
            authorization: AuthorizedCommandTarget | None = None
            sender_provider_instance_id: str | None = None
            if body.target_kind == "action_instance":
                sender_provider_instance_id = self._command_sender_provider_instance_id(
                    msg
                )
            elif body.reason != "stale_lifecycle" or body.target_kind == "binding":
                authorization = await self._authorize_action_command(
                    msg,
                    metadata=metadata,
                    context_id=context_id,
                )
                if authorization is not None:
                    sender_provider_instance_id = (
                        authorization.sender_provider_instance_id
                    )
            await self._lifecycle.rejections.handle(
                msg,
                body,
                authorization=authorization,
                sender_provider_instance_id=sender_provider_instance_id,
                context_id=context_id,
            )
            return

        authorization = await self._authorize_action_command(
            msg,
            metadata=metadata,
            context_id=context_id,
        )
        if authorization is None:
            return

        if isinstance(body, BindingOutputBody):
            if authorization.binding is not None:
                await self._outputs._handle_binding_output(
                    authorization.binding, body, msg
                )
            return

        if isinstance(body, BindingOverlayBody):
            if authorization.binding is not None:
                await self._outputs._handle_binding_overlay(
                    authorization.binding, body, msg
                )
            return

        if isinstance(body, BindingOverlayClearBody):
            if authorization.binding is not None:
                await self._outputs._handle_binding_overlay_clear(
                    authorization.binding,
                    body,
                    msg,
                )
            return

        if authorization.page_session is not None:
            return

    def _command_sender_provider_instance_id(
        self,
        msg: DeckrMessage,
    ) -> str | None:
        service_id = parse_service_address(msg.sender)
        provider_instance_id = (
            action_runtime_provider_instance_id(service_id) if service_id else None
        )
        if provider_instance_id is None:
            self._logger.warning(
                "Ignoring action command %s from non-provider sender %s",
                msg.message_type,
                msg.sender,
            )
            return None
        if provider_instance_id in RESERVED_BUILTIN_PROVIDER_IDS:
            self._logger.warning(
                "Ignoring action command %s from external provider using reserved id %s",
                msg.message_type,
                provider_instance_id,
            )
            return None
        return provider_instance_id

    async def _authorize_action_command(
        self,
        msg: DeckrMessage,
        *,
        context_id: str,
        metadata: CommandMetadata | None,
    ) -> AuthorizedCommandTarget | None:
        sender_provider_instance_id = self._command_sender_provider_instance_id(msg)
        if sender_provider_instance_id is None:
            return None

        if metadata is None:
            return None
        action_instance_id = metadata.action_instance_id
        binding_id = (
            metadata.binding_id if isinstance(metadata, BindingMetadata) else None
        )
        page_session_id = (
            metadata.page_session_id
            if isinstance(metadata, (BindingMetadata, PageSessionMetadata))
            else None
        )
        metadata_provider_id = metadata.provider_instance_id
        if (
            metadata_provider_id is not None
            and metadata_provider_id != sender_provider_instance_id
        ):
            self._logger.warning(
                "Ignoring action command %s from %s with mismatched metadata provider %s",
                msg.message_type,
                msg.sender,
                metadata_provider_id,
            )
            return None

        if binding_id is not None:
            lease = self._host.binding_by_id(binding_id)
            if lease is None or lease.context_id != context_id:
                self._logger.debug(
                    "Ignoring action command %s from %s for inactive binding %s",
                    msg.message_type,
                    msg.sender,
                    binding_id,
                )
                return None
            if not self._host.binding_command_authorized(lease):
                self._logger.warning(
                    "Ignoring action command %s for unauthorized binding %s",
                    msg.message_type,
                    binding_id,
                )
                return None
            if sender_provider_instance_id != lease.provider_instance_id:
                self._logger.warning(
                    "Ignoring action command %s from provider %s for binding owned by provider %s",
                    msg.message_type,
                    sender_provider_instance_id,
                    lease.provider_instance_id,
                )
                return None
            if msg.sender_session_id != lease.provider_session_id:
                self._logger.warning(
                    "Ignoring action command %s from stale provider session %s",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if not await self._host.message_contract_authorized(
                msg,
                lease.provider_session_key,
            ):
                await self._host.recover_binding_provider_session_contract(
                    lease,
                    reason=f"{msg.message_type}_contract_mismatch",
                )
                self._logger.warning(
                    "Ignoring action command %s from provider session %s without "
                    "matching Concord contract",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if (
                action_instance_id is not None
                and action_instance_id != lease.action_instance_id
            ):
                self._logger.warning(
                    "Ignoring action command %s for mismatched action instance %s",
                    msg.message_type,
                    action_instance_id,
                )
                return None
            if page_session_id is not None and page_session_id != lease.page_session_id:
                self._logger.warning(
                    "Ignoring action command %s for mismatched page session %s",
                    msg.message_type,
                    page_session_id,
                )
                return None
            return AuthorizedCommandTarget(
                sender_provider_instance_id=sender_provider_instance_id,
                context_id=context_id,
                binding=lease,
            )

        session = self._host.active_page_session()
        if page_session_id is not None:
            if (
                session is None
                or page_session_id != session.page_session_id
                or context_id != session.context_id
            ):
                self._logger.warning(
                    "Ignoring action command %s for inactive page session %s",
                    msg.message_type,
                    page_session_id,
                )
                return None
            if sender_provider_instance_id != session.owner_provider_instance_id:
                self._logger.warning(
                    "Ignoring action command %s from provider %s for page owned by provider %s",
                    msg.message_type,
                    sender_provider_instance_id,
                    session.owner_provider_instance_id,
                )
                return None
            if msg.sender_session_id != session.owner_provider_session_id:
                self._logger.warning(
                    "Ignoring page action command %s from stale provider session %s",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if not await self._host.message_contract_authorized(
                msg,
                self._host.provider_session_key_for_session(
                    provider_instance_id=session.owner_provider_instance_id,
                    provider_id=session.owner_provider_id,
                    provider_session_id=session.owner_provider_session_id,
                ),
            ):
                self._logger.warning(
                    "Ignoring page action command %s from provider session %s "
                    "without matching Concord contract",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if (
                action_instance_id is not None
                and action_instance_id != session.action_instance_id
            ):
                self._logger.warning(
                    "Ignoring action command %s for mismatched page action instance %s",
                    msg.message_type,
                    action_instance_id,
                )
                return None
            return AuthorizedCommandTarget(
                sender_provider_instance_id=sender_provider_instance_id,
                context_id=context_id,
                page_session=session,
            )

        self._logger.warning(
            "Ignoring action command %s from %s without binding or page session subject",
            msg.message_type,
            msg.sender,
        )
        return None
