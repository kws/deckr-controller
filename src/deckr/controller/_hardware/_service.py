from __future__ import annotations

import logging

import anyio
from deckr.beacon import Beacon, BeaconDirectory
from deckr.concord import Concord, ConcordUnavailable
from deckr.contracts.messages import (
    HARDWARE_MESSAGES_LANE,
    DeckrMessage,
    hardware_manager_address,
    message_is_expired,
)
from deckr.core.util.anyio import CoalescedTrigger
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import CapabilityRef, DeviceRef
from deckr.hardware.profiles import HARDWARE_FEATURE_ID
from deckr.hardware.state import HardwareUnavailable
from deckr.hardware.validation import resolve_capability, validate_input, validate_value
from deckr.lanes import EndpointSession
from deckr.substrates.nats_kv import KvUnavailable

from deckr.controller._hardware._claims import HardwareClaimCoordinator
from deckr.controller._hardware._commands import HardwareCommandService
from deckr.controller._hardware._discovery import (
    parse_hardware_candidate,
    select_hardware_candidates,
)
from deckr.controller._hardware._models import (
    ControllerHardwareSnapshot,
    HardwareCandidate,
    HardwareServiceCallbacks,
)
from deckr.controller._hardware._routes import DeviceRouteRegistry, LiveDeviceRoute
from deckr.controller._stop_aware import cancel_on_stopping, sleep_until_stopping
from deckr.controller.config import DeviceConfigService

logger = logging.getLogger(__name__)

STATE_RECONCILE_SECONDS = 15.0
STATE_NOTIFICATION_BATCH_SECONDS = 0.05
WATCH_RETRY_SECONDS = 1.0


def _route_sender_and_contract_match(
    live: LiveDeviceRoute,
    message: DeckrMessage,
) -> bool:
    claimed = message.contract
    expected = live.contract
    return (
        message.sender == hardware_manager_address(live.ref.manager_id)
        and claimed is not None
        and claimed.contract_id == expected.contract_id
        and claimed.generation == expected.generation
    )


def _route_authorizes_message(
    live: LiveDeviceRoute,
    message: DeckrMessage,
) -> bool:
    return (
        _route_sender_and_contract_match(live, message)
        and live.manager_session_id is not None
        and message.sender_session_id == live.manager_session_id
    )


