"""Direct authorization and dispatch tests for provider command ingress."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from deckr.actions.endpoints import (
    BUILTIN_ACTION_PROVIDER_ID,
    action_provider_address,
)
from deckr.actions.messages import (
    ACTION_LIFECYCLE_REJECTED,
    BINDING_OUTPUT,
    BINDING_OVERLAY,
    BINDING_OVERLAY_CLEAR,
    CLOSE_PAGE,
    OPEN_PAGE,
    REPLACE_PAGE,
    ActionLifecycleRejectedBody,
    BindingMetadata,
    BindingOutputBody,
    BindingOverlayBody,
    BindingOverlayClearBody,
    DynamicPageCommand,
    PageChildBindingDescriptor,
    PageChildBindingTarget,
    action_message,
    context_subject,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import controller_address
from deckr.hardware.descriptors import CapabilityRef, ControlRef, DeviceRef

from deckr.controller._actions import ProviderSessionKey
from deckr.controller._bindings._commands import ProviderCommandIngress

CONFIG_ID = "config-a"
CONTEXT_ID = "context-a"
BINDING_ID = "binding-a"
ACTION_INSTANCE_ID = "action-instance-a"
PAGE_SESSION_ID = "page-session-a"
PROVIDER_INSTANCE_ID = "provider-a"
PROVIDER_ID = "dev.deckr.provider"
PROVIDER_SESSION_ID = "provider-session-a"
CONTROLLER_ID = "controller-main"
DEVICE_REF = DeviceRef(managerId="manager-a", deviceId="device-a")
CONTRACT = ContractPointer(contractId="contract-a", generation=1)
SESSION_KEY = ProviderSessionKey(
    PROVIDER_INSTANCE_ID,
    PROVIDER_ID,
    PROVIDER_SESSION_ID,
)


def _binding_metadata(*, output_generation: int = 3) -> BindingMetadata:
    return BindingMetadata(
        providerInstanceId=PROVIDER_INSTANCE_ID,
        providerId=PROVIDER_ID,
        actionId="action.alpha",
        actionInstanceId=ACTION_INSTANCE_ID,
        configId=CONFIG_ID,
        contextId=CONTEXT_ID,
        bindingId=BINDING_ID,
        deviceRef=DEVICE_REF,
        controlRef=ControlRef(deviceRef=DEVICE_REF, controlId="key-1"),
        outputGeneration=output_generation,
    )


def _descriptor(page_id: str = "dynamic-a") -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=(
            PageChildBindingDescriptor(
                controlId="key-2",
                target=PageChildBindingTarget(kind="self"),
            ),
        ),
    )


def _lease():
    context = SimpleNamespace(
        set_raster_image=AsyncMock(),
        clear_raster=AsyncMock(),
        show_overlay=AsyncMock(return_value=True),
        clear_overlay=AsyncMock(return_value=True),
    )
    return SimpleNamespace(
        binding_id=BINDING_ID,
        context_id=CONTEXT_ID,
        action_instance_id=ACTION_INSTANCE_ID,
        action_uuid="action.alpha",
        provider_instance_id=PROVIDER_INSTANCE_ID,
        provider_id=PROVIDER_ID,
        provider_session_id=PROVIDER_SESSION_ID,
        provider_session_key=SESSION_KEY,
        page_session_id=None,
        control_id="key-1",
        raster_capability_id="raster.bitmap",
        context=context,
    )


def _page_session():
    return SimpleNamespace(
        page_session_id=PAGE_SESSION_ID,
        context_id=CONTEXT_ID,
        action_instance_id=ACTION_INSTANCE_ID,
        owner_provider_instance_id=PROVIDER_INSTANCE_ID,
        owner_provider_id=PROVIDER_ID,
        owner_provider_session_id=PROVIDER_SESSION_ID,
    )


class _Host:
    config_id = CONFIG_ID

    def __init__(self) -> None:
        self.lease = _lease()
        self.session = None
        self.binding_commands_authorized = True
        self.binding_outputs_authorized = True
        self.contract_authorized = True
        self.message_contract_authorized = AsyncMock(
            side_effect=self._message_contract_authorized
        )
        self.recover_binding_provider_session_contract = AsyncMock()
        self.open_page = AsyncMock()
        self.replace_page = AsyncMock()
        self.close_page = AsyncMock()

    def binding_by_id(self, binding_id: str):
        return self.lease if binding_id == BINDING_ID else None

    def active_page_session(self):
        return self.session

    def binding_command_authorized(self, lease) -> bool:
        return lease is self.lease and self.binding_commands_authorized

    def binding_output_authorized(self, lease) -> bool:
        return lease is self.lease and self.binding_outputs_authorized

    def provider_session_key_for_session(self, **kwargs):
        if kwargs == {
            "provider_instance_id": PROVIDER_INSTANCE_ID,
            "provider_id": PROVIDER_ID,
            "provider_session_id": PROVIDER_SESSION_ID,
        }:
            return SESSION_KEY
        return None

    async def _message_contract_authorized(self, message, key) -> bool:
        return self.contract_authorized and key == SESSION_KEY and message.contract == CONTRACT


def _message(
    message_type: str,
    body,
    *,
    binding: bool = True,
    page: bool = False,
    sender=None,
    sender_session_id: str = PROVIDER_SESSION_ID,
    subject_provider_instance_id: str = PROVIDER_INSTANCE_ID,
    contract: ContractPointer | None = CONTRACT,
):
    return action_message(
        sender=sender or action_provider_address(PROVIDER_INSTANCE_ID),
        sender_session_id=sender_session_id,
        recipient=controller_address(CONTROLLER_ID),
        message_type=message_type,
        body=body,
        subject=context_subject(
            CONTEXT_ID,
            provider_instance_id=subject_provider_instance_id,
            provider_id=PROVIDER_ID,
            config_id=CONFIG_ID,
            action_instance_id=ACTION_INSTANCE_ID,
            binding_id=BINDING_ID if binding else None,
            page_session_id=PAGE_SESSION_ID if page else None,
        ),
        contract=contract,
    )


def _ingress():
    host = _Host()
    lifecycle = SimpleNamespace(handle_lifecycle_rejected=AsyncMock())
    return host, lifecycle, ProviderCommandIngress(host=host, lifecycle=lifecycle)


@pytest.mark.asyncio
async def test_binding_and_page_commands_dispatch_with_exact_authority() -> None:
    host, _, ingress = _ingress()
    descriptor = _descriptor()

    await ingress.handle(
        _message(OPEN_PAGE, {"descriptor": descriptor.to_dict()})
    )

    host.open_page.assert_awaited_once()
    assert host.open_page.await_args.kwargs["binding_id"] == BINDING_ID

    host.session = _page_session()
    await ingress.handle(
        _message(
            REPLACE_PAGE,
            {"descriptor": _descriptor("replacement").to_dict()},
            binding=False,
            page=True,
        )
    )
    await ingress.handle(_message(CLOSE_PAGE, {}, binding=False, page=True))

    host.replace_page.assert_awaited_once()
    host.close_page.assert_awaited_once_with(
        context_id=CONTEXT_ID,
        reason="close",
        causation_id=host.close_page.await_args.kwargs["causation_id"],
    )


@pytest.mark.asyncio
async def test_inactive_binding_and_wrong_page_owner_have_no_authority() -> None:
    host, _, ingress = _ingress()
    host.binding_commands_authorized = False

    await ingress.handle(
        _message(OPEN_PAGE, {"descriptor": _descriptor().to_dict()})
    )

    host.open_page.assert_not_awaited()
    host.session = _page_session()
    host.session.owner_provider_instance_id = "provider-b"
    await ingress.handle(_message(CLOSE_PAGE, {}, binding=False, page=True))

    host.close_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_contract_mismatch_recovers_binding_but_stale_session_does_not() -> None:
    host, _, ingress = _ingress()
    host.contract_authorized = False

    await ingress.handle(
        _message(OPEN_PAGE, {"descriptor": _descriptor().to_dict()})
    )

    host.recover_binding_provider_session_contract.assert_awaited_once_with(
        host.lease,
        reason="openPage_contract_mismatch",
    )
    host.open_page.assert_not_awaited()

    host.recover_binding_provider_session_contract.reset_mock()
    host.message_contract_authorized.reset_mock()
    await ingress.handle(
        _message(
            OPEN_PAGE,
            {"descriptor": _descriptor().to_dict()},
            sender_session_id="stale-session",
        )
    )

    host.message_contract_authorized.assert_not_awaited()
    host.recover_binding_provider_session_contract.assert_not_awaited()


@pytest.mark.parametrize(
    "rejection_kind",
    ("reserved_sender", "subject_mismatch"),
)
@pytest.mark.asyncio
async def test_reserved_or_subject_mismatched_provider_is_rejected(
    rejection_kind,
) -> None:
    host, _, ingress = _ingress()
    message = _message(
        OPEN_PAGE,
        {"descriptor": _descriptor().to_dict()},
        subject_provider_instance_id=(
            "provider-b"
            if rejection_kind == "subject_mismatch"
            else PROVIDER_INSTANCE_ID
        ),
    )
    if rejection_kind == "reserved_sender":
        message = message.model_copy(
            update={
                "sender": f"action_provider:{BUILTIN_ACTION_PROVIDER_ID}",
            }
        )

    await ingress.handle(message)

    host.message_contract_authorized.assert_not_awaited()
    host.open_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_output_and_overlay_commands_dispatch_to_authorized_context() -> None:
    host, _, ingress = _ingress()
    metadata = _binding_metadata()
    capability = CapabilityRef(
        deviceRef=DEVICE_REF,
        controlId="key-1",
        capabilityId="raster.bitmap",
    )

    await ingress.handle(
        _message(
            BINDING_OUTPUT,
            BindingOutputBody(
                binding=metadata,
                capability=capability,
                commandType="set_frame",
                params={"image": "data:image/png;base64,cG5n"},
                generation=3,
            ),
        )
    )
    await ingress.handle(
        _message(
            BINDING_OUTPUT,
            BindingOutputBody(
                binding=metadata,
                capability=capability,
                commandType="clear",
                generation=3,
            ),
        )
    )
    await ingress.handle(
        _message(
            BINDING_OVERLAY,
            BindingOverlayBody(
                binding=metadata,
                template="ok",
                generation=4,
            ),
        )
    )
    await ingress.handle(
        _message(
            BINDING_OVERLAY_CLEAR,
            BindingOverlayClearBody(
                binding=metadata,
                generation=5,
            ),
        )
    )

    host.lease.context.set_raster_image.assert_awaited_once()
    host.lease.context.clear_raster.assert_awaited_once_with(generation=3)
    host.lease.context.show_overlay.assert_awaited_once()
    host.lease.context.clear_overlay.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifecycle_rejection_routes_authorization_to_lifecycle_service() -> None:
    host, lifecycle, ingress = _ingress()
    body = ActionLifecycleRejectedBody(
        targetKind="binding",
        binding=_binding_metadata(),
        reason="stale_lifecycle",
    )
    message = _message(ACTION_LIFECYCLE_REJECTED, body)

    await ingress.handle(message)

    lifecycle.handle_lifecycle_rejected.assert_awaited_once()
    call = lifecycle.handle_lifecycle_rejected.await_args
    assert call.args == (message, body)
    assert call.kwargs["authorization"].binding is host.lease
    assert call.kwargs["sender_provider_instance_id"] == PROVIDER_INSTANCE_ID
