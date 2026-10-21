"""Binding rendering admission, bounded workers, dispatch, and correlated results."""

from __future__ import annotations

import base64
import io
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

import anyio
import anyio.to_process
from deckr.action_runtime import (
    RenderBindingState,
    RenderCommandContext,
    RenderRejection,
    validate_render_command,
    validate_rendering_support,
)
from deckr.actions.pages import BindingCommandTarget
from deckr.actions.rendering import (
    RENDER_IMAGE_FORMAT,
    RENDER_TEXT_FORMAT,
    BindingRenderBody,
    BindingRenderResultBody,
    RenderImage,
    RenderingSupport,
    RenderLimits,
    RenderTargetSupport,
)
from deckr.contracts.messages import entity_subject
from deckr.contracts.models import thaw_json
from deckr.services import ServiceMessageBody

from deckr.controller._render import RenderModel, RenderSource
from deckr.controller._render_content import (
    ContentFailure,
    decode_image,
    render_content,
)
from deckr.controller._render_graph import (
    INVARIANT_FORMAT,
    render_graph,
    validate_graph,
)

_WORKERS = threading.BoundedSemaphore(8)
_GRAPH_WORKERS = anyio.CapacityLimiter(4)
logger = logging.getLogger(__name__)
DEFAULT_LIMITS = RenderLimits(
    maxMessageBytes=262144,
    maxImageBytes=131072,
    maxImagePixels=1048576,
    maxTextCodePoints=1024,
    maxRenderTimeMs=2000,
)


def rendering_support(context):
    fmt = context.control.image_format
    if (
        fmt is None
        or fmt.width < 1
        or fmt.height < 1
        or fmt.width * fmt.height > DEFAULT_LIMITS.max_image_pixels
    ):
        return RenderingSupport(version=1, targets=[])
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
            support = RenderingSupport(version=1, targets=[candidate])
            validate_rendering_support(context.metadata, support)
        except ValueError:
            continue
        targets.append(candidate)
    return RenderingSupport(version=1, targets=targets)


@dataclass
class _Job:
    message: object
    body: BindingRenderBody
    lease: object
    target: object
    ticket: tuple
    deadline: float
    direct: bool = False
    done: bool = False
    command_id: str | None = None
    rejection: object = None
    result_emitted: bool = False


