"""Action instance and provider runtime lifecycle handling for bindings."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from deckr.actions.messages import (
    ActionInstanceMetadata,
    PageSessionMetadata,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage

from deckr.controller._actions import (
    ActionIntentKey,
    ActionMetadata,
    ProviderActionKey,
    ProviderSessionKey,
)
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._pages import DynamicPageSession

logger = logging.getLogger(__name__)


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
        and stored.action_id == action_meta.action_id
        and stored.config_id == config_id
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
        and metadata.generation == session.generation
        and metadata.context_id == session.context_id
        and metadata.owner_binding_id == session.owner_binding_id
    )


@dataclass(slots=True)
class ActionInstanceRecord:
    metadata: ActionInstanceMetadata
    provider_session_key: ProviderSessionKey | None
    contract: ContractPointer | None
    started: bool = False
