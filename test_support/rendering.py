import base64
import io
from dataclasses import replace

import anyio
from PIL import Image

from deckr.controller._render_workers import ThreadRenderBackend
from test_support.action_runtime import notifications
from test_support.managed_hardware import eventually


def png(size=(10, 5), color=(255, 0, 0, 255)):
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def parameters(h, *, text="Kitchen", operation="set", control_id="key-1"):
    binding = h.device.context_for_control(control_id).metadata
    result = {
        "target": {
            "kind": "binding",
            "bindingId": binding.binding_id,
            "generation": binding.output_generation,
        },
        "requirementName": "display",
        "operation": operation,
    }
    if operation == "set":
        result["content"] = {
            "format": "dev.deckr.render.text.v1",
            "data": {"text": text},
        }
    return result


def real_backend(h, backend=None):
    backend = backend or ThreadRenderBackend()
    h.device._render_backend = backend
    h.device.components.rendering.ports = replace(
        h.device.components.rendering.ports, backend=backend
    )
    h.device.components.dispatcher._backend = backend
    return backend


async def result_for(h, request):
    await eventually(
        lambda: any(
            e["requestMessageId"] == request.message_id
            for e in notifications(h, "binding_render_result")
        )
    )
    return next(
        e
        for e in notifications(h, "binding_render_result")
        if e["requestMessageId"] == request.message_id
    )


class GatedBackend(ThreadRenderBackend):
    def __init__(self):
        super().__init__()
        self.started = []
        self.gates = []

    async def render(self, request):
        gate = anyio.Event()
        self.started.append(request)
        self.gates.append(gate)
        await gate.wait()
        return await super().render(request)


def direct_parameters(h, *, image=None, operation="set_frame"):
    binding = h.device.context_for_control("key-1").metadata
    match = next(
        m for m in binding.matched_capabilities if m.requirement_name == "display"
    )
    return {
        "binding": binding.model_dump(by_alias=True, mode="json", exclude_none=True),
        "capability": match.capability.model_dump(
            by_alias=True, mode="json", exclude_none=True
        ),
        "generation": binding.output_generation,
        "commandType": operation,
        "params": {}
        if operation == "clear"
        else {"image": image or png((72, 72)), "encoding": "png"},
    }


async def drained(h):
    await eventually(lambda: h.device.components.rendering.active.get("key-1", 0) == 0)
