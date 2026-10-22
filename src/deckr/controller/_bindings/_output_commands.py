"""Inbound action-provider command handling for controller bindings."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deckr.controller._bindings._commands import ProviderCommandHost


import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

from deckr.actions.messages import (
    BINDING_OVERLAY,
    BINDING_OVERLAY_CLEAR,
    BindingMetadata,
    BindingOutputBody,
    BindingOverlayBody,
    BindingOverlayClearBody,
)
from deckr.contracts.messages import DeckrMessage
from deckr.hardware.capabilities import (
    raster_bitmap_command_params,
)
from pydantic import ValidationError

from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._attachments import (
    binding_body_matches_lease as _binding_body_matches_lease,
)
from deckr.controller._render import RenderSource

logger = logging.getLogger(__name__)


def _message_trace_payload(msg: DeckrMessage) -> dict[str, Any] | None:
    if msg.trace is None:
        return None
    return msg.trace.model_dump(by_alias=True, exclude_none=True, mode="json")


def _binding_overlay_render_source(
    binding: BindingMetadata,
    msg: DeckrMessage,
    *,
    command_type: str,
    overlay_generation: int,
) -> RenderSource:
    return RenderSource(
        provider_instance_id=binding.provider_instance_id,
        provider_id=binding.provider_id,
        provider_session_id=msg.sender_session_id,
        action_id=binding.action_id,
        action_instance_id=binding.action_instance_id,
        action_message_id=msg.message_id,
        action_causation_id=msg.causation_id,
        trace=_message_trace_payload(msg),
        command_type=command_type,
        binding_output_generation=binding.output_generation,
        overlay_generation=overlay_generation,
    )


def _payload_kind_hash(params: Mapping[str, Any]) -> tuple[str, str | None]:
    image = params.get("image")
    if isinstance(image, str):
        if image.startswith("data:"):
            kind = "data_uri"
        elif image.startswith(("http://", "https://")):
            kind = "remote_uri"
        elif image:
            kind = "encoded_image"
        else:
            kind = "empty_image"
        return kind, hashlib.sha256(image.encode("utf-8")).hexdigest()[:12]
    if not params:
        return "empty", None
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return "params", hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class BindingOutputCommands:
    """Apply authorized raster and overlay commands to the current binding."""

    def __init__(self, *, host: ProviderCommandHost, logger: logging.Logger):
        self._host, self._logger = host, logger

    async def _handle_binding_output(
        self,
        lease: BindingLease,
        body: BindingOutputBody,
        msg: DeckrMessage,
    ) -> None:
        if not self._host.binding_output_authorized(lease):
            self._logger.warning(
                "Ignoring binding output from non-output owner binding %s",
                lease.binding_id,
            )
            return
        if not _binding_body_matches_lease(lease, body.binding):
            self._logger.warning(
                "Ignoring binding output with mismatched mirrored lease identity"
            )
            return
        if body.binding.output_generation != body.generation:
            self._logger.warning(
                "Ignoring binding output with mismatched generation for binding %s",
                lease.binding_id,
            )
            return
        if body.capability.control_id != lease.control_id:
            self._logger.warning(
                "Ignoring binding output for wrong control %s on binding %s",
                body.capability.control_id,
                lease.binding_id,
            )
            return
        if body.capability.device_ref != lease.context.metadata.device_ref:
            self._logger.warning("Ignoring binding output for a different device")
            return
        if body.capability.capability_id != lease.raster_capability_id:
            self._logger.warning(
                "Ignoring unsupported binding output capability %s on binding %s",
                body.capability.capability_id,
                lease.binding_id,
            )
            return
        payload_kind, payload_hash = _payload_kind_hash(body.params)
        self._logger.debug(
            "Accepted binding output config=%s control=%s action=%s provider=%s "
            "binding=%s command_type=%s generation=%s capability=%s "
            "payload_kind=%s payload_hash=%s",
            self._host.config_id,
            lease.control_id,
            lease.action_id,
            lease.provider_instance_id,
            lease.binding_id,
            body.command_type,
            body.generation,
            body.capability.capability_id,
            payload_kind,
            payload_hash,
        )
        try:
            raster_bitmap_command_params(body.command_type, body.params)
        except (ValueError, ValidationError):
            return
        if body.generation != lease.context.metadata.output_generation:
            return
        await self._host.render_direct_output(lease, body, msg)

    async def _handle_binding_overlay(
        self,
        lease: BindingLease,
        body: BindingOverlayBody,
        msg: DeckrMessage,
    ) -> None:
        if not self._host.binding_output_authorized(lease):
            self._logger.warning(
                "Ignoring binding overlay from non-output owner binding %s",
                lease.binding_id,
            )
            return
        if not _binding_body_matches_lease(lease, body.binding):
            self._logger.warning(
                "Ignoring binding overlay with mismatched mirrored lease identity"
            )
            return
        ok = await lease.context.show_overlay(
            template=body.template,
            title=body.title,
            params=dict(body.params),
            duration_seconds=body.duration_seconds,
            overlay_id=body.overlay_id,
            generation=body.generation,
            binding_output_generation=body.binding.output_generation,
            source=_binding_overlay_render_source(
                body.binding,
                msg,
                command_type=BINDING_OVERLAY,
                overlay_generation=body.generation,
            ),
        )
        if not ok:
            self._logger.info(
                "Ignoring stale binding overlay for binding %s generation=%s",
                lease.binding_id,
                body.generation,
            )

    async def _handle_binding_overlay_clear(
        self,
        lease: BindingLease,
        body: BindingOverlayClearBody,
        msg: DeckrMessage,
    ) -> None:
        if not self._host.binding_output_authorized(lease):
            self._logger.warning(
                "Ignoring binding overlay clear from non-output owner binding %s",
                lease.binding_id,
            )
            return
        if not _binding_body_matches_lease(lease, body.binding):
            self._logger.warning(
                "Ignoring binding overlay clear with mismatched mirrored lease identity"
            )
            return
        ok = await lease.context.clear_overlay(
            overlay_id=body.overlay_id,
            generation=body.generation,
            binding_output_generation=body.binding.output_generation,
            source=_binding_overlay_render_source(
                body.binding,
                msg,
                command_type=BINDING_OVERLAY_CLEAR,
                overlay_generation=body.generation,
            ),
        )
        if not ok:
            self._logger.info(
                "Ignoring stale binding overlay clear for binding %s generation=%s",
                lease.binding_id,
                body.generation,
            )
