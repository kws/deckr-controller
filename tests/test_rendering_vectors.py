"""External reference cases; enabled explicitly by the conformance command."""

import io
import json
import os
from pathlib import Path

import pytest
from deckr.actions.rendering import RenderContent, RenderLimits
from PIL import Image
from test_action_runtime_integration import running_controller
from test_rendering_contract import (
    GatedBackend,
    drained,
    parameters,
    real_backend,
    result_for,
)

from deckr.controller._render_content import ContentFailure, render_content
from test_support.managed_hardware import eventually


def cases(name):
    root = os.environ.get("DECKR_SPEC_ROOT")
    if root is None:
        return []
    return json.loads(
        (
            Path(root) / f"contract/v1/vectors/action-runtime-rendering-{name}.v1.json"
        ).read_text()
    )["cases"]


@pytest.mark.parametrize("case", cases("content"), ids=lambda c: c["id"])
def test_reference_content(case):
    content = RenderContent.from_wire(case["instance"])
    limits = RenderLimits.from_wire(case["limits"])
    code = None
    try:
        result = render_content(content, case["width"], case["height"], limits)
    except ContentFailure as exc:
        code = exc.code
    assert code == case["expectedCode"]
    if code is None:
        with Image.open(io.BytesIO(result)) as image:
            assert image.size == (case["width"], case["height"])
            for pixel in case["expectedPixels"]:
                assert list(image.getpixel((pixel["x"], pixel["y"]))) == pixel["rgb"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", cases("ordering"), ids=lambda c: c["id"])
async def test_reference_ordering(managed_substrate, case):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        requests, results = {}, {}
        for step in case["steps"]:
            if "send" in step:
                body = parameters(
                    h, text=step.get("text", ""), operation=step.get("operation", "set")
                )
                if "content" in step:
                    body["content"] = step["content"]
                requests[step["send"]] = await h.channel.send("binding_render", body)
            elif "waitRender" in step:
                await eventually(
                    lambda count=step["waitRender"]: len(backend.started) == count
                )
            elif "completeRender" in step:
                backend.gates[step["completeRender"] - 1].set()
            elif "drain" in step:
                await drained(h)
            elif "expectResult" in step:
                result = await result_for(h, requests[step["expectResult"]])
                results[step["expectResult"]] = result
                assert result["status"] == step["status"], result
                if "code" in step:
                    assert result["code"] == step["code"]
            else:
                raise AssertionError(f"Unknown reference step: {step}")
        expected = [
            results[key]["commandMessageId"] for key in case["expectedCommandRequests"]
        ]
        await eventually(lambda: h.commands.await_count == len(expected))
        assert [
            call.args[0].message.message_id for call in h.commands.await_args_list
        ] == expected
