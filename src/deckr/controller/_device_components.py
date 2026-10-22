from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from deckr.lanes import EndpointSession

    from deckr.controller._actions._models import BuiltinActionCatalog
    from deckr.controller._hardware import HardwareCommandService
    from deckr.controller.settings import SettingsService

from dataclasses import dataclass
from typing import TYPE_CHECKING

from deckr.actions.messages import (
    ACTION_INSTANCE_DESTROYED,
    PAGE_SESSION_CLOSED,
    ActionInstanceLifecycleBody,
)

from deckr.controller._actions import (
    ControllerActionService,
)
from deckr.controller._binding_planner import (
    BindingPlanner,
)
from deckr.controller._bindings._action_lifecycle import (
    ActionInstanceLifecycleService,
)
from deckr.controller._bindings._commands import ProviderCommandIngress
from deckr.controller._bindings._input import InputInteractions
from deckr.controller._bindings._page_commands import PageCommandPorts
from deckr.controller._bindings._planning import PagePlanning
from deckr.controller._bindings._ports import BindingActionService
from deckr.controller._bindings._presentation import BindingPresentation
from deckr.controller._bindings._reconcile import BindingReconciler
from deckr.controller._bindings._rendering import BindingRenderer
from deckr.controller._bindings._requests import BindingRequestHandler
from deckr.controller._event_translator import EventTranslator
from deckr.controller._interest_diagnostics import InterestDiagnostics
from deckr.controller._pages._navigation import DeviceNavigation, NavigationBindings
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._session_effects import Effect, SessionEffects

if TYPE_CHECKING:
    from deckr.controller._device_session import DeviceSession


@dataclass(frozen=True, slots=True)
class DeviceComponents:
    """Explicit component graph owned by one device session."""

    requests: BindingRequestHandler
    effects: SessionEffects
    dispatcher: RenderDispatcher
    actions: BindingActionService
    presentation: BindingPresentation
    inputs: InputInteractions
    planning: PagePlanning
    interests: InterestDiagnostics
    lifecycle: ActionInstanceLifecycleService
    bindings: BindingReconciler
    navigation: DeviceNavigation
    commands: ProviderCommandIngress
    rendering: BindingRenderer
    page_commands: PageCommandPorts


