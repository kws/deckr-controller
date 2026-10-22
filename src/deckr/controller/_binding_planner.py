"""Pure page binding planning for the controller runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from deckr.actions.messages import (
    DynamicPageCommand,
    PageChildBindingDescriptor,
)
from deckr.hardware.descriptors import DeviceDescriptor

from deckr.controller._actions._models import ActionIntentKey, ActionMetadata
from deckr.controller._binding_identity import (
    dynamic_child_action_instance_id,
    static_action_instance_id,
)
from deckr.controller._binding_plan import (
    BindingPlanOutcome,
    BindingPlanStatus,
    PagePlan,
    PagePlanBuildResult,
    PlannedBinding,
    ValidationError,
)
from deckr.controller._binding_resolution import (
    ConfiguredControlBinding,
    ResolvedControlBinding,
    exact_control_binding,
    resolve_binding,
)
from deckr.controller._capability_matching import select_requirements
from deckr.controller._pages import DynamicPageSession, StaticPageRef


class BindingPlanner:
    def __init__(self, controller_id: str, config_id: str) -> None:
        self._controller_id = controller_id
        self._config_id = config_id

    def static_action_intents(
        self,
        bindings: Sequence[ConfiguredControlBinding],
    ) -> tuple[ActionIntentKey, ...]:
        return tuple(self.configured_action_intent_key(binding) for binding in bindings)

    def dynamic_action_intents(
        self,
        children: Sequence[PageChildBindingDescriptor],
        *,
        owner_action_id: str,
        owner_provider_instance_id: str,
    ) -> tuple[ActionIntentKey, ...]:
        intents: list[ActionIntentKey] = []
        for child in children:
            binding = self._dynamic_page_child_binding(
                child,
                owner_action_id=owner_action_id,
                owner_provider_instance_id=owner_provider_instance_id,
            )
            if binding is None:
                continue
            intents.append(self.configured_action_intent_key(binding))
        return tuple(intents)

    def build_static_page_plan(
        self,
        entry: StaticPageRef,
        *,
        bindings: Sequence[ConfiguredControlBinding],
        device: DeviceDescriptor,
        action_metadata: Mapping[ActionIntentKey, ActionMetadata],
        action_status: Mapping[ActionIntentKey, BindingPlanStatus] | None = None,
        retained_plan: PagePlan | None = None,
    ) -> PagePlanBuildResult:
        planned: list[PlannedBinding] = []
        outcomes: list[BindingPlanOutcome] = []
        validation_errors: list[ValidationError] = []
        profile_id = entry.profile_name
        page_id = str(entry.page_index)

        for binding in bindings:
            resolved = resolve_binding(binding, device.controls)
            intent = self.configured_action_intent_key(binding)
            if resolved.binding is None:
                error = self._resolution_error(
                    binding=binding,
                    code=resolved.code,
                    message=resolved.message,
                    details=resolved.details,
                    profile_id=profile_id,
                    page_id=page_id,
                )
                validation_errors.append(error)
                outcomes.append(
                    BindingPlanOutcome(
                        status=BindingPlanStatus.INVALID_DEVICE_CONTROL,
                        intent=intent,
                        control_ref=error.control_ref,
                        error=error,
                    )
                )
                continue

            planned_binding = self._plan_binding(
                binding=resolved.binding,
                device=device,
                action_instance_id=static_action_instance_id(
                    self._controller_id,
                    self._config_id,
                    entry,
                    resolved.binding,
                ),
                action_metadata=action_metadata,
                action_status=action_status or {},
                retained_plan=retained_plan,
                page_session_id=None,
                settings_target_enabled=True,
            )
            planned.append(planned_binding)
            outcomes.append(self._binding_outcome(planned_binding))

        validation_errors.extend(_control_conflicts(planned, profile_id, page_id))
        if validation_errors:
            return PagePlanBuildResult(
                plan=None,
                outcomes=tuple(outcomes),
                validation_errors=tuple(validation_errors),
            )
        return PagePlanBuildResult(
            plan=PagePlan(
                entry=entry,
                profile_id=profile_id,
                page_id=page_id,
                page_session=None,
                bindings=tuple(planned),
            ),
            outcomes=tuple(outcomes),
        )

    def build_dynamic_page_plan(
        self,
        entry: DynamicPageCommand,
        *,
        device: DeviceDescriptor,
        page_session: DynamicPageSession,
        page_session_generation: int | None = None,
        action_metadata: Mapping[ActionIntentKey, ActionMetadata],
        action_status: Mapping[ActionIntentKey, BindingPlanStatus] | None = None,
        retained_plan: PagePlan | None = None,
    ) -> PagePlanBuildResult:
        planned: list[PlannedBinding] = []
        outcomes: list[BindingPlanOutcome] = []
        validation_errors: list[ValidationError] = []
        effective_action_metadata = dict(action_metadata)
        owner_intent = ActionIntentKey(
            action_id=page_session.owner_action_id,
            provider_instance_id=page_session.owner_provider_instance_id,
            provider_labels=(),
        )
        owner_action_meta = page_session.owner_action_meta
        provided_owner_meta = effective_action_metadata.get(owner_intent)
        if action_status and (
            action_status.get(owner_intent) == BindingPlanStatus.UNAVAILABLE
        ):
            effective_action_metadata.pop(owner_intent, None)
        elif provided_owner_meta is not None and (
            provided_owner_meta.provider_id != page_session.owner_provider_id
            or provided_owner_meta.provider_session_id
            != page_session.owner_provider_session_id
        ):
            effective_action_metadata[owner_intent] = provided_owner_meta
        else:
            effective_action_metadata[owner_intent] = owner_action_meta

        for child in entry.bindings:
            binding = self._dynamic_page_child_binding(
                child,
                owner_action_id=page_session.owner_action_id,
                owner_provider_instance_id=page_session.owner_provider_instance_id,
            )
            if binding is None:
                error = ValidationError(
                    code="invalid_child_target",
                    message="action page child target missing actionId",
                    control_ref=child.control_id,
                    action_id="",
                    profile_id="_dynamic",
                    page_id=entry.page_id,
                )
                validation_errors.append(error)
                outcomes.append(
                    BindingPlanOutcome(
                        status=BindingPlanStatus.INVALID_CONFIG,
                        intent=ActionIntentKey("", None, ()),
                        control_ref=child.control_id,
                        error=error,
                    )
                )
                continue

            resolved = resolve_binding(binding, device.controls)
            intent = self.configured_action_intent_key(binding)
            if resolved.binding is None:
                error = self._resolution_error(
                    binding=binding,
                    code=resolved.code,
                    message=resolved.message,
                    details=resolved.details,
                    profile_id="_dynamic",
                    page_id=entry.page_id,
                )
                validation_errors.append(error)
                outcomes.append(
                    BindingPlanOutcome(
                        status=BindingPlanStatus.INVALID_DEVICE_CONTROL,
                        intent=intent,
                        control_ref=error.control_ref,
                        error=error,
                    )
                )
                continue

            planned_binding = self._plan_binding(
                binding=resolved.binding,
                device=device,
                action_instance_id=dynamic_child_action_instance_id(
                    self._controller_id,
                    self._config_id,
                    page_session=page_session,
                    page_session_generation=page_session_generation,
                    child=child,
                    binding=resolved.binding,
                ),
                action_metadata=effective_action_metadata,
                action_status=action_status or {},
                retained_plan=retained_plan,
                page_session_id=page_session.page_session_id,
                settings_target_enabled=False,
                item_key=child.item_key,
                handler=child.handler,
                child=child,
                internal=child.internal,
            )
            planned.append(planned_binding)
            outcomes.append(self._binding_outcome(planned_binding))

        validation_errors.extend(_control_conflicts(planned, "_dynamic", entry.page_id))
        if validation_errors:
            return PagePlanBuildResult(
                plan=None,
                outcomes=tuple(outcomes),
                validation_errors=tuple(validation_errors),
            )
        return PagePlanBuildResult(
            plan=PagePlan(
                entry=entry,
                profile_id="_dynamic",
                page_id=entry.page_id,
                page_session=page_session,
                bindings=tuple(planned),
            ),
            outcomes=tuple(outcomes),
        )

    def configured_action_intent_key(
        self,
        binding: ConfiguredControlBinding,
    ) -> ActionIntentKey:
        return ActionIntentKey(
            action_id=binding.action_id,
            provider_instance_id=binding.provider_instance_id,
            provider_labels=tuple(sorted(binding.provider_labels.items())),
        )

    def resolved_action_intent_key(
        self,
        binding: ResolvedControlBinding,
    ) -> ActionIntentKey:
        return ActionIntentKey(
            action_id=binding.action_id,
            provider_instance_id=binding.provider_instance_id,
            provider_labels=tuple(sorted(binding.provider_labels.items())),
        )

    def _plan_binding(
        self,
        *,
        binding: ResolvedControlBinding,
        device: DeviceDescriptor,
        action_instance_id: str,
        action_metadata: Mapping[ActionIntentKey, ActionMetadata],
        action_status: Mapping[ActionIntentKey, BindingPlanStatus],
        retained_plan: PagePlan | None,
        page_session_id: str | None,
        settings_target_enabled: bool,
        item_key: str | None = None,
        handler: str | None = None,
        child: PageChildBindingDescriptor | None = None,
        internal: Mapping[str, Any] | None = None,
    ) -> PlannedBinding:
        action_meta = self._action_metadata_for_binding(
            action_metadata=action_metadata,
            action_status=action_status,
            retained_plan=retained_plan,
            binding=binding,
            action_instance_id=action_instance_id,
        )
        status = (
            BindingPlanStatus.BOUND
            if action_meta is not None
            else action_status.get(
                self.resolved_action_intent_key(binding),
                BindingPlanStatus.UNAVAILABLE,
            )
        )
        if action_meta is not None:
            try:
                matches = select_requirements(binding, action_meta.requirements, device)
            except ValueError:
                status = BindingPlanStatus.INVALID_DEVICE_CONTROL
            else:
                selected = frozenset(cap.capability_id for _name, cap in matches)
                binding = replace(
                    binding,
                    **{
                        name: getattr(binding, name) & selected
                        for name in (
                            "input_capability_ids",
                            "output_capability_ids",
                            "state_capability_ids",
                            "config_capability_ids",
                            "diagnostic_capability_ids",
                        )
                    },
                )
        return PlannedBinding(
            binding=binding,
            action_instance_id=action_instance_id,
            status=status,
            action_meta=action_meta,
            page_session_id=page_session_id,
            settings_target_enabled=settings_target_enabled,
            item_key=item_key,
            handler=handler,
            child=child,
            internal=dict(internal or {}),
        )

    def _action_metadata_for_binding(
        self,
        *,
        action_metadata: Mapping[ActionIntentKey, ActionMetadata],
        action_status: Mapping[ActionIntentKey, BindingPlanStatus],
        retained_plan: PagePlan | None,
        binding: ResolvedControlBinding,
        action_instance_id: str,
    ) -> ActionMetadata | None:
        intent = self.resolved_action_intent_key(binding)
        provided = action_metadata.get(intent)
        if provided is not None:
            return provided
        if intent in action_status:
            return None
        return self._retained_action_metadata(
            retained_plan=retained_plan,
            binding=binding,
            action_instance_id=action_instance_id,
        )

    def _retained_action_metadata(
        self,
        *,
        retained_plan: PagePlan | None,
        binding: ResolvedControlBinding,
        action_instance_id: str,
    ) -> ActionMetadata | None:
        if retained_plan is None:
            return None
        intent_key = self.resolved_action_intent_key(binding)
        for planned in retained_plan.bindings:
            if planned.action_instance_id != action_instance_id:
                continue
            if self.resolved_action_intent_key(planned.binding) != intent_key:
                continue
            return planned.action_meta
        return None

    def _dynamic_page_child_binding(
        self,
        binding: PageChildBindingDescriptor,
        *,
        owner_action_id: str,
        owner_provider_instance_id: str,
    ) -> ConfiguredControlBinding | None:
        target = binding.target
        if target.kind == "self":
            action_id = owner_action_id
            provider_instance_id = owner_provider_instance_id
            provider_labels: Mapping[str, str] | None = None
        else:
            if target.action_id is None:
                return None
            action_id = target.action_id
            provider_instance_id = target.provider_instance_id
            provider_labels = target.provider_labels

        return exact_control_binding(
            control_id=binding.control_id,
            action_id=action_id,
            provider_instance_id=provider_instance_id,
            provider_labels=provider_labels,
            settings=binding.settings,
        )

    def _binding_outcome(self, planned: PlannedBinding) -> BindingPlanOutcome:
        return BindingPlanOutcome(
            status=planned.status,
            intent=self.resolved_action_intent_key(planned.binding),
            control_ref=planned.control_id,
            control_id=planned.control_id,
            action_instance_id=planned.action_instance_id,
            planned=planned,
        )

    def _resolution_error(
        self,
        *,
        binding: ConfiguredControlBinding,
        code: str | None,
        message: str | None,
        details: tuple[str, ...],
        profile_id: str,
        page_id: str,
    ) -> ValidationError:
        return ValidationError(
            code=code or "control_not_found",
            message=message or "control selector did not resolve",
            control_ref=", ".join(details) or "<selector>",
            action_id=binding.action_id,
            profile_id=profile_id,
            page_id=page_id,
            details=list(details),
        )


def _control_conflicts(bindings, profile_id, page_id) -> list[ValidationError]:
    """A page must resolve exclusive control ownership before issuing grants."""
    seen = set()
    errors = []
    for planned in bindings:
        if planned.control_id in seen:
            errors.append(
                ValidationError(
                    code="duplicate_control",
                    message="multiple bindings select one control",
                    control_ref=planned.control_id,
                    action_id=planned.binding.action_id,
                    profile_id=profile_id,
                    page_id=page_id,
                )
            )
        seen.add(planned.control_id)
    return errors
