from dataclasses import replace

from deckr.controller._binding_plan import BindingPlanStatus as Status
from deckr.controller._bindings._decisions import (
    BindingChange as Change,
)
from deckr.controller._bindings._decisions import (
    InstalledBindingFacts,
    binding_decision,
)

CURRENT = InstalledBindingFacts(True, True, True, True, True, False)


def test_pending_catalog_does_not_revoke_a_live_agreement():
    assert binding_decision(Status.PENDING, CURRENT).change == Change.RETAIN
    lost = binding_decision(Status.PENDING, replace(CURRENT, current_agreement=False))
    assert lost.retire and lost.change == Change.PENDING


def test_requirements_changes_and_successors_have_different_cleanup():
    requirements = binding_decision(
        Status.BOUND, replace(CURRENT, same_requirements=False)
    )
    successor = binding_decision(
        Status.BOUND, replace(CURRENT, current_agreement=False)
    )
    assert requirements.retire and not requirements.destroy_instance
    assert successor.retire and successor.destroy_instance
    assert requirements.change == successor.change == Change.REPLACE


def test_invalid_desired_binding_never_preserves_an_installed_grant():
    for status in (Status.INVALID_CONFIG, Status.INVALID_DEVICE_CONTROL):
        assert binding_decision(status, CURRENT).retire
        assert binding_decision(status, CURRENT).change == Change.INVALID
