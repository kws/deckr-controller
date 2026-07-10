"""Direct tests for action-instance and page lifecycle coordination."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from deckr.actions.endpoints import action_provider_address
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_INSTANCE_DESTROYED,
    ACTION_LIFECYCLE_REJECTED,
    PAGE_SESSION_CLOSED,
    PAGE_SESSION_OPENED,
    ActionInstanceMetadata,
    ActionLifecycleRejectedBody,
    BindingMetadata,
    PageSessionMetadata,
    action_message,
    context_subject,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import controller_address
from deckr.hardware.descriptors import ControlRef, DeviceRef

from deckr.controller._actions import (
    ActionIntentKey,
    ActionMetadata,
    ProviderActionKey,
    ProviderSessionKey,
)
from deckr.controller._bindings._action_lifecycle import (
    ActionInstanceLifecycleService,
)
from deckr.controller._bindings._attachments import AuthorizedCommandTarget
from deckr.controller._pages import DynamicPageSession

CONFIG_ID = "config-a"
CONTEXT_ID = "context-a"
ACTION_ID = "action.alpha"
ACTION_INSTANCE_ID = "action-instance-a"
PROVIDER_INSTANCE_ID = "provider-a"
PROVIDER_ID = "dev.deckr.provider"
PROVIDER_SESSION_ID = "provider-session-a"
CONTROLLER_ID = "controller-main"
CONTRACT = ContractPointer(contractId="contract-a", generation=1)
DEVICE_REF = DeviceRef(managerId="manager-a", deviceId="device-a")


def _session_key(session_id: str = PROVIDER_SESSION_ID) -> ProviderSessionKey:
    return ProviderSessionKey(PROVIDER_INSTANCE_ID, PROVIDER_ID, session_id)


def _action_metadata(
    *,
    session_id: str = PROVIDER_SESSION_ID,
) -> ActionMetadata:
    return ActionMetadata(
        uuid=ACTION_ID,
        provider_instance_id=PROVIDER_INSTANCE_ID,
        provider_id=PROVIDER_ID,
        provider_session_id=session_id,
    )


def _instance_metadata() -> ActionInstanceMetadata:
    return ActionInstanceMetadata(
        providerInstanceId=PROVIDER_INSTANCE_ID,
        providerId=PROVIDER_ID,
        actionId=ACTION_ID,
        actionInstanceId=ACTION_INSTANCE_ID,
        configId=CONFIG_ID,
        contextId=CONTEXT_ID,
    )


def _binding_metadata() -> BindingMetadata:
    return BindingMetadata(
        providerInstanceId=PROVIDER_INSTANCE_ID,
        providerId=PROVIDER_ID,
        actionId=ACTION_ID,
        actionInstanceId=ACTION_INSTANCE_ID,
        configId=CONFIG_ID,
        contextId=CONTEXT_ID,
        bindingId="binding-a",
        deviceRef=DEVICE_REF,
        controlRef=ControlRef(deviceRef=DEVICE_REF, controlId="key-1"),
    )


def _page_session() -> DynamicPageSession:
    return DynamicPageSession(
        page_id="dynamic-a",
        page_session_id="page-session-a",
        context_id=CONTEXT_ID,
        action_instance_id=ACTION_INSTANCE_ID,
        owner_context_id="owner-context",
        owner_binding_id="binding-a",
        owner_control_id="key-1",
        owner_action_uuid=ACTION_ID,
        owner_provider_instance_id=PROVIDER_INSTANCE_ID,
        owner_provider_id=PROVIDER_ID,
        owner_provider_session_id=PROVIDER_SESSION_ID,
        owner_action_meta=_action_metadata(),
        owner_profile="default",
        owner_page=0,
        timeout_ms=60_000,
        last_activity=0.0,
        settings_target=None,
    )


def _page_metadata(session: DynamicPageSession) -> PageSessionMetadata:
    return PageSessionMetadata(
        providerInstanceId=session.owner_provider_instance_id,
        providerId=session.owner_provider_id,
        actionInstanceId=session.action_instance_id,
        configId=CONFIG_ID,
        pageId=session.page_id,
        pageSessionId=session.page_session_id,
        contextId=session.context_id,
        ownerBindingId=session.owner_binding_id,
    )


def _lease():
    return SimpleNamespace(
        binding_id="binding-a",
        context_id=CONTEXT_ID,
        action_instance_id=ACTION_INSTANCE_ID,
        action_uuid=ACTION_ID,
        provider_instance_id=PROVIDER_INSTANCE_ID,
        provider_id=PROVIDER_ID,
        provider_session_id=PROVIDER_SESSION_ID,
        provider_session_key=_session_key(),
        control_id="key-1",
        stale_lifecycle_recoveries=0,
        context=SimpleNamespace(on_binding_attached=AsyncMock()),
        planned_intent=ActionIntentKey(ACTION_ID, PROVIDER_INSTANCE_ID, ()),
    )


class _RuntimeSender:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []
        self.raise_for: set[str] = set()

    async def send_action_runtime_message(
        self,
        *,
        provider_session_key,
        message_type,
        body,
    ) -> bool:
        self.messages.append(
            SimpleNamespace(
                provider_session_key=provider_session_key,
                message_type=message_type,
                body=body,
            )
        )
        if message_type in self.raise_for:
            raise RuntimeError("provider unavailable")
        return True


class _AvailabilityRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_lifecycle_unavailable(self, **kwargs) -> ProviderActionKey:
        self.calls.append(kwargs)
        return ProviderActionKey(
            kwargs["provider_instance_id"],
            kwargs["action_uuid"],
        )


class _Host:
    config_id = CONFIG_ID

    def __init__(self) -> None:
        self.contracts = {_session_key(): CONTRACT}
        self.active_session = None
        self.leases = []
        self.revoke_binding = AsyncMock()
        self.close_page = AsyncMock()
        self.on_action_availability_changed = AsyncMock()

    def current_contract(self, key):
        return self.contracts.get(key)

    def provider_session_key_for_session(self, **kwargs):
        key = ProviderSessionKey(
            kwargs["provider_instance_id"],
            kwargs["provider_id"],
            kwargs["provider_session_id"],
        )
        return key if key in self.contracts else None

    def page_session_metadata(self, session):
        return _page_metadata(session)

    def active_page_session(self):
        return self.active_session

    def binding_by_id(self, binding_id):
        return next(
            (lease for lease in self.leases if lease.binding_id == binding_id),
            None,
        )

    def iter_binding_leases(self):
        return tuple(self.leases)

    def planned_intent_for_lease(self, lease):
        return lease.planned_intent

    async def message_contract_authorized(self, message, key) -> bool:
        return self.current_contract(key) == message.contract


def _service():
    host = _Host()
    sender = _RuntimeSender()
    recorder = _AvailabilityRecorder()
    lifecycle = ActionInstanceLifecycleService(
        config_id=CONFIG_ID,
        runtime_sender=sender,
        availability_recorder=recorder,
        host=host,
        clock=lambda: 123.0,
    )
    return host, sender, recorder, lifecycle


def _rejection_message(
    body: ActionLifecycleRejectedBody,
    *,
    sender_session_id: str = PROVIDER_SESSION_ID,
    contract: ContractPointer | None = CONTRACT,
):
    return action_message(
        sender=action_provider_address(PROVIDER_INSTANCE_ID),
        sender_session_id=sender_session_id,
        recipient=controller_address(CONTROLLER_ID),
        message_type=ACTION_LIFECYCLE_REJECTED,
        body=body,
        subject=context_subject(
            CONTEXT_ID,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            config_id=CONFIG_ID,
            action_instance_id=ACTION_INSTANCE_ID,
            binding_id=(
                body.binding.binding_id if body.binding is not None else None
            ),
            page_session_id=(
                body.page_session.page_session_id
                if body.page_session is not None
                else None
            ),
        ),
        contract=contract,
    )


@pytest.mark.asyncio
async def test_create_destroy_and_provider_session_movement() -> None:
    host, sender, _, lifecycle = _service()

    await lifecycle.ensure_action_instance(
        action_meta=_action_metadata(),
        action_instance_id=ACTION_INSTANCE_ID,
        context_id=CONTEXT_ID,
    )

    assert lifecycle.has_action_instance(ACTION_INSTANCE_ID)
    assert sender.messages[0].message_type == ACTION_INSTANCE_CREATED
    assert sender.messages[0].provider_session_key == _session_key()

    successor_key = _session_key("provider-session-b")
    host.contracts[successor_key] = ContractPointer(
        contractId="contract-b",
        generation=1,
    )
    lifecycle.move_action_instance_provider_session(
        ACTION_INSTANCE_ID,
        _action_metadata(session_id=successor_key.provider_session_id),
    )
    await lifecycle.destroy_action_instance(ACTION_INSTANCE_ID, reason="test")

    assert not lifecycle.has_action_instance(ACTION_INSTANCE_ID)
    assert sender.messages[-1].message_type == ACTION_INSTANCE_DESTROYED
    assert sender.messages[-1].provider_session_key == successor_key
    assert sender.messages[-1].body.reason == "test"


@pytest.mark.asyncio
async def test_page_notifications_and_provider_failure_isolation() -> None:
    host, sender, _, lifecycle = _service()
    session = _page_session()

    await lifecycle.emit_page_opened(session)
    sender.raise_for.add(PAGE_SESSION_CLOSED)
    await lifecycle.emit_page_closed(session, "close")

    assert [message.message_type for message in sender.messages] == [
        PAGE_SESSION_OPENED,
        PAGE_SESSION_CLOSED,
    ]
    assert sender.messages[-1].body.reason == "close"
    host.close_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_lifecycle_recovery_republishes_and_reattaches_once() -> None:
    host, sender, _, lifecycle = _service()
    lease = _lease()
    host.leases.append(lease)
    await lifecycle.ensure_action_instance(
        action_meta=_action_metadata(),
        action_instance_id=ACTION_INSTANCE_ID,
        context_id=CONTEXT_ID,
    )
    sender.messages.clear()

    body = ActionLifecycleRejectedBody(
        targetKind="binding",
        binding=_binding_metadata(),
        reason="stale_lifecycle",
    )
    message = _rejection_message(body)
    authorization = AuthorizedCommandTarget(
        sender_provider_instance_id=PROVIDER_INSTANCE_ID,
        context_id=CONTEXT_ID,
        binding=lease,
    )
    await lifecycle.handle_lifecycle_rejected(
        message,
        body,
        authorization=authorization,
        sender_provider_instance_id=PROVIDER_INSTANCE_ID,
        context_id=CONTEXT_ID,
    )
    await lifecycle.handle_lifecycle_rejected(
        message,
        body,
        authorization=authorization,
        sender_provider_instance_id=PROVIDER_INSTANCE_ID,
        context_id=CONTEXT_ID,
    )

    assert lease.stale_lifecycle_recoveries == 1
    assert [item.message_type for item in sender.messages] == [ACTION_INSTANCE_CREATED]
    lease.context.on_binding_attached.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_and_retryable_rejections_route_destructive_and_overlay_state() -> None:
    host, _, recorder, lifecycle = _service()
    lease = _lease()
    host.leases.append(lease)
    authorization = AuthorizedCommandTarget(
        sender_provider_instance_id=PROVIDER_INSTANCE_ID,
        context_id=CONTEXT_ID,
        binding=lease,
    )
    terminal = ActionLifecycleRejectedBody(
        targetKind="binding",
        binding=_binding_metadata(),
        reason="invalid_settings",
    )

    await lifecycle.handle_lifecycle_rejected(
        _rejection_message(terminal),
        terminal,
        authorization=authorization,
        sender_provider_instance_id=PROVIDER_INSTANCE_ID,
        context_id=CONTEXT_ID,
    )

    host.revoke_binding.assert_awaited_once_with(
        lease.binding_id,
        clear_output=True,
        notify_provider=False,
        reason="invalid_settings",
        clear_held_input=True,
    )

    host.revoke_binding.reset_mock()
    retryable = ActionLifecycleRejectedBody(
        targetKind="binding",
        binding=_binding_metadata(),
        reason="resource_unavailable",
        retryable=True,
    )
    await lifecycle.handle_lifecycle_rejected(
        _rejection_message(retryable),
        retryable,
        authorization=authorization,
        sender_provider_instance_id=PROVIDER_INSTANCE_ID,
        context_id=CONTEXT_ID,
    )

    host.revoke_binding.assert_not_awaited()
    assert recorder.calls == [
        {
            "provider_instance_id": PROVIDER_INSTANCE_ID,
            "provider_id": PROVIDER_ID,
            "provider_session_id": PROVIDER_SESSION_ID,
            "action_uuid": ACTION_ID,
            "reason": "resource_unavailable",
            "intent": lease.planned_intent,
            "now": 123.0,
        }
    ]
    host.on_action_availability_changed.assert_awaited_once_with(
        frozenset({ProviderActionKey(PROVIDER_INSTANCE_ID, ACTION_ID)})
    )


@pytest.mark.asyncio
async def test_action_instance_rejection_requires_current_session_and_contract() -> None:
    host, _, _, lifecycle = _service()
    await lifecycle.ensure_action_instance(
        action_meta=_action_metadata(),
        action_instance_id=ACTION_INSTANCE_ID,
        context_id=CONTEXT_ID,
    )
    body = ActionLifecycleRejectedBody(
        targetKind="action_instance",
        actionInstance=_instance_metadata(),
        reason="permission_denied",
    )

    await lifecycle.handle_lifecycle_rejected(
        _rejection_message(body, sender_session_id="stale-session"),
        body,
        authorization=None,
        sender_provider_instance_id=PROVIDER_INSTANCE_ID,
        context_id=CONTEXT_ID,
    )
    assert lifecycle.has_action_instance(ACTION_INSTANCE_ID)

    await lifecycle.handle_lifecycle_rejected(
        _rejection_message(body),
        body,
        authorization=None,
        sender_provider_instance_id=PROVIDER_INSTANCE_ID,
        context_id=CONTEXT_ID,
    )
    assert not lifecycle.has_action_instance(ACTION_INSTANCE_ID)
    host.close_page.assert_not_awaited()
