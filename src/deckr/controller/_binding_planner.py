"""Pure page binding planning for the controller runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from deckr.actions.messages import (
    DynamicPageCommand,
    PageChildBindingDescriptor,
    SettingsTargetRef,
)
from deckr.hardware.descriptors import DeviceDescriptor

from deckr.controller._binding_resolution import (
    ConfiguredControlBinding,
    ResolvedControlBinding,
    exact_control_binding,
    resolve_binding,
)
from deckr.controller._binding_validator import ValidationError
from deckr.controller._navigation_service import PageStackEntry, StaticPageRef
from deckr.controller.action_provider.provider import ActionMetadata
from deckr.controller.settings import derive_action_instance_id


class BindingPlanStatus(StrEnum):
    BOUND = "bound"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"
    INVALID_CONFIG = "invalid_config"
    INVALID_DEVICE_CONTROL = "invalid_device_control"


@dataclass(frozen=True, slots=True)
class ActionIntentKey:
    action_uuid: str
    provider_instance_id: str | None
    provider_labels: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class DynamicPageSession:
    page_id: str
    page_session_id: str
    context_id: str
    action_instance_id: str
    owner_context_id: str
    owner_binding_id: str
    owner_control_id: str
    owner_action_uuid: str
    owner_provider_instance_id: str
    owner_provider_id: str
    owner_provider_session_id: str | None
    owner_action_meta: ActionMetadata
    owner_profile: str
    owner_page: int
    timeout_ms: int
    last_activity: float
    settings_target: SettingsTargetRef | None


@dataclass(slots=True)
class PlannedBinding:
    binding: ResolvedControlBinding
    action_instance_id: str
    status: BindingPlanStatus
    action_meta: ActionMetadata | None
    page_session_id: str | None
    persist_settings: bool
    item_key: str | None = None
    handler: str | None = None
    child: PageChildBindingDescriptor | None = None

    @property
    def control_id(self) -> str:
        return self.binding.control_id


@dataclass(slots=True)
class PagePlan:
    entry: PageStackEntry
    profile_id: str
    page_id: str
    page_session: DynamicPageSession | None
    bindings: tuple[PlannedBinding, ...]


@dataclass(slots=True)
class PageFrame:
    entry: PageStackEntry
    page_session: DynamicPageSession | None
    committed_plan: PagePlan


@dataclass(slots=True)
class BindingPlanOutcome:
    status: BindingPlanStatus
    intent: ActionIntentKey
    control_ref: str
    control_id: str | None = None
    action_instance_id: str | None = None
    planned: PlannedBinding | None = None
    error: ValidationError | None = None


@dataclass(slots=True)
class PagePlanBuildResult:
    plan: PagePlan | None
    outcomes: tuple[BindingPlanOutcome, ...]
    validation_errors: tuple[ValidationError, ...] = ()


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
        owner_action_uuid: str,
        owner_provider_instance_id: str,
    ) -> tuple[ActionIntentKey, ...]:
        intents: list[ActionIntentKey] = []
        for child in children:
            binding = self._dynamic_page_child_binding(
                child,
                owner_action_uuid=owner_action_uuid,
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
                action_instance_id=self._static_action_instance_id(
                    entry,
                    resolved.binding,
                ),
                action_metadata=action_metadata,
                action_status=action_status or {},
                retained_plan=retained_plan,
                page_session_id=None,
                persist_settings=True,
            )
            planned.append(planned_binding)
            outcomes.append(self._binding_outcome(planned_binding))

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
        action_metadata: Mapping[ActionIntentKey, ActionMetadata],
        action_status: Mapping[ActionIntentKey, BindingPlanStatus] | None = None,
        retained_plan: PagePlan | None = None,
    ) -> PagePlanBuildResult:
        planned: list[PlannedBinding] = []
        outcomes: list[BindingPlanOutcome] = []
        validation_errors: list[ValidationError] = []
        effective_action_metadata = dict(action_metadata)
        effective_action_metadata[
            ActionIntentKey(
                action_uuid=page_session.owner_action_uuid,
                provider_instance_id=page_session.owner_provider_instance_id,
                provider_labels=(),
            )
        ] = page_session.owner_action_meta

        for child in entry.bindings:
            binding = self._dynamic_page_child_binding(
                child,
                owner_action_uuid=page_session.owner_action_uuid,
                owner_provider_instance_id=page_session.owner_provider_instance_id,
            )
            if binding is None:
                error = ValidationError(
                    code="invalid_child_target",
                    message="action page child target missing actionId",
                    control_ref=child.control_id,
                    action_uuid="",
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
                action_instance_id=self._dynamic_child_action_instance_id(
                    page_session=page_session,
                    child=child,
                    binding=resolved.binding,
                ),
                action_metadata=effective_action_metadata,
                action_status=action_status or {},
                retained_plan=retained_plan,
                page_session_id=page_session.page_session_id,
                persist_settings=False,
                item_key=child.item_key,
                handler=child.handler,
                child=child,
            )
            planned.append(planned_binding)
            outcomes.append(self._binding_outcome(planned_binding))

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
            action_uuid=binding.action_uuid,
            provider_instance_id=binding.provider_instance_id,
            provider_labels=tuple(sorted(binding.provider_labels.items())),
        )

    def resolved_action_intent_key(
        self,
        binding: ResolvedControlBinding,
    ) -> ActionIntentKey:
        return ActionIntentKey(
            action_uuid=binding.action_uuid,
            provider_instance_id=binding.provider_instance_id,
            provider_labels=tuple(sorted(binding.provider_labels.items())),
        )

    def _plan_binding(
        self,
        *,
        binding: ResolvedControlBinding,
        action_instance_id: str,
        action_metadata: Mapping[ActionIntentKey, ActionMetadata],
        action_status: Mapping[ActionIntentKey, BindingPlanStatus],
        retained_plan: PagePlan | None,
        page_session_id: str | None,
        persist_settings: bool,
        item_key: str | None = None,
        handler: str | None = None,
        child: PageChildBindingDescriptor | None = None,
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
        return PlannedBinding(
            binding=binding,
            action_instance_id=action_instance_id,
            status=status,
            action_meta=action_meta,
            page_session_id=page_session_id,
            persist_settings=persist_settings,
            item_key=item_key,
            handler=handler,
            child=child,
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

    def _static_action_instance_id(
        self,
        entry: StaticPageRef,
        binding: ResolvedControlBinding,
    ) -> str:
        return derive_action_instance_id(
            controller_id=self._controller_id,
            config_id=self._config_id,
            action_id=binding.action_uuid,
            stable_id=binding.stable_id,
            profile_id=entry.profile_name,
            page_id=str(entry.page_index),
            control_id=binding.control_id,
        )

    def _dynamic_child_action_instance_id(
        self,
        *,
        page_session: DynamicPageSession,
        child: PageChildBindingDescriptor,
        binding: ResolvedControlBinding,
    ) -> str:
        if child.target.kind == "self":
            return page_session.action_instance_id

        provider_key = binding.provider_instance_id or ""
        if binding.provider_labels:
            provider_key = "|".join(
                (
                    provider_key,
                    *(
                        f"{key}={value}"
                        for key, value in sorted(binding.provider_labels.items())
                    ),
                )
            )
        target_key = child.target.instance_key or child.control_id
        stable_id = "\x1f".join(
            (
                "dynamic-page",
                page_session.page_session_id,
                provider_key,
                target_key,
            )
        )
        return derive_action_instance_id(
            controller_id=self._controller_id,
            config_id=self._config_id,
            action_id=binding.action_uuid,
            stable_id=stable_id,
        )

    def _dynamic_page_child_binding(
        self,
        binding: PageChildBindingDescriptor,
        *,
        owner_action_uuid: str,
        owner_provider_instance_id: str,
    ) -> ConfiguredControlBinding | None:
        target = binding.target
        if target.kind == "self":
            action_uuid = owner_action_uuid
            provider_instance_id = owner_provider_instance_id
            provider_labels: Mapping[str, str] | None = None
        else:
            if target.action_id is None:
                return None
            action_uuid = target.action_id
            provider_instance_id = target.provider_instance_id
            provider_labels = target.provider_labels

        return exact_control_binding(
            control_id=binding.control_id,
            action_uuid=action_uuid,
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
            action_uuid=binding.action_uuid,
            profile_id=profile_id,
            page_id=page_id,
            details=list(details),
        )
