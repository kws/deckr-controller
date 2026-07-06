"""Tests for NavigationService: current page and transitions (no stack)."""

import pytest

from deckr.controller._navigation_service import (
    NavigationService,
    StaticPageRef,
)
from deckr.controller.config._data import (
    Control,
    DeviceConfig,
    Page,
    Profile,
)


@pytest.fixture
def device_config():
    return DeviceConfig(
        id="dev1",
        name="Test Device",
        match={"fingerprint": "fingerprint-dev1"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action="action.a",
                                settings={"x": 1},
                            ),
                            Control(
                                selector={"control_id": "0,1"},
                                action="action.b",
                                settings={},
                            ),
                        ]
                    ),
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "1,0"},
                                action="action.c",
                                settings={},
                            ),
                        ]
                    ),
                ],
            ),
        ],
    )


def test_resolve_static_bindings_returns_control_bindings(device_config):
    nav = NavigationService(device_config)
    ref = StaticPageRef(profile_name="default", page_index=0)
    bindings = nav.resolve_static_bindings(ref)
    assert len(bindings) == 2
    assert bindings[0].control_id == "0,0"
    assert bindings[0].action_uuid == "action.a"
    assert bindings[0].settings == {"x": 1}
    assert bindings[1].control_id == "0,1"
    assert bindings[1].action_uuid == "action.b"


