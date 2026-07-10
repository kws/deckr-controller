"""Inbound action-provider command handling for controller bindings."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from deckr.action_runtime import action_runtime_provider_instance_id
from deckr.actions.endpoints import (
    RESERVED_BUILTIN_PROVIDER_IDS,
    parse_action_provider_address,
)
from deckr.actions.messages import (
    ACTION_LIFECYCLE_REJECTED,
    BINDING_OUTPUT,
    BINDING_OVERLAY,
    BINDING_OVERLAY_CLEAR,
    CLOSE_PAGE,
    OPEN_PAGE,
    REPLACE_PAGE,
    ActionLifecycleRejectedBody,
    BindingMetadata,
    BindingOutputBody,
    BindingOverlayBody,
    BindingOverlayClearBody,
    DynamicPageCommand,
    action_body_dict,
    subject_action_instance_id,
    subject_binding_id,
    subject_config_id,
    subject_context_id,
    subject_page_session_id,
    subject_provider_instance_id,
)
from deckr.contracts.messages import DeckrMessage, parse_service_address
from deckr.hardware.capabilities import (
    RasterBitmapClearParams,
    raster_bitmap_command_params,
)
from pydantic import ValidationError

from deckr.controller._actions import ProviderSessionKey
from deckr.controller._bindings._action_lifecycle import (
    ActionInstanceLifecycleService,
)
from deckr.controller._bindings._attachments import (
    AuthorizedCommandTarget,
    BindingLease,
)
from deckr.controller._pages import DynamicPageSession
from deckr.controller._render import RenderSource

logger = logging.getLogger(__name__)

_IMAGE_SOURCE_SCHEMES = ("data:", "http://", "https://")


class ProviderCommandHost(Protocol):
    config_id: str

    def binding_by_id(self, binding_id: str) -> BindingLease | None: ...

    def active_page_session(self) -> DynamicPageSession | None: ...

    def binding_command_authorized(self, lease: BindingLease) -> bool: ...

    def binding_output_authorized(self, lease: BindingLease) -> bool: ...

    def provider_session_key_for_session(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None,
    ) -> ProviderSessionKey | None: ...

    async def message_contract_authorized(
        self,
        msg: DeckrMessage,
        key: ProviderSessionKey | None,
    ) -> bool: ...

    async def recover_binding_provider_session_contract(
        self,
        lease: BindingLease,
        *,
        reason: str,
    ) -> None: ...

    async def open_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        binding_id: str | None = None,
        causation_id: str | None = None,
    ) -> DynamicPageSession | None: ...

    async def replace_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        causation_id: str | None = None,
    ) -> None: ...

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None: ...


def _descriptor_from_payload(data: dict) -> DynamicPageCommand | None:
    """Validate a dynamic page descriptor from a bus payload."""
    if not data:
        return None
    bindings_data = data.get("bindings")
    if not bindings_data:
        return None
    try:
        return DynamicPageCommand.model_validate(data)
    except ValidationError:
        logger.warning(
            "Ignoring invalid dynamic page descriptor payload", exc_info=True
        )
        return None


def _binding_output_image_source(params: Mapping[str, Any]) -> str | None:
    image = params.get("image")
    if not isinstance(image, str) or not image:
        return None
    if image.startswith(_IMAGE_SOURCE_SCHEMES):
        return image
    encoding = params.get("encoding")
    if encoding in {"jpeg", "png"}:
        return f"data:image/{encoding};base64,{image}"
    return None


def _image_source_content_kind(image_source: str) -> str:
    if image_source.startswith("data:application/vnd.invariant.graph"):
        return "invariant_graph"
    if image_source.startswith("data:"):
        return "data_image"
    if image_source.startswith(("http://", "https://")):
        return "remote_image"
    return "image"


def _message_trace_payload(msg: DeckrMessage) -> dict[str, Any] | None:
    if msg.trace is None:
        return None
    return msg.trace.model_dump(by_alias=True, exclude_none=True, mode="json")


def _binding_output_render_source(
    body: BindingOutputBody,
    msg: DeckrMessage,
    *,
    image_source: str,
) -> RenderSource:
    binding = body.binding
    return RenderSource(
        provider_instance_id=binding.provider_instance_id,
        provider_id=binding.provider_id,
        provider_session_id=msg.sender_session_id,
        action_id=binding.action_id,
        action_instance_id=binding.action_instance_id,
        action_message_id=msg.message_id,
        action_causation_id=msg.causation_id,
        trace=_message_trace_payload(msg),
        command_type=body.command_type,
        content_kind=_image_source_content_kind(image_source),
        binding_output_generation=body.generation,
    )


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


def _binding_body_matches_lease(lease: BindingLease, binding: BindingMetadata) -> bool:
    return (
        binding.provider_instance_id == lease.provider_instance_id
        and binding.provider_id == lease.provider_id
        and binding.action_id == lease.action_uuid
        and binding.context_id == lease.context_id
        and binding.binding_id == lease.binding_id
        and binding.action_instance_id == lease.action_instance_id
    )


class ProviderCommandIngress:
    def __init__(
        self,
        *,
        host: ProviderCommandHost,
        lifecycle: ActionInstanceLifecycleService,
        logger: logging.Logger = logger,
    ) -> None:
        self._host = host
        self._lifecycle = lifecycle
        self._logger = logger

    async def handle(self, msg: DeckrMessage) -> None:
        """Handle a canonical command message from an action provider."""
        try:
            payload = action_body_dict(msg)
        except (ValidationError, ValueError):
            self._logger.warning(
                "Ignoring invalid action command body %s from %s",
                msg.message_type,
                msg.sender,
                exc_info=True,
            )
            return
        msg_type = msg.message_type

        context_id = subject_context_id(msg.subject) or ""
        if not context_id:
            return
        config_id = subject_config_id(msg.subject)
        if config_id != self._host.config_id:
            return

        if msg_type == ACTION_LIFECYCLE_REJECTED:
            body = ActionLifecycleRejectedBody.model_validate(payload)
            authorization: AuthorizedCommandTarget | None = None
            sender_provider_instance_id: str | None = None
            if body.target_kind == "action_instance":
                sender_provider_instance_id = self._command_sender_provider_instance_id(
                    msg
                )
            elif body.reason != "stale_lifecycle" or body.target_kind == "binding":
                authorization = await self._authorize_action_command(
                    msg,
                    context_id=context_id,
                )
                if authorization is not None:
                    sender_provider_instance_id = (
                        authorization.sender_provider_instance_id
                    )
            await self._lifecycle.handle_lifecycle_rejected(
                msg,
                body,
                authorization=authorization,
                sender_provider_instance_id=sender_provider_instance_id,
                context_id=context_id,
            )
            return

        authorization = await self._authorize_action_command(
            msg,
            context_id=context_id,
        )
        if authorization is None:
            return

        if msg_type == OPEN_PAGE:
            if authorization.binding is None:
                self._logger.warning(
                    "Ignoring open_page without binding authority for %s",
                    context_id,
                )
                return
            desc_data = payload.get("descriptor")
            descriptor = _descriptor_from_payload(desc_data) if desc_data else None
            if descriptor is not None:
                await self._host.open_page(
                    descriptor=descriptor,
                    context_id=context_id,
                    binding_id=authorization.binding.binding_id,
                    causation_id=msg.message_id,
                )
            return

        if msg_type == REPLACE_PAGE:
            if authorization.page_session is None:
                self._logger.warning(
                    "Ignoring replace_page without page-session authority for %s",
                    context_id,
                )
                return
            desc_data = payload.get("descriptor")
            descriptor = _descriptor_from_payload(desc_data) if desc_data else None
            if descriptor is not None:
                await self._host.replace_page(
                    descriptor=descriptor,
                    context_id=authorization.page_session.context_id,
                    causation_id=msg.message_id,
                )
            return

        if msg_type == CLOSE_PAGE:
            if authorization.page_session is None:
                self._logger.warning(
                    "Ignoring close_page without page-session authority for %s",
                    context_id,
                )
                return
            await self._host.close_page(
                context_id=authorization.page_session.context_id,
                reason="close",
                causation_id=msg.message_id,
            )
            return

        if msg_type == BINDING_OUTPUT:
            if authorization.binding is not None:
                body = BindingOutputBody.model_validate(payload)
                await self._handle_binding_output(authorization.binding, body, msg)
            return

        if msg_type == BINDING_OVERLAY:
            if authorization.binding is not None:
                body = BindingOverlayBody.model_validate(payload)
                await self._handle_binding_overlay(authorization.binding, body, msg)
            return

        if msg_type == BINDING_OVERLAY_CLEAR:
            if authorization.binding is not None:
                body = BindingOverlayClearBody.model_validate(payload)
                await self._handle_binding_overlay_clear(
                    authorization.binding,
                    body,
                    msg,
                )
            return

        if authorization.page_session is not None:
            return

    def _command_sender_provider_instance_id(
        self,
        msg: DeckrMessage,
    ) -> str | None:
        provider_instance_id = parse_action_provider_address(msg.sender)
        if provider_instance_id is None:
            service_id = parse_service_address(msg.sender)
            provider_instance_id = (
                action_runtime_provider_instance_id(service_id)
                if service_id is not None
                else None
            )
        if provider_instance_id is None:
            self._logger.warning(
                "Ignoring action command %s from non-provider sender %s",
                msg.message_type,
                msg.sender,
            )
            return None
        if provider_instance_id in RESERVED_BUILTIN_PROVIDER_IDS:
            self._logger.warning(
                "Ignoring action command %s from external provider using reserved id %s",
                msg.message_type,
                provider_instance_id,
            )
            return None
        return provider_instance_id

    async def _authorize_action_command(
        self,
        msg: DeckrMessage,
        *,
        context_id: str,
    ) -> AuthorizedCommandTarget | None:
        sender_provider_instance_id = self._command_sender_provider_instance_id(msg)
        if sender_provider_instance_id is None:
            return None

        action_instance_id = subject_action_instance_id(msg.subject)
        binding_id = subject_binding_id(msg.subject)
        page_session_id = subject_page_session_id(msg.subject)
        subject_provider_id = subject_provider_instance_id(msg.subject)
        if (
            subject_provider_id is not None
            and subject_provider_id != sender_provider_instance_id
        ):
            self._logger.warning(
                "Ignoring action command %s from %s with mismatched subject provider %s",
                msg.message_type,
                msg.sender,
                subject_provider_id,
            )
            return None

        if binding_id is not None:
            lease = self._host.binding_by_id(binding_id)
            if lease is None or lease.context_id != context_id:
                self._logger.debug(
                    "Ignoring action command %s from %s for inactive binding %s",
                    msg.message_type,
                    msg.sender,
                    binding_id,
                )
                return None
            if not self._host.binding_command_authorized(lease):
                self._logger.warning(
                    "Ignoring action command %s for unauthorized binding %s",
                    msg.message_type,
                    binding_id,
                )
                return None
            if sender_provider_instance_id != lease.provider_instance_id:
                self._logger.warning(
                    "Ignoring action command %s from provider %s for binding owned by provider %s",
                    msg.message_type,
                    sender_provider_instance_id,
                    lease.provider_instance_id,
                )
                return None
            if msg.sender_session_id != lease.provider_session_id:
                self._logger.warning(
                    "Ignoring action command %s from stale provider session %s",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if not await self._host.message_contract_authorized(
                msg,
                lease.provider_session_key,
            ):
                await self._host.recover_binding_provider_session_contract(
                    lease,
                    reason=f"{msg.message_type}_contract_mismatch",
                )
                self._logger.warning(
                    "Ignoring action command %s from provider session %s without "
                    "matching Concord contract",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if (
                action_instance_id is not None
                and action_instance_id != lease.action_instance_id
            ):
                self._logger.warning(
                    "Ignoring action command %s for mismatched action instance %s",
                    msg.message_type,
                    action_instance_id,
                )
                return None
            if page_session_id is not None and page_session_id != lease.page_session_id:
                self._logger.warning(
                    "Ignoring action command %s for mismatched page session %s",
                    msg.message_type,
                    page_session_id,
                )
                return None
            return AuthorizedCommandTarget(
                sender_provider_instance_id=sender_provider_instance_id,
                context_id=context_id,
                binding=lease,
            )

        session = self._host.active_page_session()
        if page_session_id is not None:
            if (
                session is None
                or page_session_id != session.page_session_id
                or context_id != session.context_id
            ):
                self._logger.warning(
                    "Ignoring action command %s for inactive page session %s",
                    msg.message_type,
                    page_session_id,
                )
                return None
            if sender_provider_instance_id != session.owner_provider_instance_id:
                self._logger.warning(
                    "Ignoring action command %s from provider %s for page owned by provider %s",
                    msg.message_type,
                    sender_provider_instance_id,
                    session.owner_provider_instance_id,
                )
                return None
            if msg.sender_session_id != session.owner_provider_session_id:
                self._logger.warning(
                    "Ignoring page action command %s from stale provider session %s",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if not await self._host.message_contract_authorized(
                msg,
                self._host.provider_session_key_for_session(
                    provider_instance_id=session.owner_provider_instance_id,
                    provider_id=session.owner_provider_id,
                    provider_session_id=session.owner_provider_session_id,
                ),
            ):
                self._logger.warning(
                    "Ignoring page action command %s from provider session %s "
                    "without matching Concord contract",
                    msg.message_type,
                    msg.sender_session_id,
                )
                return None
            if (
                action_instance_id is not None
                and action_instance_id != session.action_instance_id
            ):
                self._logger.warning(
                    "Ignoring action command %s for mismatched page action instance %s",
                    msg.message_type,
                    action_instance_id,
                )
                return None
            return AuthorizedCommandTarget(
                sender_provider_instance_id=sender_provider_instance_id,
                context_id=context_id,
                page_session=session,
            )

        self._logger.warning(
            "Ignoring action command %s from %s without binding or page session subject",
            msg.message_type,
            msg.sender,
        )
        return None

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
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
            body.command_type,
            body.generation,
            body.capability.capability_id,
            payload_kind,
            payload_hash,
        )
        if body.command_type == "clear":
            try:
                params = raster_bitmap_command_params(body.command_type, body.params)
            except (ValueError, ValidationError) as exc:
                self._logger.warning(
                    "Ignoring invalid raster output command %s on binding %s: %s",
                    body.command_type,
                    lease.binding_id,
                    exc,
                )
                return
            if not isinstance(params, RasterBitmapClearParams):
                return
            await lease.context.clear_raster(generation=body.generation)
            return
        if body.command_type != "set_frame":
            self._logger.warning(
                "Ignoring unsupported raster output command %s on binding %s",
                body.command_type,
                lease.binding_id,
            )
            return
        image_source = _binding_output_image_source(body.params)
        if image_source is None:
            self._logger.warning(
                "Ignoring raster output without a valid image source on binding %s",
                lease.binding_id,
            )
            return
        source = _binding_output_render_source(body, msg, image_source=image_source)
        await lease.context.set_raster_image(
            image_source,
            generation=body.generation,
            source=source,
        )

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
