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
    TitleOptions,
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
                            Control(slot="0,0", action="action.a", settings={"x": 1}),
                            Control(slot="0,1", action="action.b", settings={}),
                        ]
                    ),
                    Page(
                        controls=[
                            Control(slot="1,0", action="action.c", settings={}),
                        ]
                    ),
                ],
            ),
        ],
    )


def test_set_page_initial_returns_transition(device_config):
    nav = NavigationService(device_config)
    ref = StaticPageRef(profile_name="default", page_index=0)
    transition = nav.set_page(ref)
    assert transition.departing is None
    assert transition.arriving == ref
    assert nav.current_page == ref


def test_set_page_replaces_current(device_config):
    nav = NavigationService(device_config)
    ref0 = StaticPageRef(profile_name="default", page_index=0)
    ref1 = StaticPageRef(profile_name="default", page_index=1)
    nav.set_page(ref0)
    transition = nav.set_page(ref1)
    assert transition.departing == ref0
    assert transition.arriving == ref1
    assert nav.current_page == ref1


def test_resolve_static_bindings_returns_slot_bindings(device_config):
    nav = NavigationService(device_config)
    ref = StaticPageRef(profile_name="default", page_index=0)
    bindings = nav.resolve_static_bindings(ref)
    assert len(bindings) == 2
    assert bindings[0].slot_id == "0,0"
    assert bindings[0].action_uuid == "action.a"
    assert bindings[0].settings == {"x": 1}
    assert bindings[1].slot_id == "0,1"
    assert bindings[1].action_uuid == "action.b"


def test_resolve_static_bindings_includes_title_options():
    """When Control has title_options, SlotBinding receives converted TitleOptions."""
    config = DeviceConfig(
        id="dev1",
        name="Test",
        match={"fingerprint": "fingerprint-dev1"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                slot="0,0",
                                action="action.a",
                                settings={},
                                title_options=TitleOptions(
                                    font_family="Roboto Mono",
                                    font_size=36,
                                    title_color="#00FF00",
                                    title_alignment="top",
                                ),
                            ),
                        ]
                    ),
                ],
            ),
        ],
    )
    nav = NavigationService(config)
    bindings = nav.resolve_static_bindings(
        StaticPageRef(profile_name="default", page_index=0)
    )
    assert len(bindings) == 1
    assert bindings[0].title_options is not None
    assert bindings[0].title_options.font_family == "Roboto Mono"
    assert bindings[0].title_options.font_size == 36
    assert bindings[0].title_options.title_color == "#00FF00"
    assert bindings[0].title_options.title_alignment == "top"