def build_device_components(
    session: DeviceSession,
    *,
    controller_id: str,
    command_service: HardwareCommandService,
    manager: BuiltinActionCatalog,
    actions_bus: EndpointSession,
    start_soon: Callable[..., None],
    settings_service: SettingsService | None,
    action_service: BindingActionService | None,
    page_timeout_check_interval: float,
) -> DeviceComponents:
    """Wire collaborators once; runtime decisions belong to their owning component."""
    effects = SessionEffects(start_soon)
    dispatcher = RenderDispatcher(
        command_service=command_service,
        config_id=session.config_id,
        backend=session._render_backend,
        start_soon=start_soon,
        result_authorizer=session._render_output_authorized,
        completion_transaction=lambda: session.boundary,
    )
    translator = EventTranslator()
    planner = BindingPlanner(
        controller_id=controller_id,
        config_id=session.config_id,
    )
    actions = action_service or (
        ControllerActionService(
            controller_id=controller_id,
            controller_session_id=actions_bus.session_id,
            manager=manager,
            start_soon=None,
            clock=session._clock,
        )
    )
    presentation = BindingPresentation(
        state=session.state,
        hardware=session._command_service,
        catalog=actions,
        dispatcher=dispatcher,
        clock=session._clock,
        planner=planner,
    )
    inputs = InputInteractions(
        state=session.state,
        effects=effects,
        translator=translator,
        catalog=actions,
        hardware=session._command_service,
    )
    planning = PagePlanning(
        state=session.state,
        catalog=actions,
        planner=planner,
        clock=session._clock,
    )
    interests = InterestDiagnostics(
        state=session.state,
        catalog=actions,
        planner=planner,
        clock=session._clock,
    )
    lifecycle = ActionInstanceLifecycleService(
        config_id=session.config_id,
        runtime_sender=session._runtime_sender,
        notification_sender=DeferredLifecycleNotifications(
            effects, session._runtime_sender
        ),
        schedule_recovery=lambda lease: session.components.bindings.installer.recover_attachment(
            lease
        ),
        availability_recorder=actions,
        host=session,
        clock=session._clock,
    )
    bindings = BindingReconciler(
        state=session.state,
        boundary=session.boundary,
        effects=effects,
        lifecycle=lifecycle,
        catalog=actions,
        registry=manager,
        settings=settings_service,
        runtime_sender=session._runtime_sender,
        scoped_requests=session.scoped_requests,
        start_soon=start_soon,
        controller_id=controller_id,
        hardware=command_service,
        presentation=presentation,
        planning=planning,
        inputs=inputs,
        interests=interests,
        close_page=session.close_page,
        retire_rendering=lambda binding_id: session.components.rendering.retire_binding(
            binding_id
        ),
    )
    navigation = DeviceNavigation(
        state=session.state,
        boundary=session.boundary,
        lifecycle=lifecycle,
        clock=session._clock,
        interval=page_timeout_check_interval,
        bindings=NavigationBindings(
            build_plan=planning._build_page_plan,
            prepare_commit=bindings._prepare_page_commit,
            apply_commit=bindings._apply_page_commit,
            release_children=bindings._destroy_inactive_external_dynamic_page_children,
            sync_interest=session._sync_top_frame_state,
            cancel_inputs=inputs._cancel_all_held_inputs,
            revoke_bindings=bindings._revoke_active_bindings,
            clear_outputs=presentation._clear_all_raster_controls,
        ),
    )
    commands = ProviderCommandIngress(
        host=session,
        lifecycle=lifecycle,
    )
    from deckr.controller._bindings._render_policy import RenderingPorts
    from deckr.controller._bindings._rendering import (
        BindingRenderer,
    )

    rendering = BindingRenderer(
        RenderingPorts(
            endpoint=session._endpoint,
            hardware=session._command_service,
            actions=actions,
            dispatcher=dispatcher,
            backend=session._render_backend,
            boundary=session.boundary,
            start_soon=start_soon,
            config_id=session.config_id,
            hardware_ref=session.hardware_ref,
            notify=session._queue_render_result,
            is_active=lambda: session.config_active
            and session.state.hardware_current(session._command_service),
            binding_by_id=session.binding_by_id,
            binding_output_authorized=session.binding_output_authorized,
            current_contract=session.current_contract,
        )
    )
    from deckr.controller._bindings._page_commands import PageCommandPorts

    page_commands = PageCommandPorts(
        endpoint=session._endpoint,
        hardware=session._command_service,
        actions=actions,
        navigation=navigation,
        boundary=session.boundary,
        state=session.state,
        binding_by_id=session.binding_by_id,
        binding_command_authorized=session.binding_command_authorized,
        provider_session_key_for_session=session.provider_session_key_for_session,
        current_contract=session.current_contract,
        emit_page_opened=lifecycle.emit_page_opened,
    )

    requests = BindingRequestHandler(
        state=session.state,
        boundary=session.boundary,
        hardware=command_service,
        navigation=navigation,
        current_contract=session.current_contract,
        provider_session_key_for_session=session.provider_session_key_for_session,
        emit_page_opened=lifecycle.emit_page_opened,
        agreement_current=bindings.rules._lease_uses_current_provider_session_contract,
        set_page=session.set_page,
        open_page=session.open_page,
    )
    return DeviceComponents(
        requests=requests,
        effects=effects,
        dispatcher=dispatcher,
        actions=actions,
        presentation=presentation,
        inputs=inputs,
        planning=planning,
        interests=interests,
        lifecycle=lifecycle,
        bindings=bindings,
        navigation=navigation,
        commands=commands,
        rendering=rendering,
        page_commands=page_commands,
    )


class DeferredLifecycleNotifications:
    """Snapshot notifications are effects, ordered with the instance's other work."""

    def __init__(self, effects, sender):
        self.effects = effects
        self.sender = sender

    async def send_action_runtime_message(
        self, *, provider_session_key, contract, message_type, body, causation_id=None
    ):
        metadata = (
            body.metadata
            if isinstance(body, ActionInstanceLifecycleBody)
            else body.page_session
        )
        resource = metadata.action_instance_id
        return self.effects.submit(
            resource,
            Effect(
                message_type,
                lambda: self.sender.send_action_runtime_message(
                    provider_session_key=provider_session_key,
                    contract=contract,
                    causation_id=causation_id,
                    message_type=message_type,
                    body=body,
                ),
                cleanup=message_type
                in {ACTION_INSTANCE_DESTROYED, PAGE_SESSION_CLOSED},
            ),
        )
