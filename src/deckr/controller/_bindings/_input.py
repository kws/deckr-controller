import logging

from deckr.contracts.messages import DeckrMessage
from deckr.hardware import messages as hw_messages

from deckr.controller._bindings._attachments import (
    BindingLease,
    HeldInputRecord,
)
from deckr.controller._session_effects import Effect

logger = logging.getLogger(__name__)


class InputInteractions:
    """Held-interaction rules and ordered, single-attempt action input effects."""

    def __init__(self, *, state, effects, translator, catalog, hardware):
        self.catalog = catalog
        self.state = state
        self.effects = effects
        self.translator = translator
        self.hardware = hardware

    async def _cancel_held_inputs_for_binding(self, lease: BindingLease) -> None:
        for held in self.state.attachments.cancel_held_inputs_for_binding(
            lease.binding_id
        ):
            await self._deliver_cancelled_input(lease, held)

    async def _cancel_all_held_inputs(self) -> None:
        for held in self.state.attachments.cancel_all_held_inputs():
            lease = self.state.attachments.binding_leases.get(held.binding_id)
            if lease is not None:
                await self._deliver_cancelled_input(lease, held)

    async def _deliver_cancelled_input(
        self, lease: BindingLease, held: HeldInputRecord
    ) -> None:
        if lease.attached and held.context_id == lease.context_id:
            event = held.down_event.model_copy(update={"event_type": "cancel"})
            self.effects.submit(
                lease.action_instance_id,
                Effect(
                    "cancel input", lambda: self._send_input(lease, event), cleanup=True
                ),
            )

    async def _send_input(self, lease, event) -> None:
        # Only terminal events may finish an old interaction after revocation.
        # Queued presses/turns must not execute once their grant or hardware ends.
        if event.event_type not in {"up", "cancel"} and (
            not self.state.hardware_current(self.hardware)
            or not lease.ready
            or self.state.attachments.binding_leases.get(lease.binding_id) is not lease
        ):
            return
        # Even terminal events cannot cross a provider incarnation or agreement.
        if (
            lease.provider_session_key is not None
            and self.catalog.current_contract(lease.provider_session_key)
            != lease.context.contract
        ):
            return
        await lease.context.on_input(event)

    async def _route_hardware_input(self, message: DeckrMessage):
        event = hw_messages.hardware_body_from_message(message)
        translated = self.translator.translate(event)
        if translated is None:
            return
        if self.state.navigation.active_dynamic_session() is not None:
            self.state.navigation.record_activity()

        control_id = translated.control_id
        route_owner = self.state.attachments.active_input_for_event(
            control_id,
            translated.capability_id,
            translated.action_event.event_type,
        )
        if isinstance(route_owner, HeldInputRecord):
            lease = self.state.attachments.binding_leases.get(route_owner.binding_id)
            if lease is None:
                return
            await self._deliver_input_to_lease(lease, translated)
            return

        lease = route_owner
        if lease is None:
            if translated.action_event.event_type == "up":
                logger.info(
                    "Ignoring release without held owner config=%s control=%s capability=%s",
                    self.state.config.id,
                    control_id,
                    translated.capability_id,
                )
                return
            logger.info(
                "Ignoring input from unbound control config=%s control=%s capability=%s",
                self.state.config.id,
                control_id,
                translated.capability_id,
            )
            return
        if translated.capability_id not in lease.input_capability_ids:
            logger.info(
                "Ignoring input for unselected capability config=%s control=%s capability=%s",
                self.state.config.id,
                control_id,
                translated.capability_id,
            )
            return

        await self._deliver_input_to_lease(lease, translated)

    async def _deliver_input_to_lease(self, lease: BindingLease, translated) -> None:
        if translated.capability_id not in lease.input_capability_ids:
            logger.info(
                "Ignoring input for unselected capability config=%s control=%s capability=%s",
                self.state.config.id,
                translated.control_id,
                translated.capability_id,
            )
            return
        try:
            match = next(
                (
                    match
                    for match in lease.context.metadata.matched_capabilities
                    if match.capability.capability_id == translated.capability_id
                ),
                None,
            )
            event = translated.action_event
            if match is not None:
                event = event.model_copy(
                    update={
                        "view": {
                            "projection": "projected",
                            "derivation": "derived",
                        }.get(match.provenance, match.provenance),
                        "source": match.source,
                    }
                )
            if event.event_type == "down":
                self.state.attachments.record_held_input(
                    lease=lease,
                    control_id=translated.control_id,
                    capability_id=translated.capability_id,
                    down_event=event,
                )
            if not self.effects.submit(
                lease.action_instance_id,
                Effect("deliver input", lambda: self._send_input(lease, event)),
            ):
                await self._cancel_held_inputs_for_binding(lease)
                if event.event_type == "up":
                    # Routing already removed the held owner. A refused release
                    # still owes that owner a terminal notification.
                    cancel = event.model_copy(update={"event_type": "cancel"})
                    self.effects.submit(
                        lease.action_instance_id,
                        Effect(
                            "cancel refused release",
                            lambda: self._send_input(lease, cancel),
                            cleanup=True,
                        ),
                    )
        except Exception as e:
            logger.error(
                "Error delivering input to action %s: %s",
                lease.action_id,
                e,
                exc_info=True,
            )
