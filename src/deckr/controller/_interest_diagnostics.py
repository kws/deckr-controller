import logging

from deckr.controller._action_interest import (
    ActionInterestSnapshot,
    ActionInterestSource,
    ActionInterestTracker,
)
from deckr.controller._actions import (
    ActionIntentKey,
)
from deckr.controller._bindings._models import (
    _dedupe_action_intents,
)
from deckr.controller._pages import (
    StaticPageRef,
)

logger = logging.getLogger(__name__)


class InterestDiagnostics:
    """Diagnostic interest snapshots. These do not select watchers or retain resources."""

    def __init__(self, *, state, catalog, planner, clock):
        self.state = state
        self.catalog = catalog
        self.planner = planner
        self.clock = clock
        self.tracker = ActionInterestTracker(clock=clock)

    def action_interest_snapshot(
        self,
        *,
        now: float | None = None,
    ) -> ActionInterestSnapshot:
        return self.tracker.snapshot(now=now)

    def _sync_action_interest(self) -> None:
        now = self.clock()
        if self.state.navigation.config_active:
            self.tracker.replace_strong_interests(
                ActionInterestSource.CONNECTED_CONFIG,
                self._configured_action_intents(),
                now=now,
            )
        else:
            self.tracker.clear_source(
                ActionInterestSource.CONNECTED_CONFIG,
                now=now,
            )

        visible_source = self._visible_action_interest_source()
        visible_intents = self._current_plan_action_intents()
        if visible_source == ActionInterestSource.DYNAMIC_PAGE:
            self.tracker.clear_source(
                ActionInterestSource.VISIBLE_BINDING,
                now=now,
            )
            self.tracker.replace_strong_interests(
                ActionInterestSource.DYNAMIC_PAGE,
                visible_intents,
                now=now,
            )
            self._publish_action_interest_snapshot(now=now)
            return

        self.tracker.replace_strong_interests(
            ActionInterestSource.VISIBLE_BINDING,
            visible_intents,
            now=now,
        )
        self.tracker.clear_source(
            ActionInterestSource.DYNAMIC_PAGE,
            now=now,
        )
        self._publish_action_interest_snapshot(now=now)

    def _publish_action_interest_snapshot(self, *, now: float) -> None:
        if self.state.navigation.config_active:
            self.catalog.update_config_interest(
                self.state.config.id,
                self.tracker.snapshot(now=now),
            )
        else:
            self.catalog.clear_config_interest(self.state.config.id)

    def _visible_action_interest_source(self) -> ActionInterestSource:
        current_plan = self.state.navigation.current_plan()
        if current_plan is not None and current_plan.page_session is not None:
            return ActionInterestSource.DYNAMIC_PAGE
        return ActionInterestSource.VISIBLE_BINDING

    def _configured_action_intents(self) -> tuple[ActionIntentKey, ...]:
        intents: list[ActionIntentKey] = []
        for profile in self.state.config.profiles:
            for page_index in range(len(profile.pages)):
                bindings = self.state.navigation.resolve_static_bindings(
                    StaticPageRef(
                        profile_name=profile.name,
                        page_index=page_index,
                    )
                )
                intents.extend(self.planner.static_action_intents(bindings))
        return _dedupe_action_intents(intents)

    def _current_plan_action_intents(self) -> tuple[ActionIntentKey, ...]:
        current_plan = self.state.navigation.current_plan()
        if current_plan is None:
            return ()
        return _dedupe_action_intents(
            self.planner.resolved_action_intent_key(planned.binding)
            for planned in current_plan.bindings
        )
