"""Controller-owned control attachment authority."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from deckr.actions.messages import SettingsTargetRef

from deckr.controller._actions._models import ProviderSessionKey
from deckr.controller._binding_planner import DynamicPageSession
from deckr.controller._device_layout import ControlSurface
from deckr.controller.action_provider.context import ControlContext


@dataclass(slots=True)
class BindingLease:
    binding_id: str
    context_id: str
    action_instance_id: str
    action_uuid: str
    provider_instance_id: str
    provider_id: str
    provider_session_id: str | None
    provider_session_key: ProviderSessionKey | None
    attached: bool
    control_id: str
    control: ControlSurface
    input_capability_ids: frozenset[str]
    raster_capability_id: str | None
    profile_id: str
    page_id: str
    settings_target: SettingsTargetRef | None
    context: ControlContext
    page_session_id: str | None = None
    item_key: str | None = None
    handler: str | None = None
    output_route_generation: int = 0
    command_route_generation: int = 0
    stale_lifecycle_recoveries: int = 0


@dataclass(slots=True)
class HeldInputRecord:
    binding_id: str
    control_id: str
    capability_id: str
    context_id: str
    down_event: Any


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizedCommandTarget:
    sender_provider_instance_id: str
    context_id: str
    binding: BindingLease | None = None
    page_session: DynamicPageSession | None = None


class ControlAttachmentState:
    """Single source of truth for controller-owned control attachment routes."""

    def __init__(self) -> None:
        self.binding_leases: dict[str, BindingLease] = {}
        self.binding_by_context: dict[str, str] = {}
        self.active_input_by_control: dict[str, str] = {}
        self.active_output_by_control: dict[str, str] = {}
        self.command_authority_by_binding: set[str] = set()
        self.held_input_bindings: dict[tuple[str, str], HeldInputRecord] = {}
        self._suppressed_release_keys: set[tuple[str, str]] = set()
        self._output_generation_by_control: dict[str, int] = {}
        self._command_generation = 0

    def add_binding(self, lease: BindingLease) -> None:
        self.binding_leases[lease.binding_id] = lease

    def remove_binding(self, binding_id: str) -> BindingLease | None:
        lease = self.binding_leases.get(binding_id)
        if lease is None:
            return None
        self.disable_binding_authority(lease)
        return self.binding_leases.pop(binding_id, None)

    def binding_for_context(self, context_id: str) -> BindingLease | None:
        binding_id = self.binding_by_context.get(context_id)
        if binding_id is None:
            return None
        return self.binding_leases.get(binding_id)

    def binding_for_control(self, control_id: str) -> BindingLease | None:
        lease = self.active_input_lease(control_id)
        if lease is not None:
            return lease
        lease = self.active_output_lease(control_id)
        if lease is not None:
            return lease
        for lease in self.binding_leases.values():
            if lease.control_id == control_id:
                return lease
        return None

    def active_input_lease(self, control_id: str) -> BindingLease | None:
        binding_id = self.active_input_by_control.get(control_id)
        if binding_id is None:
            return None
        return self.binding_leases.get(binding_id)

    def active_output_lease(self, control_id: str) -> BindingLease | None:
        binding_id = self.active_output_by_control.get(control_id)
        if binding_id is None:
            return None
        return self.binding_leases.get(binding_id)

    def enable_binding_authority(self, lease: BindingLease) -> None:
        self.binding_by_context[lease.context_id] = lease.binding_id
        if lease.input_capability_ids:
            self.active_input_by_control[lease.control_id] = lease.binding_id
        if lease.raster_capability_id is not None:
            if self.active_output_by_control.get(lease.control_id) == lease.binding_id:
                generation = self._output_generation_by_control.get(
                    lease.control_id,
                    0,
                )
            else:
                generation = self._bump_output_generation(lease.control_id)
            lease.output_route_generation = generation
            self.active_output_by_control[lease.control_id] = lease.binding_id
        if lease.binding_id not in self.command_authority_by_binding:
            self._command_generation += 1
            lease.command_route_generation = self._command_generation
        self.command_authority_by_binding.add(lease.binding_id)

    def disable_binding_authority(self, lease: BindingLease) -> None:
        current = self.binding_by_context.get(lease.context_id)
        if current == lease.binding_id:
            self.binding_by_context.pop(lease.context_id, None)
        current = self.active_input_by_control.get(lease.control_id)
        if current == lease.binding_id:
            self.active_input_by_control.pop(lease.control_id, None)
        current = self.active_output_by_control.get(lease.control_id)
        if current == lease.binding_id:
            self.active_output_by_control.pop(lease.control_id, None)
            self._bump_output_generation(lease.control_id)
        self.command_authority_by_binding.discard(lease.binding_id)

    def binding_command_authorized(self, lease: BindingLease) -> bool:
        return (
            lease.attached
            and self.binding_leases.get(lease.binding_id) is lease
            and lease.binding_id in self.command_authority_by_binding
        )

    def binding_output_authorized(self, lease: BindingLease) -> bool:
        return (
            self.binding_command_authorized(lease)
            and self.active_output_by_control.get(lease.control_id)
            == lease.binding_id
            and self._output_generation_by_control.get(lease.control_id, 0)
            == lease.output_route_generation
        )

    def output_render_authorized(
        self,
        control_id: str,
        binding_id: str | None,
        context_id: str | None,
    ) -> bool:
        if binding_id is None:
            return self.active_output_lease(control_id) is None
        lease = self.binding_leases.get(binding_id)
        return (
            lease is not None
            and lease.control_id == control_id
            and lease.context_id == context_id
            and self.binding_output_authorized(lease)
        )

    def active_input_for_event(
        self,
        control_id: str,
        capability_id: str,
        event_type: str,
    ) -> BindingLease | HeldInputRecord | None:
        key = (control_id, capability_id)
        if event_type == "up":
            if key in self._suppressed_release_keys:
                return None
            held = self.held_input_bindings.pop(key, None)
            if held is not None:
                self._suppressed_release_keys.add(key)
                return held
            return None

        if event_type == "down":
            self._suppressed_release_keys.discard(key)

        lease = self.active_input_lease(control_id)
        if lease is None or capability_id not in lease.input_capability_ids:
            return None
        return lease

    def record_held_input(
        self,
        *,
        lease: BindingLease,
        control_id: str,
        capability_id: str,
        down_event: Any,
    ) -> None:
        self.held_input_bindings[(control_id, capability_id)] = HeldInputRecord(
            binding_id=lease.binding_id,
            control_id=control_id,
            capability_id=capability_id,
            context_id=lease.context_id,
            down_event=down_event,
        )

    def cancel_held_inputs_for_binding(
        self,
        binding_id: str,
    ) -> tuple[HeldInputRecord, ...]:
        cancelled: list[HeldInputRecord] = []
        for key, held in tuple(self.held_input_bindings.items()):
            if held.binding_id != binding_id:
                continue
            self.held_input_bindings.pop(key, None)
            self._suppressed_release_keys.add(key)
            cancelled.append(held)
        return tuple(cancelled)

    def cancel_all_held_inputs(self) -> tuple[HeldInputRecord, ...]:
        return tuple(
            held
            for binding_id in self._held_binding_ids()
            for held in self.cancel_held_inputs_for_binding(binding_id)
        )

    def _held_binding_ids(self) -> Iterable[str]:
        return tuple(
            dict.fromkeys(held.binding_id for held in self.held_input_bindings.values())
        )

    def _bump_output_generation(self, control_id: str) -> int:
        generation = self._output_generation_by_control.get(control_id, 0) + 1
        self._output_generation_by_control[control_id] = generation
        return generation
