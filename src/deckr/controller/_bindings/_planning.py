import logging

from deckr.actions.messages import (
    DynamicPageCommand,
)

from deckr.controller._actions import (
    ActionIntentKey,
    ActionPlanningSnapshot,
    ProviderActionKey,
)
from deckr.controller._binding_planner import (
    BindingPlanStatus,
    PagePlan,
    format_validation_summary,
)
from deckr.controller._pages import (
    DynamicPageSession,
    PageStackEntry,
    StaticPageRef,
)

logger = logging.getLogger(__name__)


class PagePlanning:
    """Build desired bindings from local navigation, descriptor and catalog snapshots."""

    def __init__(self, *, state, catalog, planner, clock):
        self.state = state
        self.catalog = catalog
        self.planner = planner
        self.clock = clock

    def _action_availability_change_affects_plan(
        self,
        changed_keys: frozenset[ProviderActionKey],
        plan: PagePlan,
    ) -> bool:
        if not changed_keys:
            return True
        exact_keys = set(self._existing_provider_action_keys())
        for planned in plan.bindings:
            if planned.action_meta is None:
                continue
            exact_keys.add(
                ProviderActionKey(
                    planned.action_meta.provider_instance_id,
                    planned.action_meta.uuid,
                )
            )
        if changed_keys & exact_keys:
            return True

        intents = tuple(
            self.planner.resolved_action_intent_key(planned.binding)
            for planned in plan.bindings
        )
        return any(
            self._provider_action_key_matches_intent(key, intent)
            for key in changed_keys
            for intent in intents
        )

    def _provider_action_key_matches_intent(
        self,
        key: ProviderActionKey,
        intent: ActionIntentKey,
    ) -> bool:
        if key.action_uuid != intent.action_uuid:
            return False
        if intent.provider_instance_id is not None:
            return key.provider_instance_id == intent.provider_instance_id
        if not intent.provider_labels:
            return True
        record = self.catalog.record_for_key(key)
        if record is None or record.metadata is None:
            return True
        labels = record.metadata.provider_labels or {}
        return all(labels.get(name) == value for name, value in intent.provider_labels)

    async def _action_metadata_snapshot_for_plan(
        self,
        intents: tuple[ActionIntentKey, ...],
        *,
        refresh_actions: bool,
    ) -> ActionPlanningSnapshot:
        snapshot = self.catalog.planning_snapshot(
            intents,
            existing_provider_keys=self._existing_provider_action_keys(),
            now=self.clock(),
        )
        if not refresh_actions:
            return snapshot
        return snapshot

    def _existing_provider_action_keys(self) -> frozenset[ProviderActionKey]:
        return frozenset(
            ProviderActionKey(lease.provider_instance_id, lease.action_uuid)
            for lease in self.state.attachments.binding_leases.values()
        )

    def _log_static_page_plan_rejection(
        self,
        result_errors,
    ) -> None:
        logger.error(
            "Page transition rejected (capability validation): %s",
            format_validation_summary(list(result_errors)),
        )
        for err in result_errors:
            logger.error(
                "Binding validation failed [%s]: %s (control=%s action=%s) %s",
                err.code,
                err.message,
                err.control_ref,
                err.action_uuid,
                err.details,
            )

    def _log_dynamic_page_plan_rejection(
        self,
        result_errors,
    ) -> None:
        logger.error(
            "Dynamic page transition rejected (capability validation): %s",
            format_validation_summary(list(result_errors)),
        )
        for err in result_errors:
            logger.error(
                "Dynamic page binding validation failed [%s]: %s (control=%s action=%s) %s",
                err.code,
                err.message,
                err.control_ref,
                err.action_uuid,
                err.details,
            )

    async def _build_static_page_plan(
        self,
        entry: StaticPageRef,
        *,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> PagePlan | None:
        bindings = self.state.navigation.resolve_static_bindings(entry)
        action_metadata = await self._action_metadata_snapshot_for_plan(
            self.planner.static_action_intents(bindings),
            refresh_actions=refresh_actions,
        )
        action_status = self._binding_plan_status(action_metadata)
        result = self.planner.build_static_page_plan(
            entry,
            bindings=bindings,
            device=self.state.device,
            action_metadata=action_metadata.metadata,
            action_status=action_status,
            retained_plan=retained_plan,
        )
        if result.plan is None:
            self._log_static_page_plan_rejection(result.validation_errors)
        return result.plan

    async def _build_dynamic_page_plan(
        self,
        entry: DynamicPageCommand,
        *,
        page_session: DynamicPageSession,
        page_session_generation: int | None = None,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> PagePlan | None:
        action_metadata = await self._action_metadata_snapshot_for_plan(
            self.planner.dynamic_action_intents(
                entry.bindings,
                owner_action_uuid=page_session.owner_action_uuid,
                owner_provider_instance_id=page_session.owner_provider_instance_id,
            ),
            refresh_actions=refresh_actions,
        )
        action_status = self._binding_plan_status(action_metadata)
        result = self.planner.build_dynamic_page_plan(
            entry,
            device=self.state.device,
            page_session=page_session,
            page_session_generation=page_session_generation,
            action_metadata=action_metadata.metadata,
            action_status=action_status,
            retained_plan=retained_plan,
        )
        if result.plan is None:
            self._log_dynamic_page_plan_rejection(result.validation_errors)
        return result.plan

    async def _build_page_plan(
        self,
        entry: PageStackEntry,
        *,
        page_session: DynamicPageSession | None = None,
        page_session_generation: int | None = None,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> PagePlan | None:
        if isinstance(entry, StaticPageRef):
            return await self._build_static_page_plan(
                entry,
                retained_plan=retained_plan,
                refresh_actions=refresh_actions,
            )
        if page_session is None:
            logger.error("Dynamic page planning missing page session")
            return None
        return await self._build_dynamic_page_plan(
            entry,
            page_session=page_session,
            page_session_generation=page_session_generation,
            retained_plan=retained_plan,
            refresh_actions=refresh_actions,
        )

    def _binding_plan_status(
        self,
        snapshot: ActionPlanningSnapshot,
    ) -> dict[ActionIntentKey, BindingPlanStatus]:
        status: dict[ActionIntentKey, BindingPlanStatus] = {
            intent: BindingPlanStatus.PENDING for intent in snapshot.pending
        }
        status.update(
            {intent: BindingPlanStatus.UNAVAILABLE for intent in snapshot.unavailable}
        )
        return status
