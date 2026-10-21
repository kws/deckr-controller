"""Page commands resolved against retained state at a serialized commit."""

from deckr.action_runtime import (
    PageCommandContext,
    PageTargetState,
    validate_page_command,
)
from deckr.action_runtime.protocol import (
    action_runtime_body_from_service_message,
    action_runtime_service_id,
)
from deckr.actions.messages import (
    BindingCommandTarget,
    ClosePageBody,
    OpenPageBody,
    ReplacePageBody,
)
from deckr.contracts.messages import service_address
from deckr.services import ServiceMessageBody

from deckr.controller._pages import PageOwnerBinding


def _owner_from_binding(lease):
    return PageOwnerBinding(
        context_id=lease.context_id,
        binding_id=lease.binding_id,
        control_id=lease.control_id,
        action_uuid=lease.action_uuid,
        provider_instance_id=lease.provider_instance_id,
        provider_id=lease.provider_id,
        provider_session_id=lease.provider_session_id,
        action_instance_id=lease.action_instance_id,
        profile_id=lease.profile_id,
        page_id=lease.page_id,
        page_session_id=lease.page_session_id,
        settings_target=lease.settings_target,
        action_descriptor=lease.action_descriptor,
        contract=lease.context.contract,
    )


def _owner_from_session(session):
    return PageOwnerBinding(
        context_id=session.context_id,
        binding_id=session.owner_binding_id,
        control_id=session.owner_control_id,
        action_uuid=session.owner_action_uuid,
        provider_instance_id=session.owner_provider_instance_id,
        provider_id=session.owner_provider_id,
        provider_session_id=session.owner_provider_session_id,
        action_instance_id=session.action_instance_id,
        profile_id=session.owner_profile,
        page_id=session.page_id,
        page_session_id=session.page_session_id,
        settings_target=session.settings_target,
        action_descriptor=session.owner_action_meta.descriptor,
        contract=session.owner_contract,
    )


def _observations(host, message, owner, target):
    key = host.provider_session_key_for_session(
        provider_instance_id=owner.provider_instance_id,
        provider_id=owner.provider_id,
        provider_session_id=owner.provider_session_id,
    )
    current_contract = host.current_contract(key)
    bindings, sessions = {}, {}
    if isinstance(target, BindingCommandTarget):
        lease = host.binding_by_id(target.binding_id)
        if lease is not None and lease.context.contract is not None:
            bindings[target.binding_id] = PageTargetState(
                actionInstanceId=lease.action_instance_id,
                active=host.config_active and host.binding_command_authorized(lease),
                generation=lease.context.metadata.output_generation,
                contract=lease.context.contract,
                runtimeEndpoint=str(
                    service_address(
                        action_runtime_service_id(lease.provider_instance_id)
                    )
                ),
                runtimeSessionId=lease.provider_session_id,
                deviceRef=host.hardware_ref,
                controlIds=[control.control_id for control in host.device.controls],
            )
    else:
        session = host.active_page_session()
        if session is not None and session.owner_contract is not None:
            sessions[session.page_session_id] = PageTargetState(
                actionInstanceId=session.action_instance_id,
                active=host.config_active,
                generation=session.generation,
                contract=session.owner_contract,
                runtimeEndpoint=str(
                    service_address(
                        action_runtime_service_id(session.owner_provider_instance_id)
                    )
                ),
                runtimeSessionId=session.owner_provider_session_id,
                deviceRef=host.hardware_ref,
                controlIds=[control.control_id for control in host.device.controls],
                pageId=session.page_id,
            )
    return PageCommandContext(
        contract=message.contract,
        controllerEndpoint=str(host._endpoint.address),
        controllerSessionId=host._endpoint.session_id,
        runtimeEndpoint=str(
            service_address(action_runtime_service_id(owner.provider_instance_id))
        ),
        runtimeSessionId=owner.provider_session_id,
        localAuthority=current_contract is not None
        and current_contract == message.contract,
        hardwareAuthority=host._command_service.has_authority(
            host.config_id, host.hardware_ref
        ),
        bindings=bindings,
        pageSessions=sessions,
    )


async def handle_page_command(host, message) -> bool:
    """Return whether this device owns the addressed state, even on refusal."""
    try:
        body = action_runtime_body_from_service_message(
            ServiceMessageBody.from_wire(message.body)
        )
    except (ValueError, TypeError):
        return False
    if not isinstance(body, OpenPageBody | ReplacePageBody | ClosePageBody):
        return False
    target = body.target
    async with host._nav_lock:
        if isinstance(target, BindingCommandTarget):
            lease = host.binding_by_id(target.binding_id)
            if lease is None:
                return False
            owner = _owner_from_binding(lease)
        else:
            session = host.active_page_session()
            if session is None or session.page_session_id != target.page_session_id:
                return False
            owner = _owner_from_session(session)

        async def validate():
            key = host.provider_session_key_for_session(
                provider_instance_id=owner.provider_instance_id,
                provider_id=owner.provider_id,
                provider_session_id=owner.provider_session_id,
            )
            if not await host._action_service.authorize_runtime_message(key, message):
                raise ValueError("Page command lost managed service authority")
            validate_page_command(message, _observations(host, message, owner, target))

        try:
            await validate()
            if isinstance(body, OpenPageBody):
                draft = host._pages.begin_open_page(
                    descriptor=body.descriptor, owner=owner
                )
            elif isinstance(body, ReplacePageBody):
                draft = host._pages.begin_replace_page(
                    descriptor=body.descriptor, context_id=owner.context_id
                )
            else:
                draft = host._pages.begin_close_page(context_id=owner.context_id)
            if draft is None:
                return True
            ok, _ = await host._execute_page_draft(
                draft,
                causation_id=message.message_id,
                validate_commit=validate,
            )
        except (ValueError, TypeError):
            return True
        if ok and not isinstance(body, ClosePageBody):
            await host._lifecycle.emit_page_opened(
                draft.page_session, causation_id=message.message_id
            )
        return True
