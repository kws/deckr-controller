"""Controller-owned action runtime service coordination."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deckr.controller._actions._catalog import ActionCatalog
    from deckr.controller._actions._supervisor import ActionRuntimeSupervisor


import logging

from deckr.action_runtime import (
    ACTION_RUNTIME_SERVICE_NAMESPACE,
    RUNTIME_TO_CONTROLLER_MESSAGES,
    action_runtime_message_name,
    action_runtime_payload,
    action_runtime_provider_instance_id,
    validate_action_runtime_message,
)
from deckr.actions.messages import (
    ActionMessageBody,
)
from deckr.actions.rendering import BindingRenderResultBody
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage, parse_service_address
from deckr.services import (
    ServiceMessageBody,
    ServiceUnavailable,
    service_body,
    service_unavailable_ends_service_use,
)

from deckr.controller._actions._availability_policy import (
    SERVICE_VIEW_UNAVAILABLE_REASON,
)
from deckr.controller._actions._models import (
    ProviderSessionKey,
)
from deckr.controller._actions._routing import (
    ActionRuntimeCommand,
    decode_action_command,
)
from deckr.controller._actions._supervisor import _RuntimeLeaseState

logger = logging.getLogger(__name__)


class ActionRuntimeGateway:
    """Exact-agreement send and inbound message authorization."""

    def __init__(self, *, supervisor: ActionRuntimeSupervisor, catalog: ActionCatalog):
        self.supervisor = supervisor
        self.catalog = catalog

    def current_contract(
        self,
        provider_session_key: ProviderSessionKey | None,
    ) -> ContractPointer | None:
        if provider_session_key is None:
            return None
        state = self.supervisor._runtime_leases.get(provider_session_key)
        if state is None:
            return None
        try:
            if (
                state.lease.descriptor.session_id
                != provider_session_key.provider_session_id
            ):
                return None
            return (
                state.lease.state.contract
                if state.lease.state.local_authority
                else None
            )
        except ServiceUnavailable:
            return None

    async def authorize_runtime_message(
        self,
        key: ProviderSessionKey | None,
        message: DeckrMessage,
        *,
        validate_payload: bool = True,
    ) -> bool:
        state = self.supervisor._runtime_leases.get(key)
        if state is None:
            return False
        try:
            validate_action_runtime_message(message, validate_payload=validate_payload)
            body = ServiceMessageBody.from_wire(message.body)
            await state.lease.authorize_inbound_message(message, name=body.name)
        except (ServiceUnavailable, ValueError, TypeError):
            return False
        return True

    async def send_runtime_message(
        self,
        provider_session_key: ProviderSessionKey,
        message_type: str,
        body: ActionMessageBody,
        *,
        contract: ContractPointer | None,
        causation_id: str | None = None,
    ) -> bool:
        services = self.supervisor._services
        state = self.supervisor._runtime_leases.get(provider_session_key)
        if (
            services is None
            or state is None
            or contract is None
            or self.current_contract(provider_session_key) != contract
        ):
            return False
        # ManagedService.send captures its concrete ServiceUseLease before its first
        # await. It validates that lease and never retries one-way sends on recovery.
        name = action_runtime_message_name(message_type)
        params, event = action_runtime_payload(name, body)
        try:
            options = {"causation_id": causation_id} if causation_id is not None else {}
            if name == "binding_render_result":
                options["causation_id"] = body.request_message_id
            await state.lease.send(name, params=params, event=event, **options)
        except ServiceUnavailable as exc:
            if service_unavailable_ends_service_use(exc):
                self.supervisor._drop_runtime_lease(provider_session_key, state)
                self.supervisor._restart_service_watch_after_lease_loss(state)
                changed = self.catalog.mark_provider_session_unavailable(
                    provider_session_key,
                    reason=SERVICE_VIEW_UNAVAILABLE_REASON,
                )
                if changed:
                    await self.supervisor._notify_availability_changed(changed)
            logger.warning(
                "Could not send action runtime message provider=%s provider_id=%s "
                "session=%s name=%s code=%s message=%s diagnostics=%s",
                provider_session_key.provider_instance_id,
                provider_session_key.provider_id,
                provider_session_key.provider_session_id,
                name,
                exc.code,
                exc.message,
                exc.diagnostics,
            )
            return False
        return True

    async def render_message_key(
        self, message: DeckrMessage
    ) -> ProviderSessionKey | None:
        provider_instance_id = _runtime_provider_instance_id_from_message(message)
        if provider_instance_id is None:
            return None
        for state in self.supervisor._runtime_lease_states_for_inbound(
            provider_instance_id=provider_instance_id,
            provider_session_id=message.sender_session_id,
        ):
            if await self.authorize_runtime_message(
                state.key, message, validate_payload=False
            ):
                return state.key
        return None

    async def send_render_result(
        self, message: DeckrMessage, body: BindingRenderResultBody
    ) -> bool:
        key = await self.render_message_key(message)
        if key is None or self.current_contract(key) != message.contract:
            return False
        return await self.send_runtime_message(
            key, "bindingRenderResult", body, contract=message.contract
        )

    async def decode_inbound_runtime_message(
        self,
        deckr_message: DeckrMessage,
    ) -> ActionRuntimeCommand | None:
        services = self.supervisor._services
        if services is None:
            return None
        if deckr_message.message_type != "serviceMessage":
            return None
        try:
            body = service_body(deckr_message)
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring invalid Action Runtime service message", exc_info=True
            )
            return None
        if body.service_namespace != ACTION_RUNTIME_SERVICE_NAMESPACE:
            return None
        if body.name not in RUNTIME_TO_CONTROLLER_MESSAGES:
            return None
        provider_instance_id = _runtime_provider_instance_id_from_message(deckr_message)
        if provider_instance_id is None:
            logger.warning(
                "Ignoring Action Runtime service message %s from non-runtime sender %s",
                body.name,
                deckr_message.sender,
            )
            return None

        states = self.supervisor._runtime_lease_states_for_inbound(
            provider_instance_id=provider_instance_id,
            provider_session_id=deckr_message.sender_session_id,
        )
        for state in states:
            try:
                authorized = await _authorize_inbound_action_runtime_message(
                    state,
                    deckr_message,
                    body,
                )
            except ValueError:
                return None
            except ServiceUnavailable as exc:
                if service_unavailable_ends_service_use(exc):
                    self.supervisor._drop_runtime_lease(state.key, state)
                    self.supervisor._restart_service_watch_after_lease_loss(state)
                    changed = self.catalog.mark_provider_session_unavailable(
                        state.key,
                        reason=SERVICE_VIEW_UNAVAILABLE_REASON,
                    )
                    if changed:
                        await self.supervisor._notify_availability_changed(changed)
                logger.warning(
                    "Ignoring unauthorized Action Runtime service message "
                    "provider=%s provider_id=%s session=%s name=%s code=%s "
                    "message=%s diagnostics=%s",
                    state.key.provider_instance_id,
                    state.key.provider_id,
                    state.key.provider_session_id,
                    body.name,
                    exc.code,
                    exc.message,
                    exc.diagnostics,
                )
                continue
            try:
                command = decode_action_command(deckr_message, authorized)
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring invalid Action Runtime payload name=%s",
                    authorized.name,
                    exc_info=True,
                )
                return None
            return command
        logger.warning(
            "Ignoring Action Runtime service message without active provider-session "
            "lease provider=%s session=%s name=%s",
            provider_instance_id,
            deckr_message.sender_session_id,
            body.name,
        )
        return None


def _runtime_provider_instance_id_from_message(message: DeckrMessage) -> str | None:
    service_id = parse_service_address(message.sender)
    if service_id is None:
        return None
    return action_runtime_provider_instance_id(service_id)


async def _authorize_inbound_action_runtime_message(
    state: _RuntimeLeaseState,
    message: DeckrMessage,
    body: ServiceMessageBody,
) -> ServiceMessageBody:
    validate_action_runtime_message(message)
    return await state.lease.authorize_inbound_message(message, name=body.name)
