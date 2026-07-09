from __future__ import annotations

import logging

import anyio
from deckr.beacon import Beacon, BeaconDirectory
from deckr.concord import Concord, ConcordUnavailable
from deckr.contracts.messages import HARDWARE_MESSAGES_LANE, DeckrMessage
from deckr.core.util.anyio import CoalescedTrigger
from deckr.hardware import messages as hw_messages
from deckr.hardware.profiles import HARDWARE_CLAIM_PROFILE_ID, HARDWARE_FEATURE_ID
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
        controller_id: str,
        controller_session_id: str,
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
            controller_id=controller_id,
            controller_session_id=controller_session_id,
        )
        self._concord = concord
        self._stopping: anyio.Event | None = None
        self.command_service = HardwareCommandService(
            endpoint,
            route_lookup=self._routes.get,
        )

    async def start(self, tg: anyio.abc.TaskGroup, stopping: anyio.Event) -> None:
        self._stopping = stopping
        self._claims.set_start_soon(tg.start_soon)
        self._directory.start(tg)
        tg.start_soon(self._close_directory_on_stopping, stopping)
        tg.start_soon(self._input_loop, stopping)
        tg.start_soon(self._directory_snapshot_loop, stopping)
        tg.start_soon(self._claim_event_loop, stopping)
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
                event = hw_messages.hardware_body_from_message(message)
                ref = hw_messages.hardware_device_ref_from_message(message)
                if ref is None:
                    continue
                live = self._routes.get_by_ref(ref)
                if live is None:
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
                    await self._directory.wait_current()
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

    async def _claim_event_loop(self, stopping: anyio.Event) -> None:
        while not stopping.is_set():
            try:
                async with (
                    self._concord.watch(HARDWARE_CLAIM_PROFILE_ID) as stream,
                    cancel_on_stopping(stopping),
                ):
                    async for event in stream:
                        await self._reconcile_notifications.request(
                            f"hardware claim {event.event_type.value}"
                        )
            except ConcordUnavailable:
                await sleep_until_stopping(stopping, WATCH_RETRY_SECONDS)

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
        candidates = await self._hardware_candidates_from_beacon()
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
