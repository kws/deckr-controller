from __future__ import annotations

from deckr.controller._action_interest import (
    ActionInterestPolicy,
    ActionInterestSource,
    ActionInterestTracker,
)
from deckr.controller._binding_planner import ActionIntentKey


def _intent(
    action_uuid: str,
    *,
    provider_instance_id: str | None = None,
    provider_labels: dict[str, str] | None = None,
) -> ActionIntentKey:
    return ActionIntentKey(
        action_uuid=action_uuid,
        provider_instance_id=provider_instance_id,
        provider_labels=tuple(sorted((provider_labels or {}).items())),
    )


def test_removed_strong_interest_is_demoted_to_warm_until_retention_expires():
    tracker = ActionInterestTracker(
        policy=ActionInterestPolicy(warm_retention_seconds=30.0)
    )
    alpha = _intent("action.alpha")
    beta = _intent("action.beta")

    tracker.replace_strong_interests(
        ActionInterestSource.VISIBLE_BINDING,
        (alpha, beta),
        now=100.0,
    )
    tracker.replace_strong_interests(
        ActionInterestSource.VISIBLE_BINDING,
        (beta,),
        now=110.0,
    )

    retained = tracker.snapshot(now=139.0)
    assert retained.strong_intents == (beta,)
    assert retained.warm_intents == (alpha,)

    expired = tracker.snapshot(now=141.0)
    assert expired.strong_intents == (beta,)
    assert expired.warm_intents == ()
    assert expired.all_intents == (beta,)


def test_zero_warm_retention_removes_cleared_source_interests():
    tracker = ActionInterestTracker(
        policy=ActionInterestPolicy(warm_retention_seconds=0.0)
    )
    action = _intent("action.alpha")

    tracker.replace_strong_interests(
        ActionInterestSource.VISIBLE_BINDING,
        (action,),
        now=10.0,
    )
    tracker.clear_source(ActionInterestSource.VISIBLE_BINDING, now=11.0)

    assert tracker.snapshot(now=11.0).records == ()


def test_replace_warm_interests_removes_absent_warm_records():
    tracker = ActionInterestTracker(
        policy=ActionInterestPolicy(warm_retention_seconds=30.0)
    )
    alpha = _intent("action.alpha")
    beta = _intent("action.beta")

    tracker.replace_warm_interests(
        ActionInterestSource.PREWARM,
        (alpha, beta),
        now=10.0,
    )
    tracker.replace_warm_interests(
        ActionInterestSource.PREWARM,
        (beta,),
        now=20.0,
    )

    assert tracker.snapshot(now=20.0).warm_intents == (beta,)