class BindingRenderer:
    def __init__(self, host):
        self.host = host
        self.jobs = OrderedDict()
        self.active = {}

    def _retained(self, key):
        job = self.jobs.get(key)
        if job is not None and job.done and job.deadline + 60 < time.monotonic():
            self.jobs.pop(key)
            return False
        return job is not None

    def _context(self, message, lease):
        host = self.host
        state = RenderBindingState(
            binding=lease.context.metadata,
            rendering=lease.context.rendering,
            active=host.config_active and host.binding_output_authorized(lease),
            contract=lease.context.contract,
            runtimeEndpoint=f"service:action-runtime.{lease.provider_instance_id}",
            runtimeSessionId=lease.provider_session_id,
        )
        return RenderCommandContext(
            contract=message.contract,
            controllerEndpoint=str(host._endpoint.address),
            controllerSessionId=host._endpoint.session_id,
            runtimeEndpoint=f"service:action-runtime.{lease.provider_instance_id}",
            runtimeSessionId=lease.provider_session_id,
            localAuthority=host.current_contract(lease.provider_session_key)
            == message.contract,
            hardwareAuthority=host._command_service.has_authority(
                host.config_id, host.hardware_ref
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
        try:
            with anyio.move_on_after(1):
                await self.host._action_service.send_render_result(message, result)
        except Exception:
            logger.warning(
                "Could not send render result for %s", message.message_id, exc_info=True
            )

    async def handle(self, message):
        try:
            service_body = ServiceMessageBody.from_wire(message.body)
            if service_body.name != "binding_render":
                return False
            params = thaw_json(service_body.params)
            target = BindingCommandTarget.from_wire(params["target"])
        except (ValueError, TypeError, KeyError):
            return False
        lease = self.host.binding_by_id(target.binding_id)
        if lease is None:
            return False
        key = await self.host._action_service.render_message_key(message)
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
        async with self.host._nav_lock:
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
        control_id = lease.control_id
        if self.active.get(control_id, 0) >= 2 or not _WORKERS.acquire(blocking=False):
            if not direct:
                await self._result(message, body, "rejected", "busy")
            return True
        self.active[control_id] = self.active.get(control_id, 0) + 1
        try:
            router = lease.context._router
            ticket = await self.host._render_dispatcher.reserve_base(
                control_id=control_id,
                context_id=lease.context_id,
                binding_id=lease.binding_id,
                output=router._output,
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
            self.host._start_soon(self._run, job)
            self.host._start_soon(self._expire, job)
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
        envelope = message.model_copy(
            update={
                "message_type": "serviceMessage",
                "subject": entity_subject(
                    "service",
                    serviceId=f"action-runtime.{lease.provider_instance_id}",
                    namespace="dev.deckr.action_runtime.provider",
                    name="binding_render",
                ),
                "body": ServiceMessageBody(
                    serviceNamespace="dev.deckr.action_runtime.provider",
                    name="binding_render",
                    intent="command",
                    exchangePattern="one_way",
                    params=render_body.to_dict(),
                ).to_dict(),
            }
        )
        key = (
            str(message.sender),
            message.sender_session_id,
            message.contract.contract_id,
            message.contract.generation,
            message.message_id,
        )
        if self._retained(key):
            return
        async with self.host._nav_lock:
            try:
                self._validate_direct(envelope, lease, support)
            except (ValueError, TypeError):
                return
            await self._admit(envelope, render_body, lease, support, key, direct=True)

    def _validate_direct(self, message, lease, support):
        facts = self._context(message, lease)
        state = facts.bindings[lease.binding_id]
        body = BindingRenderBody.from_wire(
            thaw_json(ServiceMessageBody.from_wire(message.body).params)
        )
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

    @staticmethod
    def _direct_frame(job):
        data = RenderImage.from_wire(thaw_json(job.body.content.data))
        source = decode_image(data, job.target.limits)
        fmt = job.lease.control.image_format
        if source.size != (fmt.width, fmt.height):
            raise ContentFailure("invalid_content")
        # The managed hardware boundary validates descriptor encoding constraints.
        if fmt.rotation:
            source = source.rotate(-fmt.rotation, expand=True)
        out = io.BytesIO()
        source.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    async def _finish(self, job, status, code=None):
        if job.done:
            return
        job.done = True
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

    async def _expire(self, job):
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
        if not await self.host._action_service.authorize_runtime_message(
            job.lease.provider_session_key, job.message
        ):
            raise RenderRejection("target_not_current")
        if job.direct:
            self._validate_direct(job.message, job.lease, job.target)
        else:
            validate_render_command(job.message, self._context(job.message, job.lease))
        if job.done or time.monotonic() >= job.deadline:
            raise ContentFailure("deadline_exceeded")

    async def _run(self, job):
        router = job.lease.context._router
        dispatcher = self.host._render_dispatcher
        try:
            if job.body.operation == "clear":
                image = None
            elif job.direct:
                image = await anyio.to_thread.run_sync(self._direct_frame, job)
            elif job.body.content.format == INVARIANT_FORMAT:
                document = thaw_json(job.body.content.data)
                validate_graph(document, job.target.limits)
                with anyio.fail_after(max(0, job.deadline - time.monotonic())):
                    image = await anyio.to_process.run_sync(
                        render_graph,
                        document,
                        job.target.width,
                        job.target.height,
                        job.target.limits,
                        cancellable=True,
                        limiter=_GRAPH_WORKERS,
                    )
            else:
                image = await anyio.to_thread.run_sync(
                    render_content,
                    job.body.content,
                    job.target.width,
                    job.target.height,
                    job.target.limits,
                )
            image_uri = (
                "data:image/png;base64," + base64.b64encode(image).decode("ascii")
                if image is not None
                else None
            )
            while not job.done:
                await self._authorize(job)
                if not dispatcher.base_is_current(job.ticket):
                    await self._finish(job, "superseded", "newer_output")
                    return
                revision = dispatcher.composition_revision(job.ticket)
                overlay = router._store.overlay
                model = RenderModel(
                    image=image_uri,
                    overlay_type=overlay.template if overlay else None,
                    overlay_title=overlay.title if overlay else None,
                )
                request = router._render_service.build_request(
                    model,
                    router._image_format,
                    config_id=self.host.config_id,
                    generation=revision,
                    source=RenderSource(
                        provider_instance_id=job.lease.provider_instance_id,
                        provider_id=job.lease.context.metadata.provider_id,
                        provider_session_id=job.message.sender_session_id,
                        action_id=job.lease.context.metadata.action_id,
                        action_instance_id=job.lease.context.metadata.action_instance_id,
                        action_message_id=job.message.message_id,
                        action_causation_id=job.message.causation_id,
                        command_type="binding_output"
                        if job.direct
                        else "binding_render",
                        content_kind=job.body.content.format
                        if job.body.content
                        else None,
                        content=thaw_json(job.body.content.data)
                        if job.body.content
                        else None,
                        binding_output_generation=job.body.target.generation,
                    ),
                    context_id=job.lease.context_id,
                    binding_id=job.lease.binding_id,
                    control_id=job.lease.control_id,
                )
                frame = None
                if request is not None:
                    if job.direct and overlay is None:
                        frame = base64.b64decode(
                            job.body.content.data["image"], validate=True
                        )
                    else:
                        result = await self.host._render_backend.render(request)
                        if result.frame is None:
                            raise ContentFailure("render_failed")
                        frame = result.frame
                if job.done:
                    return
                async with self.host._nav_lock:
                    await self._authorize(job)
                    job.command_id = str(uuid.uuid4())
                    self.host._command_service.observe_render_command(
                        job.command_id,
                        self.host.config_id,
                        job.lease.control_id,
                        job.lease.raster_capability_id,
                        "clear" if frame is None else "set_frame",
                        lambda message: self._hardware_rejection(job, message),
                    )

                    def committed():
                        router._store.content.image = image_uri
                        router._store.content.title = None

                    with anyio.fail_after(max(0, job.deadline - time.monotonic())):
                        sent = await dispatcher.publish_base(
                            job.ticket,
                            revision,
                            frame,
                            message_id=job.command_id,
                            authorize=lambda: self._authorize(job),
                            committed=committed,
                        )
                if sent:
                    await self._finish(job, "dispatched")
                    return
        except RenderRejection:
            await self._finish(job, "superseded", "target_not_current")
        except ContentFailure as exc:
            await self._finish(job, "failed", exc.code)
        except TimeoutError:
            await self._finish(job, "failed", "deadline_exceeded")
        except Exception:
            logger.warning(
                "Rendering failed for %s", job.message.message_id, exc_info=True
            )
            if dispatcher.base_is_current(job.ticket):
                await self._finish(
                    job,
                    "failed",
                    "dispatch_failed" if job.command_id else "render_failed",
                )
            else:
                await self._finish(job, "superseded", "newer_output")
        finally:
            self.active[job.lease.control_id] -= 1
            _WORKERS.release()
