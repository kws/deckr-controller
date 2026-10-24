"""Binding rendering admission, bounded workers, dispatch, and correlated results."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deckr.contracts.messages import DeckrMessage

    from deckr.controller._bindings._render_policy import RenderingPorts, _Job, _Receipt


import base64
import io
import logging
import time
import uuid
import weakref
from typing import TYPE_CHECKING

import anyio
import anyio.to_process
from deckr.action_runtime import (
    RenderRejection,
)
from deckr.actions.rendering import (
    RenderImage,
)
from deckr.contracts.models import thaw_json

from deckr.controller._render import RenderModel, RenderSource
from deckr.controller._render_content import (
    ContentFailure,
    decode_image,
    render_content,
)
from deckr.controller._render_workers import render_with_retry, run_process, run_thread

if TYPE_CHECKING:
    pass
from deckr.controller._bindings._render_policy import _GRAPH_WORKERS
from deckr.controller._render_graph import (
    INVARIANT_FORMAT,
    render_graph,
)

logger = logging.getLogger(__name__)


class RenderExecution:
    """Run bounded content and composition work for an admitted render job."""

    def __init__(
        self,
        *,
        ports: Callable[[], RenderingPorts],
        authorize: Callable[[_Job], Awaitable[None]],
        finish: Callable[..., Awaitable[None]],
        hardware_rejection: Callable[[_Receipt, DeckrMessage], Awaitable[None]],
    ):
        self.ports, self.authorize, self.finish = ports, authorize, finish
        self.hardware_rejection = hardware_rejection

    @staticmethod
    def direct_frame(job):
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

    async def execute(self, job):
        router = job.lease.context.presentation
        dispatcher = self.ports().dispatcher
        try:
            if job.body.operation == "clear":
                image = None
            elif job.direct:
                image = await run_thread(self.direct_frame, job)
            elif job.body.content.format == INVARIANT_FORMAT:
                document = thaw_json(job.body.content.data)
                # Supersession invalidates publication immediately, but let this
                # bounded pure computation finish so page churn does not destroy
                # warm workers. Its original deadline still kills stalled work.
                with (
                    anyio.CancelScope(shield=True),
                    anyio.fail_after(max(0, job.deadline - time.monotonic())),
                ):
                    image = await run_process(
                        render_graph,
                        document,
                        job.target.width,
                        job.target.height,
                        job.target.limits,
                        limiter=_GRAPH_WORKERS,
                    )
            else:
                image = await run_thread(
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
                await self.authorize(job)
                if not dispatcher.base_is_current(job.ticket):
                    await self.finish(job, "superseded", "newer_output")
                    return
                revision = dispatcher.composition_revision(job.ticket)
                overlay = router.store.overlay
                model = RenderModel(
                    image=image_uri,
                    overlay_type=overlay.template if overlay else None,
                    overlay_title=overlay.title if overlay else None,
                )
                request = router.render_service.build_request(
                    model,
                    router.image_format,
                    config_id=self.ports().config_id,
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
                        with anyio.CancelScope(shield=True):
                            result = await render_with_retry(
                                self.ports().backend,
                                request,
                                is_current=lambda: not job.done
                                and dispatcher.base_is_current(job.ticket),
                                timeout=max(0, job.deadline - time.monotonic()),
                            )
                        if result is None:
                            await self.finish(job, "superseded", "newer_output")
                            return
                        if result.frame is None:
                            raise ContentFailure("render_failed")
                        frame = result.frame
                if job.done:
                    return
                await self.authorize(job)
                job.command_id = str(uuid.uuid4())
                receipt = job.receipt
                receipt.command_id = job.command_id
                receiver = weakref.WeakMethod(self.hardware_rejection)

                async def rejected(message, receipt=receipt, receiver=receiver):
                    callback = receiver()
                    if callback is not None:
                        await callback(receipt, message)

                self.ports().hardware.observe_render_command(
                    job.command_id,
                    self.ports().config_id,
                    job.lease.control_id,
                    job.lease.raster_capability_id,
                    "clear" if frame is None else "set_frame",
                    rejected,
                )

                def committed():
                    router.store.content.image = image_uri
                    router.store.content.title = None

                with anyio.fail_after(max(0, job.deadline - time.monotonic())):
                    sent = await dispatcher.publish_base(
                        job.ticket,
                        revision,
                        frame,
                        message_id=job.command_id,
                        authorize=lambda: self.authorize(job),
                        committed=committed,
                        recompose=router.render,
                    )
                if sent:
                    await self.finish(job, "dispatched")
                    return
        except RenderRejection:
            await self.finish(job, "superseded", "target_not_current")
        except ContentFailure as exc:
            await self.finish(job, "failed", exc.code)
        except TimeoutError:
            await self.finish(job, "failed", "deadline_exceeded")
        except Exception:
            logger.warning(
                "Rendering failed for %s", job.message.message_id, exc_info=True
            )
            if dispatcher.base_is_current(job.ticket):
                await self.finish(
                    job,
                    "failed",
                    "dispatch_failed" if job.command_id else "render_failed",
                )
            else:
                await self.finish(job, "superseded", "newer_output")