class ControllerHardwareService:
    """Facade for controller hardware discovery, claims, routes, and input."""

    def __init__(
        self,
        *,
        endpoint: EndpointSession,
        beacon: Beacon,
        concord: Concord,
        config_service: DeviceConfigService,
        callbacks: HardwareServiceCallbacks,
    ) -> None:
        self._endpoint = endpoint
        self._routes = DeviceRouteRegistry()
        self._directory = BeaconDirectory(
            beacon,
            HARDWARE_FEATURE_ID,
            parse_hardware_candidate,
            log_label="ControllerHardware",
        )
        self._reconcile_lock = anyio.Lock()
        self._reconcile_notifications = CoalescedTrigger(
            batch_interval=STATE_NOTIFICATION_BATCH_SECONDS
        )
        self._candidates: dict[tuple[str, str], HardwareCandidate] = {}
        self._callbacks = callbacks
        self._claims = HardwareClaimCoordinator(
            concord=concord,
            config_service=config_service,
            route_registry=self._routes,
            callbacks=callbacks,
            endpoint=endpoint,
        )
        self._stopping: anyio.Event | None = None
        self.command_service = HardwareCommandService(
            endpoint,
            route_lookup=self._claims.authorized_route,
        )

    async def start(self, tg: anyio.abc.TaskGroup, stopping: anyio.Event) -> None:
        self._stopping = stopping
        self._claims.set_start_soon(tg.start_soon)
        self._claims.set_state_changed(self._reconcile_notifications.request)
        self._directory.start(tg)
        tg.start_soon(self._close_directory_on_stopping, stopping)
        tg.start_soon(self._input_loop, stopping)
        tg.start_soon(self._directory_snapshot_loop, stopping)
        tg.start_soon(self._notification_reconciliation_loop, stopping)
        tg.start_soon(self._reconciliation_loop, stopping)

    async def aclose(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        await self._directory.aclose()
        await self._reconcile_notifications.aclose()
        await self._claims.release_all(reason="controller stop")

    def snapshot(self) -> ControllerHardwareSnapshot:
        return ControllerHardwareSnapshot(
            candidates=tuple(self._candidates.values()),
            owned_claims=self._claims.snapshot(),
            live_routes=self._routes.all(),
        )

    def route_for_config(self, config_id: str) -> LiveDeviceRoute | None:
        return self._routes.get(config_id)

    async def wait_current(self) -> None:
        await self._directory.wait_current()

    async def reconcile(self, *, reason: str) -> None:
        async with self._reconcile_lock:
            await self._reconcile_locked(reason=reason)

    async def disconnect_config(
        self,
        config_id: str,
        *,
        release_claim: bool,
        reason: str,
    ) -> None:
        await self._claims.disconnect_config(
            config_id,
            release_claim=release_claim,
            reason=reason,
        )

    async def _input_loop(self, stopping: anyio.Event) -> None:
        async with (
            self._endpoint.subscribe(HARDWARE_MESSAGES_LANE) as subscribe,
            cancel_on_stopping(stopping),
        ):
            async for message in subscribe:
                if not isinstance(message, DeckrMessage):
                    continue
                try:
                    event = hw_messages.hardware_body_from_message(message)
                    ref = hw_messages.hardware_device_ref_from_message(message)
                    if ref is None:
                        continue
                    live = await self._authorized_ingress_route(ref, message)
                    if live is None:
                        continue
                    capability_ref = CapabilityRef(
                        deviceRef=event.device_ref,
                        controlId=event.control_id,
                        capabilityId=event.capability_id,
                    )
                    if message.subject != hw_messages.hardware_subject_for_capability(
                        capability_ref
                    ):
                        continue
                    capability = resolve_capability(
                        {live.device.device_id: live.device}, capability_ref
                    )
                    if isinstance(event, hw_messages.ControlInputMessage):
                        validate_input(capability, event.event_type, event.value)
                    elif isinstance(event, hw_messages.CapabilityStateChangedMessage):
                        if "emits" not in capability.access:
                            continue
                        validate_value(capability, event.value)
                except (HardwareUnavailable, ValueError, TypeError):
                    logger.debug("Ignoring invalid hardware input", exc_info=True)
                    continue
                if isinstance(event, hw_messages.ControlInputMessage):
                    await self._callbacks.on_hardware_control_input(live, message)
                elif isinstance(event, hw_messages.CapabilityStateChangedMessage):
                    await self._callbacks.on_hardware_capability_state_changed(
                        live,
                        event,
                    )
                elif isinstance(event, hw_messages.CommandRejectedMessage):
                    await self._callbacks.on_hardware_command_rejected(live, event)

    async def _authorized_ingress_route(
        self,
        ref: DeviceRef,
        message: DeckrMessage,
    ) -> LiveDeviceRoute | None:
        live = self._routes.get_by_ref(ref)
        if (
            live is None
            or not _route_authorizes_message(live, message)
            or message_is_expired(message)
            or getattr(message.recipient, "endpoint", None) != self._endpoint.address
            or message.recipient_session_id != self._endpoint.session_id
        ):
            return None
        return self._claims.authorized_route(live.config_id)

    async def _directory_snapshot_loop(self, stopping: anyio.Event) -> None:
        async with cancel_on_stopping(stopping):
            async for _records in self._directory.watch_records():
                if stopping.is_set():
                    return
                await self._reconcile_notifications.request(
                    "hardware Beacon directory snapshot"
                )

    async def _reconciliation_loop(self, stopping: anyio.Event) -> None:
        while not stopping.is_set():
            try:
                async with cancel_on_stopping(stopping):
                    try:
                        await self._directory.wait_current()
                    except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                        return
                if stopping.is_set():
                    return
                await self.reconcile(reason="broker snapshot")
            except (ConcordUnavailable, KvUnavailable):
                if stopping.is_set():
                    return
                logger.warning(
                    "Hardware current state unavailable; reconciliation will retry",
                    exc_info=True,
                )
            await sleep_until_stopping(stopping, STATE_RECONCILE_SECONDS)

    async def _notification_reconciliation_loop(
        self,
        stopping: anyio.Event,
    ) -> None:
        async def close_on_stopping() -> None:
            await stopping.wait()
            await self._reconcile_notifications.aclose()

        async with anyio.create_task_group() as tg:
            tg.start_soon(close_on_stopping)
            try:
                await self._reconcile_notifications.run(
                    self._reconcile_notification,
                    reason_prefix="hardware notifications",
                )
            finally:
                tg.cancel_scope.cancel()

    async def _reconcile_notification(self, reason: str) -> None:
        try:
            await self.reconcile(reason=reason)
        except (ConcordUnavailable, KvUnavailable):
            logger.warning(
                "Hardware current state unavailable; notification will retry",
                exc_info=True,
            )

    async def _reconcile_locked(self, *, reason: str) -> None:
        try:
            candidates = await self._hardware_candidates_from_beacon()
        except KvUnavailable:
            # Discovery freshness must not delay withdrawal of lost authority.
            await self._claims.reconcile({}, reason=reason)
            raise
        self._candidates = candidates
        await self._claims.reconcile(candidates, reason=reason)

    async def _hardware_candidates_from_beacon(
        self,
    ) -> dict[tuple[str, str], HardwareCandidate]:
        return select_hardware_candidates(self._directory.records())

    async def _close_directory_on_stopping(
        self,
        stopping: anyio.Event,
    ) -> None:
        await stopping.wait()
        await self._directory.aclose()
