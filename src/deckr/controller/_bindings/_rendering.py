"""Binding rendering admission, bounded workers, dispatch, and correlated results."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

import anyio
import anyio.to_process
from deckr.action_runtime import (
    RenderBindingState,
    RenderCommandContext,
    RenderRejection,
    action_runtime_service_id,
    validate_render_command,
)
from deckr.actions.pages import BindingCommandTarget
from deckr.actions.rendering import (
    RENDER_IMAGE_FORMAT,
    RENDER_TEXT_FORMAT,
    BindingRenderBody,
    BindingRenderResultBody,
    RenderTargetSupport,
)
from deckr.contracts.messages import service_address
from deckr.contracts.models import thaw_json
from deckr.services import ServiceMessageBody

from deckr.controller._render_content import (
    ContentFailure,
)

if TYPE_CHECKING:
    pass
from deckr.controller._bindings._render_execution import RenderExecution
from deckr.controller._bindings._render_policy import (
    _WORKERS,
    DEFAULT_LIMITS,
    RenderingPorts,
    _Job,
)
from deckr.controller._render_graph import (
    INVARIANT_FORMAT,
    validate_graph,
)

logger = logging.getLogger(__name__)


class BindingRenderer:
    def __init__(self, ports: RenderingPorts):
        self.ports = ports
        self.execution = RenderExecution(
            ports=lambda: self.ports,
            authorize=self._authorize,
            finish=self._finish,
            hardware_rejection=self._hardware_rejection,
        )
        self.jobs = OrderedDict()
        self.active = {}

    def _retained(self, key):
        job = self.jobs.get(key)
        if job is not None and job.done and job.deadline + 60 < time.monotonic():
            self.jobs.pop(key)
            return False
        return job is not None

    def _context(self, message, lease):
        ports = self.ports
        state = RenderBindingState(
            binding=lease.context.metadata,
            rendering=lease.context.rendering,
            active=ports.config_active and ports.binding_output_authorized(lease),
            contract=lease.context.contract,
            runtimeEndpoint=str(
                service_address(action_runtime_service_id(lease.provider_instance_id))
            ),
            runtimeSessionId=lease.provider_session_id,
        )
        return RenderCommandContext(
            contract=message.contract,
            controllerEndpoint=str(ports.endpoint.address),
            controllerSessionId=ports.endpoint.session_id,
            runtimeEndpoint=str(
                service_address(action_runtime_service_id(lease.provider_instance_id))
            ),
            runtimeSessionId=lease.provider_session_id,
            localAuthority=ports.current_contract(lease.provider_session_key)
            == message.contract,
            hardwareAuthority=ports.hardware.has_authority(
                ports.config_id, ports.hardware_ref
            ),
            bindings={lease.binding_id: state},
            messageBytes=message.wire_size_bytes,
        )

    async def _result(
        self, message, body, status, code=None, command_id=None, stage="controller"
    ):
        result = BindingRenderResultBody(
            target=body.target,
            requirement_name=body.requirement_name,
            request_message_id=message.message_id,
            stage=stage,
            status=status,
            **({"code": code} if code else {}),
            **({"command_message_id": command_id} if command_id else {}),
        )
        self.ports.notify(message, result)

    async def handle(self, message):
        try:
            service_body = ServiceMessageBody.from_wire(message.body)
            if service_body.name != "binding_render":
                return False
            params = thaw_json(service_body.params)
            target = BindingCommandTarget.from_wire(params["target"])
        except (ValueError, TypeError, KeyError):
            return False
        lease = self.ports.binding_by_id(target.binding_id)
        if lease is None:
            return False
        key = await self.ports.actions.render_message_key(message)
        if key is None:
            return True
        if (
            key != lease.provider_session_key
            or lease.context.contract != message.contract
        ):
            requirement = params.get("requirementName")
            if isinstance(requirement, str) and requirement:
                await self._result(
                    message,
                    BindingRenderBody(
                        target=target, requirementName=requirement, operation="clear"
                    ),
                    "rejected",
                    "target_not_current",
                )
            return True
        try:
            body = BindingRenderBody.from_wire(params)
        except (ValueError, TypeError):
            if (
                isinstance(params.get("requirementName"), str)
                and params["requirementName"]
            ):
                identifiable = BindingRenderBody(
                    target=target,
                    requirementName=params["requirementName"],
                    operation="clear",
                )
                await self._result(message, identifiable, "rejected", "invalid_content")
            return True
        cache_key = (
            str(message.sender),
            message.sender_session_id,
            message.contract.contract_id,
            message.contract.generation,
            message.message_id,
        )
        if self._retained(cache_key):
            return True
        async with self.ports.boundary:
            try:
                if lease.context.contract is None or lease.provider_session_id is None:
                    raise RenderRejection("target_not_current")
                if key != lease.provider_session_key:
                    raise RenderRejection("target_not_current")
                _, support = validate_render_command(
                    message, self._context(message, lease)
                )
            except RenderRejection as exc:
                if exc.code != "unauthorized":
                    await self._result(message, body, "rejected", exc.code)
                return True
            return await self._admit(message, body, lease, support, cache_key)

    async def _admit(self, message, body, lease, support, cache_key, *, direct=False):
        # The outer check can race concurrent deliveries waiting on the nav lock.
        if self._retained(cache_key):
            return True
        if body.content is not None and body.content.format == INVARIANT_FORMAT:
            try:
                validate_graph(thaw_json(body.content.data), support.limits)
            except ContentFailure as exc:
                await self._result(message, body, "rejected", exc.code)
                return True
        control_id = lease.control_id
        if self.active.get(control_id, 0) >= 2 or not _WORKERS.acquire(blocking=False):
            if not direct:
                await self._result(message, body, "rejected", "busy")
            return True
        self.active[control_id] = self.active.get(control_id, 0) + 1
        try:
            router = lease.context.presentation
            ticket = await self.ports.dispatcher.reserve_base(
                control_id=control_id,
                context_id=lease.context_id,
                binding_id=lease.binding_id,
                output=router.output,
            )
            job = _Job(
                message,
                body,
                lease,
                support,
                ticket,
                time.monotonic() + support.limits.max_render_time_ms / 1000,
                direct=direct,
            )
            for old in tuple(self.jobs.values()):
                if not old.done and old.lease.control_id == control_id:
                    await self._finish(old, "superseded", "newer_output")
            self.jobs[cache_key] = job
            for key, old in tuple(self.jobs.items()):
                if old.done and (
                    len(self.jobs) > 512 or old.deadline + 60 < time.monotonic()
                ):
                    self.jobs.pop(key, None)
            self.ports.start_soon(self._run, job)
            self.ports.start_soon(self._expire, job)
        except BaseException:
            self.active[control_id] -= 1
            _WORKERS.release()
            raise
        return True

    async def direct_output(self, lease, body, message):
        """Already-authorized byte-only binding_output shares base ordering."""
        support = next(
            (
                t
                for t in lease.context.rendering.targets
                if any(
                    m.requirement_name == t.requirement_name
                    and m.capability == body.capability
                    for m in lease.context.metadata.matched_capabilities
                )
            ),
            None,
        )
        if support is None:
            # Direct frames remain usable without an advertised rendering facility.
            match = next(
                (
                    m
                    for m in lease.context.metadata.matched_capabilities
                    if m.capability == body.capability
                    and m.family == "dev.deckr.output.raster"
                    and m.capability_type == "bitmap"
                    and m.direction == "output"
                    and body.command_type in m.command_types
                ),
                None,
            )
            fmt = lease.control.image_format
            if match is None or fmt is None:
                return
            support = RenderTargetSupport(
                requirementName=match.requirement_name,
                width=fmt.height if fmt.rotation % 180 else fmt.width,
                height=fmt.width if fmt.rotation % 180 else fmt.height,
                formats=[
                    {"id": RENDER_TEXT_FORMAT},
                    {"id": INVARIANT_FORMAT},
                    {
                        "id": RENDER_IMAGE_FORMAT,
                        "options": {"encodings": ["png", "jpeg"]},
                    },
                ],
                limits=DEFAULT_LIMITS,
            )
        params = {
            "target": {
                "kind": "binding",
                "bindingId": lease.binding_id,
                "generation": body.generation,
            },
            "requirementName": support.requirement_name,
            "operation": "clear" if body.command_type == "clear" else "set",
        }
        if body.command_type == "set_frame":
            params["content"] = {
                "format": RENDER_IMAGE_FORMAT,
                "data": thaw_json(body.params),
            }
        try:
            render_body = BindingRenderBody.from_wire(params)
        except (ValueError, TypeError):
            return
        key = (
            str(message.sender),
            message.sender_session_id,
            message.contract.contract_id,
            message.contract.generation,
            message.message_id,
        )
        if self._retained(key):
            return
        async with self.ports.boundary:
            try:
                self._validate_direct(message, render_body, lease, support)
            except (ValueError, TypeError):
                return
            await self._admit(message, render_body, lease, support, key, direct=True)

    def _validate_direct(self, message, body, lease, support):
        facts = self._context(message, lease)
        state = facts.bindings[lease.binding_id]
        if (
            not facts.local_authority
            or not facts.hardware_authority
            or not state.active
            or state.contract != message.contract
            or body.target.generation != state.binding.output_generation
        ):
            raise RenderRejection("target_not_current")
        if message.wire_size_bytes > support.limits.max_message_bytes:
            raise RenderRejection("limit_exceeded")

    async def _finish(self, job, status, code=None):
        if job.done:
            return
        job.done = True
        job.timer.cancel()
        if not job.direct:
            await self._result(
                job.message,
                job.body,
                status,
                code,
                job.command_id if status == "dispatched" else None,
            )
        job.result_emitted = True
        if job.rejection is not None:
            await self._hardware_rejection(job, job.rejection)
        job.scope.cancel()

    async def retire_binding(self, binding_id):
        for job in tuple(self.jobs.values()):
            if job.lease.binding_id == binding_id:
                await self._finish(job, "superseded", "target_not_current")

    async def _expire(self, job):
        with job.timer:
            await anyio.sleep(max(0, job.deadline - time.monotonic()))
            await self._finish(job, "failed", "deadline_exceeded")

    async def _hardware_rejection(self, job, message):
        if not job.result_emitted:
            job.rejection = message
            return
        job.rejection = None
        if not job.direct:
            await self._result(
                job.message,
                job.body,
                "rejected",
                "hardware_rejected",
                job.command_id,
                stage="hardware",
            )

    async def _authorize(self, job):
        if job.done or time.monotonic() >= job.deadline:
            raise ContentFailure("deadline_exceeded")
        if not await self.ports.actions.authorize_runtime_message(
            job.lease.provider_session_key, job.message
        ):
            raise RenderRejection("target_not_current")
        if job.direct:
            self._validate_direct(job.message, job.body, job.lease, job.target)
        else:
            validate_render_command(job.message, self._context(job.message, job.lease))
        if job.done or time.monotonic() >= job.deadline:
            raise ContentFailure("deadline_exceeded")

    async def _run(self, job):
        try:
            with job.scope:
                if not job.done:
                    await self.execution.execute(job)
        finally:
            self.active[job.lease.control_id] -= 1
            _WORKERS.release()
