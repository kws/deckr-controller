import logging

from deckr.actions.messages import (
    make_context_id,
)

from deckr.controller._actions import (
    ActionAvailabilityRecord,
    action_unavailable_cause,
    provider_session_key,
    unavailable_overlay_template,
)
from deckr.controller._binding_plan import PlannedBinding
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._models import (
    _UnavailableFallbackRender,
)
from deckr.controller._device_layout import (
    ControlLayout,
    raster_controls,
)
from deckr.controller._presentation import DeviceOutput
from deckr.controller._render import RenderModel, RenderService, RenderSource
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID

logger = logging.getLogger(__name__)


class BindingPresentation:
    """Project binding status and accepted content into versioned render requests."""

    def __init__(self, *, state, hardware, catalog, dispatcher, clock, planner):
        self.planner = planner
        self.state = state
        self.hardware = hardware
        self.catalog = catalog
        self.dispatcher = dispatcher
        self.clock = clock

    async def _render_unavailable_to_control(
        self,
        control: ControlLayout,
        *,
        planned: PlannedBinding,
        page_id: str,
    ) -> None:
        """Render a not-available overlay to an output-capable control."""
        fallback = self._unavailable_fallback_render(planned)
        self._log_unavailable_fallback_render(
            control,
            planned=planned,
            page_id=page_id,
            fallback=fallback,
        )
        await self._render_status_to_control(
            control,
            overlay_type=fallback.template,
            source=self._unavailable_fallback_render_source(planned, fallback),
        )

    async def _render_pending_to_control(self, control: ControlLayout) -> None:
        """Render a pending overlay to an output-capable control."""
        await self._render_status_to_control(
            control,
            overlay_type="pending",
            source=RenderSource(command_type="binding_preparing"),
        )

    async def _render_status_to_control(
        self,
        control: ControlLayout,
        *,
        overlay_type: str,
        source: RenderSource | None = None,
    ) -> None:
        if control.image_format is None or control.raster_capability_id is None:
            return
        model = RenderModel(overlay_type=overlay_type)
        render_service = RenderService()
        output = DeviceOutput(
            self.hardware,
            self.state.config.id,
            control.id,
            control.raster_capability_id,
        )
        context_id = make_context_id()
        request = render_service.build_request(
            model,
            control.image_format,
            config_id=self.state.config.id,
            context_id=context_id,
            control_id=control.id,
            source=source,
        )
        await self.dispatcher.submit_request(
            control_id=control.id,
            context_id=context_id,
            request=request,
            output=output,
        )

    def _unavailable_fallback_render(
        self,
        planned: PlannedBinding,
    ) -> _UnavailableFallbackRender:
        intent = self.planner.resolved_action_intent_key(planned.binding)
        now = self.clock()
        record = self.catalog.record_for_intent(intent, now=now)
        state = (
            self.catalog.state_for_key(record.key, now=now)
            if record is not None
            else None
        )
        cause = action_unavailable_cause(
            record,
            has_live_provider_session_contract=(
                self._planned_binding_has_live_provider_session_contract(planned)
            ),
        )
        return _UnavailableFallbackRender(
            template=unavailable_overlay_template(cause),
            cause=cause,
            record=record,
            state=state,
        )

    def _planned_binding_has_live_provider_session_contract(
        self,
        planned: PlannedBinding,
    ) -> bool | None:
        action_meta = planned.action_meta
        if action_meta is None:
            return None
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return True
        session_key = provider_session_key(action_meta)
        return self.catalog.current_contract(session_key) is not None

    def _log_unavailable_fallback_render(
        self,
        control: ControlLayout,
        *,
        planned: PlannedBinding,
        page_id: str,
        fallback: _UnavailableFallbackRender,
    ) -> None:
        record = fallback.record
        selected_provider_key = (
            (
                record.key.provider_instance_id,
                record.key.action_id,
            )
            if record is not None
            else None
        )
        logger.info(
            "Controller unavailable fallback render config=%s page=%s control=%s "
            "action=%s provider=%s selected_provider_key=%s "
            "availability_state=%s availability_source=%s reason=%s cause=%s "
            "template=%s",
            self.state.config.id,
            page_id,
            control.id,
            planned.binding.action_id,
            self._unavailable_fallback_provider_instance(planned, record),
            selected_provider_key,
            fallback.state.value if fallback.state is not None else None,
            record.source.value if record is not None else None,
            record.reason if record is not None else None,
            fallback.cause.value,
            fallback.template,
        )

    def _unavailable_fallback_render_source(
        self,
        planned: PlannedBinding,
        fallback: _UnavailableFallbackRender,
    ) -> RenderSource:
        record = fallback.record
        metadata = record.metadata if record is not None else None
        if metadata is None:
            metadata = planned.action_meta
        return RenderSource(
            provider_instance_id=self._unavailable_fallback_provider_instance(
                planned,
                record,
            ),
            provider_id=metadata.provider_id if metadata is not None else None,
            action_id=planned.binding.action_id,
            action_instance_id=planned.action_instance_id,
            command_type="controller_fallback",
            content_kind=f"overlay:{fallback.template}",
            availability_cause=fallback.cause.value,
            availability_state=(
                fallback.state.value if fallback.state is not None else None
            ),
            availability_source=record.source.value if record is not None else None,
            availability_reason=record.reason if record is not None else None,
        )

    def _unavailable_fallback_provider_instance(
        self,
        planned: PlannedBinding,
        record: ActionAvailabilityRecord | None,
    ) -> str | None:
        if record is not None:
            return record.key.provider_instance_id
        if planned.action_meta is not None:
            return planned.action_meta.provider_instance_id
        return planned.binding.provider_instance_id

    async def _refresh_binding_output(
        self, lease: BindingLease, *, reason: str
    ) -> None:
        base_output_generation = lease.context.base_output_generation
        metadata_output_generation = getattr(
            lease.context.metadata,
            "output_generation",
            None,
        )
        content_kind = lease.context.content_kind
        if not lease.attached:
            logger.debug(
                "Skipping cached binding output refresh for detached lease "
                "config=%s control=%s action=%s provider=%s "
                "provider_session=%s binding=%s context=%s "
                "action_instance=%s output_route_generation=%s "
                "base_output_generation=%s metadata_output_generation=%s "
                "content_kind=%s attached=%s reason=%s",
                self.state.config.id,
                lease.control_id,
                lease.action_id,
                lease.provider_instance_id,
                lease.provider_session_id,
                lease.binding_id,
                lease.context_id,
                lease.action_instance_id,
                lease.output_route_generation,
                base_output_generation,
                metadata_output_generation,
                content_kind,
                lease.attached,
                reason,
            )
            return
        logger.debug(
            "Refreshing cached binding output config=%s control=%s action=%s "
            "provider=%s provider_session=%s binding=%s context=%s "
            "action_instance=%s output_route_generation=%s "
            "base_output_generation=%s metadata_output_generation=%s "
            "content_kind=%s attached=%s reason=%s",
            self.state.config.id,
            lease.control_id,
            lease.action_id,
            lease.provider_instance_id,
            lease.provider_session_id,
            lease.binding_id,
            lease.context_id,
            lease.action_instance_id,
            lease.output_route_generation,
            base_output_generation,
            metadata_output_generation,
            content_kind,
            lease.attached,
            reason,
        )
        try:
            await lease.context.refresh_raster()
        except Exception:
            logger.exception(
                "Error refreshing binding output config=%s control=%s action=%s "
                "provider=%s binding=%s reason=%s",
                self.state.config.id,
                lease.control_id,
                lease.action_id,
                lease.provider_instance_id,
                lease.binding_id,
                reason,
            )

    async def _clear_all_raster_controls(
        self,
        *,
        preserve_control_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Clear raster-capable controls before rendering a new page."""
        for control in raster_controls(self.state.device):
            if control.control_id in preserve_control_ids:
                continue
            await self.dispatcher.clear_control(
                control.control_id,
                output=DeviceOutput(
                    self.hardware,
                    self.state.config.id,
                    control.control_id,
                    control.capability_id,
                ),
            )

    async def _claim_binding_output_route(self, lease: BindingLease) -> None:
        if lease.raster_capability_id is None:
            return
        await self.dispatcher.submit_request(
            control_id=lease.control_id,
            context_id=lease.context_id,
            binding_id=lease.binding_id,
            request=None,
            output=DeviceOutput(
                self.hardware,
                self.state.config.id,
                lease.control_id,
                lease.raster_capability_id,
            ),
        )
