"""Tests for binding validator."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from conftest import LaneHarness
from deckr.actions.messages import PageChildBindingDescriptor, PageChildBindingTarget
from deckr.contracts.messages import controller_address
from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
    DeviceRef,
)

from deckr.controller._binding_resolution import ConfiguredControlBinding
from deckr.controller._binding_validator import (
    ValidationError,
    ValidationResult,
    format_validation_summary,
    validate_dynamic_page_bindings,
    validate_page_bindings,
)
from deckr.controller._render import RenderResult
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.action_provider.provider import ActionMetadata
from deckr.controller.config import CapabilitySelector, ControlSelector

CONTROLLER_ID = "controller-main"


class FakeHardwareCommandService:
    def __init__(self):
        self.set_raster_frame = AsyncMock()
        self.clear_raster = AsyncMock()
        self.sleep_device = AsyncMock()
        self.wake_device = AsyncMock()


def _make_device(
    device_id: str = "test-dev",
    controls: list[ControlDescriptor] | None = None,
) -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test",
        fingerprint=f"fingerprint:{device_id}",
        controls=controls or [_make_control("0,0")],
    )


def _hardware_ref(device: DeviceDescriptor) -> DeviceRef:
    return DeviceRef(managerId="manager-main", deviceId=device.device_id)


class _ImmediateRenderBackend:
    async def render(self, request):
        return RenderResult(
            context_id=request.context_id,
            binding_id=request.binding_id,
            control_id=request.control_id,
            generation=request.generation,
            frame=b"frame",
        )

    async def aclose(self) -> None:
        return


def _make_control(
    control_id: str,
    row: int = 0,
    col: int = 0,
    kind: str = "key",
    has_display: bool = True,
) -> ControlDescriptor:
    output_capabilities = ()
    if has_display:
        output_capabilities = (
            CapabilityDescriptor.model_validate(
                {
                    "capabilityId": "raster.bitmap",
                    "family": DECKR_OUTPUT_RASTER,
                    "type": "bitmap",
                    "direction": "output",
                    "access": ["settable"],
                    "commandTypes": ["set_frame", "clear"],
                    "constraints": [
                        {"type": "fixed", "subject": "width", "value": 72},
                        {"type": "fixed", "subject": "height", "value": 72},
                    ],
                }
            ),
        )
    return ControlDescriptor(
        controlId=control_id,
        kind=kind,
        geometry=ControlGeometry(x=col, y=row, width=1, height=1, unit="grid"),
        inputCapabilities=(
            CapabilityDescriptor(
                capabilityId="button.momentary",
                family=DECKR_INPUT_BUTTON,
                type="momentary",
                direction="input",
                access=("emits",),
                eventTypes=("down", "up"),
            ),
        ),
        outputCapabilities=output_capabilities,
    )


# --- validate_page_bindings ---


def _make_key_action():
    return ActionMetadata(
        uuid="action.a",
        provider_instance_id="python",
        provider_id="test",
    )


def _page_child(control_id: str, **kwargs) -> PageChildBindingDescriptor:
    return PageChildBindingDescriptor(
        controlId=control_id,
        target=PageChildBindingTarget(kind="self"),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_validate_page_bindings_all_valid():
    device = _make_device(controls=[_make_control("0,0"), _make_control("0,1")])
    action = _make_key_action()

    async def get_action(uuid: str, **kwargs):
        del kwargs
        return action

    bindings = [
        _page_child("0,0", settings={}),
        _page_child("0,1", settings={}),
    ]
    result = await validate_dynamic_page_bindings(
        bindings,
        device,
        get_action,
        owner_action_uuid="action.a",
        owner_provider_instance_id="python",
    )
    assert result.valid is True
    assert len(result.errors) == 0
    assert result.actions == []


@pytest.mark.asyncio
async def test_validate_dynamic_page_bindings_resolves_explicit_child_action_target():
    device = _make_device(controls=[_make_control("3,0")])
    action = ActionMetadata(
        uuid="action.volume",
        provider_instance_id="sonos-bedroom",
        provider_id="dev.deckr.sonos",
    )

    get_action = AsyncMock(return_value=action)

    bindings = [
        PageChildBindingDescriptor(
            controlId="3,0",
            target=PageChildBindingTarget(
                kind="action",
                actionId="action.volume",
                providerInstanceId="sonos-bedroom",
                instanceKey="bedroom-volume",
            ),
            settings={"zoneName": "Bedroom"},
        )
    ]

    result = await validate_dynamic_page_bindings(
        bindings,
        device,
        get_action,
        owner_action_uuid="action.pager",
        owner_provider_instance_id="python-com.k-si.deckr.kaj",
    )

    assert result.valid is True
    assert result.bindings[0].action_uuid == "action.volume"
    assert result.bindings[0].provider_instance_id == "sonos-bedroom"
    assert result.bindings[0].settings["zoneName"] == "Bedroom"
    assert result.actions == []
    get_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_page_bindings_missing_control():
    device = _make_device(controls=[_make_control("0,0")])
    action = _make_key_action()

    async def get_action(uuid: str, **kwargs):
        del kwargs
        return action

    bindings = [_page_child("99,99", settings={})]
    result = await validate_dynamic_page_bindings(
        bindings,
        device,
        get_action,
        owner_action_uuid="action.a",
        owner_provider_instance_id="python",
    )
    assert result.valid is False
    assert result.actions == []
    assert len(result.errors) == 1
    assert result.errors[0].code == "control_not_found"
    assert "99,99" in result.errors[0].message


@pytest.mark.asyncio
async def test_validate_page_bindings_missing_action():
    """Missing action is non-blocking; page loads with control showing 'unavailable'."""
    device = _make_device(controls=[_make_control("0,0")])

    async def get_action(uuid: str, **kwargs):
        del kwargs
        return None

    bindings = [_page_child("0,0", settings={})]
    result = await validate_dynamic_page_bindings(
        bindings,
        device,
        get_action,
        owner_action_uuid="nonexistent",
        owner_provider_instance_id="python",
    )
    assert result.valid is True  # Page can load (partial activation)
    assert result.has_blocking_errors is False
    assert result.has_non_blocking_errors is False
    assert result.actions == []
    assert result.errors == []


@pytest.mark.asyncio
async def test_validate_page_bindings_rejects_ambiguous_selector():
    device = _make_device(controls=[_make_control("0,0"), _make_control("1,0")])
    action = _make_key_action()

    async def get_action(uuid: str, **kwargs):
        del kwargs
        return action

    result = await validate_page_bindings(
        [
            ConfiguredControlBinding(
                selector=ControlSelector(
                    kind="key",
                    output=(
                        CapabilitySelector(
                            family=DECKR_OUTPUT_RASTER,
                            type="bitmap",
                        ),
                    ),
                ),
                action_uuid="action.a",
                provider_instance_id=None,
                provider_labels={},
                settings={},
            )
        ],
        device,
        get_action,
    )

    assert result.valid is False
    assert result.errors[0].code == "control_selector_ambiguous"


@pytest.mark.asyncio
async def test_activation_requirement_does_not_match_momentary_only_control():
    device = _make_device(controls=[_make_control("0,0")])
    action = _make_key_action()

    async def get_action(uuid: str, **kwargs):
        del kwargs
        return action

    result = await validate_page_bindings(
        [
            ConfiguredControlBinding(
                selector=ControlSelector(
                    control_id="0,0",
                    input=(
                        CapabilitySelector(
                            family=DECKR_INPUT_BUTTON,
                            type="activation",
                        ),
                    ),
                ),
                action_uuid="action.a",
                provider_instance_id=None,
                provider_labels={},
                settings={},
            )
        ],
        device,
        get_action,
    )

    assert result.valid is False
    assert result.errors[0].code == "capability_not_found"


# --- format_validation_summary ---


def test_format_validation_summary_passed():
    result = ValidationResult(valid=True)
    assert "passed" in format_validation_summary(result)


def test_format_validation_summary_errors():
    result = ValidationResult(valid=False)
    result.add_error("control_not_found", "control 'x' not found", "x", "action.a")
    result.add_error(
        "capability_mismatch", "mismatch", "y", "action.b", details=["need image"]
    )
    s = format_validation_summary(result)
    assert "2 error(s)" in s
    assert "control_not_found" in s or "x" in s
    assert "capability_mismatch" in s or "y" in s


def test_format_validation_summary_list_of_errors():
    errors = [
        ValidationError("control_not_found", "msg", "0,0", "a", details=[]),
    ]
    s = format_validation_summary(errors)
    assert "1 error(s)" in s


# --- Integration: DeviceManager rejects invalid static page ---


@pytest.mark.asyncio
async def test_device_manager_rejects_invalid_static_page_and_reverts_stack():
    """When static page has invalid bindings (e.g. missing control), DeviceManager rejects transition and reverts stack."""
    from deckr.controller._device_manager import DeviceManager
    from deckr.controller.config._data import Control, DeviceConfig, Page, Profile

    device = _make_device(controls=[_make_control("0,0")])  # only control 0,0 exists
    command_service = FakeHardwareCommandService()

    config = DeviceConfig(
        id="test-dev",
        name="Test",
        match={"fingerprint": "fingerprint:test-dev"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "99,99"},
                                action="dev.deckr.controller.builtin.action.go_to_page",
                                settings={},
                            ),
                        ]
                    ),
                ],
            ),
        ],
    )

    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid="dev.deckr.controller.builtin.action.go_to_page",
            provider_instance_id="builtin",
            provider_id="dev.deckr.controller.builtin",
        )
    )
    registry.provider_session_id.return_value = "provider-session"
    registry.provider_instance_provides_provider.return_value = True

    def start_soon(*args, **kwargs):
        pass

    actions_bus = LaneHarness(
        "actions",
        default_endpoint=controller_address(CONTROLLER_ID),
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
        config=config,
        manager=registry,
        actions_bus=actions_bus.endpoint(controller_address(CONTROLLER_ID)).session,
        start_soon=start_soon,
    )
    await manager.set_page(profile="default", page=0)

    # Validation rejected the page: no contexts were created (invalid control 99,99 not on device).
    # Current page remains unchanged when validation fails on the first page.
    contexts = await manager.action_contexts.values()
    assert len(contexts) == 0


@pytest.mark.asyncio
async def test_device_manager_loads_page_with_missing_action_shows_unavailable():
    """When static page has missing action, page loads; control shows 'unavailable' overlay."""
    from deckr.controller._device_manager import DeviceManager
    from deckr.controller.config._data import Control, DeviceConfig, Page, Profile

    device = _make_device(controls=[_make_control("0,0"), _make_control("0,1")])
    command_service = FakeHardwareCommandService()

    config = DeviceConfig(
        id="test-dev",
        name="Test",
        match={"fingerprint": "fingerprint:test-dev"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action="dev.deckr.controller.builtin.action.go_to_page",
                                settings={},
                            ),
                            Control(
                                selector={"control_id": "0,1"},
                                action="com.example.nonexistent",
                                settings={},
                            ),
                        ]
                    ),
                ],
            ),
        ],
    )

    registry = MagicMock()
    action = _make_key_action()
    action.uuid = "dev.deckr.controller.builtin.action.go_to_page"

    async def get_action(uuid, **kwargs):
        del kwargs
        if uuid == "dev.deckr.controller.builtin.action.go_to_page":
            return ActionMetadata(
                uuid=action.uuid,
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
                provider_id="dev.deckr.controller.builtin",
            )
        return None

    registry.get_action = get_action
    builtin_action = MagicMock()
    builtin_action.on_bind = AsyncMock()
    builtin_action.on_unbind = AsyncMock()
    builtin_action.on_input = AsyncMock()
    registry.get_builtin_action.return_value = builtin_action
    registry.provider_session_id.return_value = "provider-session"
    registry.provider_instance_provides_provider.return_value = True

    actions_bus = LaneHarness(
        "actions",
        default_endpoint=controller_address(CONTROLLER_ID),
    )
    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=config,
            manager=registry,
            actions_bus=actions_bus.endpoint(controller_address(CONTROLLER_ID)).session,
            start_soon=tg.start_soon,
            render_backend=_ImmediateRenderBackend(),
        )
        await manager.set_page(profile="default", page=0)

        contexts = await manager.action_contexts.values()
        assert len(contexts) == 1

        with anyio.fail_after(1.0):
            while not any(
                c[0][1] == "0,1"
                for c in command_service.set_raster_frame.call_args_list
            ):
                await anyio.sleep(0.01)

        tg.cancel_scope.cancel()
