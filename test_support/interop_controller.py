import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.bindings.nats import DeckrNatsSubstrate
from deckr.components import RunContext
from deckr.contracts.lanes import CORE_LANE_CONTRACTS, MessageContractRegistry
from deckr.runtime import Deckr
from deckr.substrates.nats_server import NatsServerSupervisor

from deckr.controller._actions import ControllerActionService
from deckr.controller._controller_service import ControllerService
from deckr.controller._render import RenderResult
from deckr.controller.config import Control, ControlSelector, Page, Profile
from test_support.managed_hardware import (
    MemoryConfigService,
    _config,
    device_descriptor,
)
from test_support.rendering import (
    parameters,
)

INTEROP = os.environ.get("DECKR_INTEROP_ROOT")


if INTEROP:
    sys.path.insert(0, str(Path(INTEROP) / "scripts"))
    from action_runtime_support import Peer


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("language", ("python", "typescript", "rust")),
    pytest.mark.skipif(
        not INTEROP,
        reason="Run scripts/controller_action_runtime.py in deckr-integration-tests",
    ),
]


class ForeignProvider:
    def __init__(self, peer):
        self.peer = peer

    async def send(self, name, payload):
        receipt = await self.peer.command("send", name=name, payload=payload)
        return receipt["messageId"]

    async def events(self, name):
        state = await self.peer.command("status")
        assert not state["errors"], state["errors"]
        return [m for m in state["received"] if m["body"]["name"] == name]

    async def event(self, name, predicate=lambda _: True, count=1):
        with anyio.fail_after(15):
            while True:
                messages = [
                    m for m in await self.events(name) if predicate(m["body"]["event"])
                ]
                if len(messages) >= count:
                    return messages[-1]
                await anyio.sleep(0.02)

    async def result(self, request_id):
        message = await self.event(
            "binding_render_result", lambda e: e["requestMessageId"] == request_id
        )
        assert message["causationId"] == request_id
        assert message.get("inReplyTo") is None
        return message["body"]["event"]


@asynccontextmanager
async def foreign_controller(language, tmp_path):
    evidence = (
        Path(os.environ.get("DECKR_INTEROP_EVIDENCE", tmp_path))
        / tmp_path.name
        / language
    )
    evidence.mkdir(parents=True, exist_ok=True)
    supervisor = NatsServerSupervisor(
        port=-1, runtime_dir=evidence / "broker", auth_enabled=False
    )
    handle = await supervisor.start()
    requirements = [
        {"name": "press", "preferences": [{"family": "dev.deckr.input.button"}]},
        {"name": "press-alias", "preferences": [{"family": "dev.deckr.input.button"}]},
        {"name": "display", "preferences": [{"family": "dev.deckr.output.raster"}]},
    ]
    peer_config = {
        "url": handle.url,
        "instance": "test",
        "providerId": "test.provider",
        "role": "provider",
        "id": "test",
        "entries": [
            {
                "actionId": "action.test",
                "status": "available",
                "descriptor": {"actionId": "action.test", "requirements": requirements},
            }
        ],
    }
    foreign = ForeignProvider(Peer(language, peer_config, evidence / "provider"))
    try:
        await foreign.peer.start()
        contracts = MessageContractRegistry(CORE_LANE_CONTRACTS.values())
        substrate = DeckrNatsSubstrate(url=handle.url, lane_contracts=contracts)
        descriptor = device_descriptor()
        config = _config(
            config_id="config-a", fingerprint=descriptor.fingerprint
        ).model_copy(
            update={
                "profiles": [
                    Profile(
                        name="default",
                        pages=[
                            Page(
                                controls=[
                                    Control(
                                        selector=ControlSelector(control_id="key-1"),
                                        action="action.test",
                                        provider_instance_id="test",
                                    )
                                ]
                            )
                        ],
                    )
                ]
            }
        )
        registry = MagicMock()
        registry.get_action = AsyncMock(return_value=None)
        render = MagicMock()

        async def blank(request):
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                control_id=request.control_id,
                generation=request.generation,
                frame=None,
            )

        render.render = AsyncMock(side_effect=blank)
        render.aclose = AsyncMock()
        commands = AsyncMock(return_value=True)
        async with (
            Deckr(lane_contracts=contracts, message_bus=substrate) as runtime,
            runtime.endpoint("hardware_manager:manager-a") as hardware_endpoint,
            runtime.endpoint("controller:controller-main") as endpoint,
            runtime.hardware_manager(
                hardware_endpoint,
                manager_id="manager-a",
                devices={descriptor.device_id: descriptor},
                command_handler=commands,
            ) as hardware,
            runtime.services(endpoint) as services,
            anyio.create_task_group() as tg,
        ):
            actions = ControllerActionService(
                controller_id="controller-main",
                controller_session_id=endpoint.session_id,
                manager=registry,
                services=services,
                start_soon=tg.start_soon,
            )
            controller = ControllerService(
                endpoint=endpoint,
                hardware=runtime.hardware(endpoint),
                config_service=MemoryConfigService(config),
                settings_service=None,
                controller_id="controller-main",
                action_registry=registry,
                action_service=actions,
                render_backend=render,
            )
            stopping = anyio.Event()
            await controller.start(RunContext(tg=tg, stopping=stopping))
            try:
                await foreign.event("binding_attached")
                device = await controller._controller_contexts.get("config-a")
                assert (
                    device is not None
                    and device.context_for_control("key-1") is not None
                )
                yield SimpleNamespace(
                    controller=controller,
                    device=device,
                    foreign=foreign,
                    commands=commands,
                    hardware=hardware,
                    hardware_endpoint=hardware_endpoint,
                    endpoint=endpoint,
                    substrate=substrate,
                    actions=actions,
                    evidence=evidence,
                    peer_config=peer_config,
                    language=language,
                )
            finally:
                stopping.set()
                await controller.stop()
                tg.cancel_scope.cancel()
    finally:
        try:
            await foreign.peer.stop()
        finally:
            await supervisor.stop()
            (evidence / "nats.log").write_text("\n".join(supervisor.recent_logs()))


async def barrier(h):
    request = await h.foreign.send(
        "binding_render", {**parameters(h), "requirementName": "missing"}
    )
    assert (await h.foreign.result(request))["status"] == "rejected"
