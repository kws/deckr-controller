from __future__ import annotations

import pytest
from deckr.hardware.descriptors import (
    CapabilityDescriptor,
    ControlDescriptor,
    ControlGeometry,
)

from deckr.controller._binding_resolution import (
    ConfiguredControlBinding,
    _selector_summary,
    resolve_binding,
)
from deckr.controller.config import (
    CapabilitySelector,
    ControlSelector,
    GeometrySelector,
)


def _capability(
    capability_id: str,
    *,
    family: str = "family",
    capability_type: str = "button",
    direction: str = "input",
    event_types: tuple[str, ...] = (),
    command_types: tuple[str, ...] = (),
) -> CapabilityDescriptor:
    access = ("emits",) if direction == "input" else ("settable",)
    return CapabilityDescriptor(
        capabilityId=capability_id,
        family=family,
        type=capability_type,
        direction=direction,
        access=access,
        eventTypes=event_types,
        commandTypes=command_types,
    )


def _control(**updates) -> ControlDescriptor:
    data = {
        "controlId": "control-1",
        "kind": "key",
        "label": "Primary",
        "groupId": "group-a",
        "parentControlId": "parent-a",
        "surfaceId": "surface-a",
        "geometry": ControlGeometry(x=1, y=2, width=1, height=1),
        "inputCapabilities": (
            _capability("input-a", family="deckr.input", event_types=("down", "up")),
            _capability("input-b", family="deckr.input.alt", event_types=("tap",)),
        ),
        "outputCapabilities": (
            _capability(
                "output-a",
                family="deckr.output",
                capability_type="raster",
                direction="output",
                command_types=("set_frame", "clear"),
            ),
            _capability(
                "output-b",
                family="deckr.output.alt",
                capability_type="status",
                direction="output",
                command_types=("set",),
            ),
        ),
    }
    data.update(updates)
    return ControlDescriptor(**data)


def _binding(selector: ControlSelector) -> ConfiguredControlBinding:
    return ConfiguredControlBinding(
        selector=selector,
        action_uuid="action.clock",
        provider_instance_id=None,
        provider_labels={},
        settings={},
    )


@pytest.mark.parametrize(
    "selector",
    [
        ControlSelector(geometry=GeometrySelector(x=2)),
        ControlSelector(geometry=GeometrySelector(x=1)),
    ],
)
def test_geometry_selector_mismatch_and_missing_control_geometry(selector) -> None:
    control = (
        _control()
        if selector.geometry.x == 2
        else _control(geometry=None)
    )

    result = resolve_binding(_binding(selector), (control,))

    assert result.ok is False
    assert result.code == "control_not_found"
    assert result.details == ("geometry",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "dial"),
        ("group_id", "group-b"),
        ("parent_control_id", "parent-b"),
        ("surface_id", "surface-b"),
        ("label", "Secondary"),
    ],
)
def test_selector_mismatch_by_descriptor_field(field: str, value: str) -> None:
    result = resolve_binding(
        _binding(ControlSelector(**{field: value})),
        (_control(),),
    )

    assert result.ok is False
    assert result.code == "control_not_found"
    assert result.details == (f"{field}={value!r}",)


def test_missing_generic_capability_requirement() -> None:
    result = resolve_binding(
        _binding(
            ControlSelector(
                capabilities=(CapabilitySelector(family="missing.family"),)
            )
        ),
        (_control(),),
    )

    assert result.ok is False
    assert result.code == "capability_not_found"
    assert result.details == ("family=missing.family",)


def test_missing_bucket_specific_capability_requirement() -> None:
    result = resolve_binding(
        _binding(
            ControlSelector(
                output=(CapabilitySelector(family="missing.output"),)
            )
        ),
        (_control(),),
    )

    assert result.ok is False
    assert result.code == "capability_not_found"
    assert result.details == ("output:family=missing.output",)


def test_event_type_and_command_type_subset_mismatch() -> None:
    result = resolve_binding(
        _binding(
            ControlSelector(
                input=(CapabilitySelector(event_types=("down", "hold")),),
                output=(CapabilitySelector(command_types=("set_frame", "flash")),),
            )
        ),
        (_control(),),
    )

    assert result.ok is False
    assert result.details == (
        "input:events=down,hold",
        "output:commands=set_frame,flash",
    )


def test_selected_capability_ids_with_and_without_requirements() -> None:
    control = _control()

    unfiltered = resolve_binding(_binding(ControlSelector(kind="key")), (control,))
    assert unfiltered.binding is not None
    assert unfiltered.binding.input_capability_ids == frozenset({"input-a", "input-b"})
    assert unfiltered.binding.output_capability_ids == frozenset(
        {"output-a", "output-b"}
    )

    filtered = resolve_binding(
        _binding(
            ControlSelector(
                kind="key",
                capabilities=(CapabilitySelector(family="deckr.output"),),
                input=(CapabilitySelector(capability_id="input-b"),),
            )
        ),
        (control,),
    )
    assert filtered.binding is not None
    assert filtered.binding.input_capability_ids == frozenset({"input-b"})
    assert filtered.binding.output_capability_ids == frozenset({"output-a"})


def test_selector_summary_for_empty_geometry_and_capability_selectors() -> None:
    empty = ControlSelector.model_construct(
        control_id=None,
        kind=None,
        group_id=None,
        parent_control_id=None,
        surface_id=None,
        label=None,
        geometry=None,
        capabilities=(),
        input_capabilities=(),
        output_capabilities=(),
        state_capabilities=(),
        config_capabilities=(),
        diagnostic_capabilities=(),
    )
    geometry = ControlSelector(geometry=GeometrySelector(x=1))
    capability = ControlSelector(
        capabilities=(
            CapabilitySelector(
                family="family",
                type="kind",
                direction="input",
                event_types=("down",),
                command_types=("set",),
            ),
        )
    )

    assert _selector_summary(empty) == "<empty>"
    assert _selector_summary(geometry) == "geometry"
    assert _selector_summary(capability) == (
        "family=family type=kind direction=input events=down commands=set"
    )
