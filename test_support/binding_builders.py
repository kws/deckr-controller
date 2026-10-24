from collections.abc import AsyncGenerator

import anyio
import pytest_asyncio
from deckr.contracts.authority import ContractPointer
from invariant import Node, SubGraphNode, dump_graph_data_uri
from invariant.params import ref

from deckr.controller._actions import (
    ActionMetadata,
)
from deckr.controller._bindings._models import BindingLeaseSnapshot
from deckr.controller._device_session import DeviceSession
from deckr.controller._render import RenderResult
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from test_support.binding_constants import (
    PROVIDER_ID,
    PROVIDER_INSTANCE_ID,
    PROVIDER_SESSION_ID,
)


def _contract_pointer_for_provider_session(
    provider_session_id: str = PROVIDER_SESSION_ID,
    *,
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    provider_id: str = PROVIDER_ID,
) -> ContractPointer:
    return ContractPointer(
        contractId=f"provider-session:{provider_instance_id}:{provider_id}:{provider_session_id}",
        generation=1,
    )


def _binding_lease_for_control(
    manager: DeviceSession,
    control_id: str,
) -> BindingLeaseSnapshot | None:
    snapshot = manager.snapshot()
    context = snapshot.active_contexts.get(control_id)
    if context is not None:
        return snapshot.binding_leases.get(context.binding_id)
    output_owner = snapshot.output_owners.get(control_id)
    if output_owner is not None:
        return snapshot.binding_leases.get(output_owner)
    return next(
        (
            lease
            for lease in snapshot.binding_leases.values()
            if lease.control_id == control_id
        ),
        None,
    )


def _solid_key_graph() -> SubGraphNode:
    """Minimal graph: solid dark gray background (canvas size from context)."""
    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (51, 51, 51, 255),  # #333333
            },
            deps=["canvas"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="bg"
    )


def _solid_key_image() -> str:
    graph = _solid_key_graph()
    return dump_graph_data_uri(graph.graph, output=graph.output)


class ControlledFrameBackend:
    """Backend used by tests to control completion order without blocking commands."""

    def __init__(self):
        self.calls: list[int] = []
        self.requests: list = []
        self._events: dict[int, anyio.Event] = {}

    async def render(self, request) -> RenderResult:
        # These tests gate action/status results; preparing frames are transient
        # progress output, so let them finish without occupying a worker slot.
        if (
            request.source is not None
            and request.source.command_type == "binding_preparing"
        ):
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                control_id=request.control_id,
                generation=request.generation,
                frame=None,
            )
        self.calls.append(request.generation)
        self.requests.append(request)
        event = self._events.setdefault(request.generation, anyio.Event())
        await event.wait()
        return RenderResult(
            context_id=request.context_id,
            binding_id=request.binding_id,
            control_id=request.control_id,
            generation=request.generation,
            frame=f"frame-{request.generation}".encode(),
        )

    def release(self, generation: int) -> None:
        self._events.setdefault(generation, anyio.Event()).set()

    async def aclose(self) -> None:
        return


async def _wait_for_render_request(
    render_backend: ControlledFrameBackend,
    *,
    after: int = 0,
):
    with anyio.fail_after(5.0):
        while len(render_backend.requests) <= after:
            await anyio.sleep(0.01)
    return render_backend.requests[-1]


class SetRasterImageOnAppearAction:
    """Minimal action that sets a graph-backed raster image on will_appear."""

    action_id: str = "test.virtual.setops"

    async def on_will_appear(self, event, context):
        await context.set_raster_image(_solid_key_image())

    async def on_will_disappear(self, event, context):
        pass


class NoopAction:
    action_id: str = "test.virtual.noop"

    async def on_will_appear(self, event, context):
        pass

    async def on_will_disappear(self, event, context):
        pass


class CapturingBuiltinAction:
    def __init__(self, action_id: str):
        self.action_id = action_id
        self.contexts = []

    async def on_bind(self, context) -> None:
        self.contexts.append(context)

    async def on_unbind(self, context, reason: str) -> None:
        del context, reason

    async def on_input(self, context, event) -> None:
        del context, event


class MemoryConfigService:
    def __init__(self, config: DeviceConfig) -> None:
        self.config = config

    async def match_device(
        self,
        *,
        fingerprint: str,
        labels,
    ) -> DeviceConfig | None:
        del labels
        return self.config if self.config.match.fingerprint == fingerprint else None

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        return self.config if self.config.id == config_id else None

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        self.config = config
        return config

    def subscribe(self, config_id: str) -> AsyncGenerator[DeviceConfig | None, None]:
        del config_id
        return self._subscribe()

    async def _subscribe(self) -> AsyncGenerator[DeviceConfig | None, None]:
        yield self.config


def _settings_reload_config() -> DeviceConfig:
    return DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                id="root-control",
                                selector={"control_id": "0,0"},
                                action=SetRasterImageOnAppearAction.action_id,
                                settings={"label": "old"},
                            )
                        ]
                    )
                ],
            )
        ],
    )


def _settings_reload_selector_only_config() -> DeviceConfig:
    return DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={
                                    "geometry": {
                                        "x": 0,
                                        "y": 0,
                                        "width": 1,
                                        "height": 1,
                                        "unit": "grid",
                                    }
                                },
                                action=SetRasterImageOnAppearAction.action_id,
                                settings={"label": "old"},
                            )
                        ]
                    )
                ],
            )
        ],
    )


@pytest_asyncio.fixture
def device_config_set_raster_image():
    """Config: one profile, one page, one control with SetRasterImageOnAppearAction."""
    return DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=SetRasterImageOnAppearAction.action_id,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )


class ConfigurableBuiltinActionRegistry:
    """Registry that can add/remove actions for testing availability replans.

    Uses qualified IDs (provider_instance_id::action_id) internally to match BuiltinActionRegistry.
    """

    def __init__(self):
        self._actions: dict[str, ActionMetadata] = {}

    def _qualified_id(self, provider_instance_id: str, action_id: str) -> str:
        return f"{provider_instance_id}::{action_id}"

    async def get_action(
        self,
        address: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: dict[str, str] | None = None,
    ) -> ActionMetadata | None:
        del provider_labels
        if "::" in address:
            return self._actions.get(address)
        for key, meta in self._actions.items():
            if (
                provider_instance_id is not None
                and meta.provider_instance_id != provider_instance_id
            ):
                continue
            if key.endswith(f"::{address}"):
                return meta
        return None

    def add_action(self, action_id: str, meta: ActionMetadata) -> None:
        qualified = self._qualified_id(meta.provider_instance_id, action_id)
        self._actions[qualified] = meta

    def remove_action(self, action_id: str, provider_instance_id: str) -> None:
        qualified = self._qualified_id(provider_instance_id, action_id)
        self._actions.pop(qualified, None)

    def get_builtin_action(self, action_id: str):
        return None
