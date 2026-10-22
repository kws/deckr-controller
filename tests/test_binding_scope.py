"""Retired work and changed requirements cannot retain or acquire binding authority."""

from dataclasses import replace

import pytest
from deckr.actions.catalog import ActionDescriptor
from pydantic import ValidationError

from deckr.controller.config import Control, Page, Profile
from test_support.action_catalog import _metadata
from test_support.device_session import session


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["navigate", "disconnect"])
async def test_overlay_expiry_cannot_modify_retired_binding(retirement):
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        old = device.context_for_control("0,0")
        await old.show_overlay(
            template="ok",
            title="old",
            params={},
            duration_seconds=30,
            overlay_id="overlay",
            generation=1,
            binding_output_generation=0,
        )
        revision = old.presentation._overlay_revision
        if retirement == "navigate":
            await device.set_page(page=1)
            await device.wait_idle()
        else:
            await device.aclose()
        dispatcher = device.components.dispatcher
        state = dispatcher._controls["0,0"]
        owner = (state.context_id, state.binding_id, state.generation)
        await old.presentation._clear_overlay_after("overlay", 1, 0, revision)
        assert (state.context_id, state.binding_id, state.generation) == owner
        assert old.presentation._overlay_timer is None


@pytest.mark.asyncio
async def test_replaced_overlay_with_same_wire_generation_ignores_old_expiry():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        ctx = device.context_for_control("0,0")
        for title in ("first", "second"):
            await ctx.show_overlay(
                template="ok",
                title=title,
                params={},
                duration_seconds=30,
                overlay_id="overlay",
                generation=1,
                binding_output_generation=0,
            )
            if title == "first":
                first_revision = ctx.presentation._overlay_revision
        await ctx.presentation._clear_overlay_after("overlay", 1, 0, first_revision)
        assert ctx.presentation.store.overlay.title == "second"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "required_family", ["dev.deckr.input.encoder", "dev.deckr.input.button"]
)
async def test_requirement_change_retires_old_grant(required_family):
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        old = device.context_for_control("0,0")
        metadata = _metadata("action.first")
        metadata = replace(
            metadata,
            descriptor=ActionDescriptor(
                actionId="action.first",
                requirements=[
                    {
                        "name": "changed",
                        "required": True,
                        "preferences": [
                            {"family": required_family, "direction": "input"}
                        ],
                    }
                ],
            ),
        )
        device.components.actions._cache.record_available(metadata)
        await device.on_action_availability_changed()
        await device.wait_idle()
        assert device.binding_by_id(old.binding_id) is None
        current = device.context_for_control("0,0")
        if required_family.endswith("encoder"):
            assert current is None
        else:
            assert current is not None and current.binding_id != old.binding_id
            assert (
                current.metadata.matched_capabilities[0].requirement_name == "changed"
            )


@pytest.mark.asyncio
async def test_overlapping_static_selectors_never_issue_conflicting_grants():
    async with session() as device:
        cfg = device.config.model_copy(
            update={
                "profiles": [
                    Profile(
                        name="default",
                        pages=[
                            Page(
                                controls=[
                                    Control(
                                        id="first",
                                        selector={"control_id": "0,0"},
                                        action="action.first",
                                    ),
                                    Control(
                                        id="second",
                                        selector={"control_id": "0,0"},
                                        action="action.second",
                                    ),
                                ]
                            )
                        ],
                    )
                ]
            }
        )
        await device.on_config_changed(cfg)
        await device.wait_idle()
        assert not tuple(device.iter_binding_leases())
        assert not device.state.attachments.command_authority_by_binding


@pytest.mark.asyncio
async def test_invalid_config_replacement_preserves_current_grants():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        old_config, old = device.config, device.context_for_control("0,0")
        invalid = device.config.model_copy(update={"profiles": []})
        with pytest.raises(ValidationError):
            type(invalid).model_validate(invalid.model_dump(by_alias=True))
        await device.on_config_changed(invalid)
        assert device.config is old_config
        assert device.context_for_control("0,0") is old
        for kwargs in ({"page": -1}, {"page": 9}, {"profile": "missing"}):
            assert not await device.set_page(**kwargs)
            assert device.context_for_control("0,0") is old


@pytest.mark.asyncio
async def test_descriptor_geometry_change_replaces_the_installed_render_target():
    from test_support.device_layouts import _make_control

    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        old = device.context_for_control("0,0")
        controls = tuple(
            _make_control("0,0", raster_width=96, raster_height=48)
            if c.control_id == "0,0"
            else c
            for c in device.device.controls
        )
        await device.on_device_descriptor_changed(
            device.device.model_copy(update={"controls": controls})
        )
        await device.wait_idle()
        new = device.context_for_control("0,0")
        assert new is not None and new.binding_id != old.binding_id
        assert new.control.image_format.width == 96
        assert new.control.image_format.height == 48
