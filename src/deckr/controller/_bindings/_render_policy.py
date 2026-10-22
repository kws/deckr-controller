"""Binding rendering admission, bounded workers, dispatch, and correlated results."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
import anyio.to_process
from deckr.action_runtime import (
    validate_rendering_support,
)
from deckr.actions.rendering import (
    RENDER_IMAGE_FORMAT,
    RENDER_TEXT_FORMAT,
    BindingRenderBody,
    BindingRenderResultBody,
    RenderingSupport,
    RenderLimits,
    RenderTargetSupport,
)

if TYPE_CHECKING:
    from deckr.contracts.authority import ContractPointer
    from deckr.contracts.messages import DeckrMessage
    from deckr.hardware.descriptors import DeviceRef
    from deckr.lanes import EndpointSession

    from deckr.controller._actions import ProviderSessionKey
    from deckr.controller._bindings._attachments import BindingLease
    from deckr.controller._bindings._ports import BindingActionService
    from deckr.controller._hardware import HardwareCommandService
    from deckr.controller._render_dispatcher import (
        RenderDispatcher,
        RenderTicket,
    )
    from deckr.controller._render_workers import RenderBackend
    from deckr.controller._state_gate import StateGate
from deckr.controller._render_graph import (
    INVARIANT_FORMAT,
)

_WORKERS = threading.BoundedSemaphore(8)
_GRAPH_WORKERS = anyio.CapacityLimiter(4)
logger = logging.getLogger(__name__)
DEFAULT_LIMITS = RenderLimits(
    max_message_bytes=262144,
    max_image_bytes=131072,
    max_image_pixels=1048576,
    max_text_code_points=1024,
    max_render_time_ms=2000,
)


def rendering_support(context):
    fmt = context.control.image_format
    if (
        fmt is None
        or fmt.width < 1
        or fmt.height < 1
        or fmt.width * fmt.height > DEFAULT_LIMITS.max_image_pixels
    ):
        return RenderingSupport(version=1, targets=())
    targets = []
    for match in context.metadata.matched_capabilities:
        if match.capability.capability_id != context.control.raster_capability_id:
            continue
        width, height = (
            (fmt.height, fmt.width) if fmt.rotation % 180 else (fmt.width, fmt.height)
        )
        candidate = dict(
            requirementName=match.requirement_name,
            width=width,
            height=height,
            formats=[
                {"id": RENDER_TEXT_FORMAT},
                {"id": INVARIANT_FORMAT},
                {"id": RENDER_IMAGE_FORMAT, "options": {"encodings": ["png", "jpeg"]}},
            ],
            limits=DEFAULT_LIMITS,
        )
        try:
            target = RenderTargetSupport.model_validate(candidate)
            support = RenderingSupport(version=1, targets=(target,))
            validate_rendering_support(context.metadata, support)
        except ValueError:
            continue
        targets.append(target)
    return RenderingSupport(version=1, targets=tuple(targets))


@dataclass
class _Job:
    message: DeckrMessage
    body: BindingRenderBody
    lease: BindingLease
    target: RenderTargetSupport
    ticket: RenderTicket
    deadline: float
    direct: bool = False
    done: bool = False
    command_id: str | None = None
    rejection: DeckrMessage | None = None
    result_emitted: bool = False
    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    timer: anyio.CancelScope = field(default_factory=anyio.CancelScope)


@dataclass(frozen=True)
class RenderingPorts:
    """Dependencies of rendering, with no mutable session or binding-host access."""

    endpoint: EndpointSession
    hardware: HardwareCommandService
    actions: BindingActionService
    dispatcher: RenderDispatcher
    backend: RenderBackend
    boundary: StateGate
    start_soon: Callable
    notify: Callable[[DeckrMessage, BindingRenderResultBody], None]
    config_id: str
    hardware_ref: DeviceRef
    is_active: Callable[[], bool]
    binding_by_id: Callable[[str], BindingLease | None]
    binding_output_authorized: Callable[[BindingLease], bool]
    current_contract: Callable[[ProviderSessionKey | None], ContractPointer | None]

    @property
    def config_active(self):
        return self.is_active()
