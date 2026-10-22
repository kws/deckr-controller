"""Controller configuration and routes over the managed hardware boundary."""

import logging
from dataclasses import dataclass

import anyio
from deckr.core.util.owned_tasks import OwnedTaskGroup
from deckr.hardware import (
    DeckrHardware,
    HardwareCandidate,
    HardwareClaimSpec,
    HardwareUnavailable,
    ManagedHardwareClaim,
    ManagedHardwareUse,
)
from deckr.hardware import messages as hw

from deckr.controller._hardware._commands import HardwareCommandService
from deckr.controller._hardware._models import (
    ControllerHardwareSnapshot,
    HardwareClaimSnapshot,
    HardwareServiceCallbacks,
    ref_key,
)
from deckr.controller._hardware._routes import DeviceRouteRegistry, LiveDeviceRoute
from deckr.controller._stop_aware import cancel_on_stopping, sleep_until_stopping
from deckr.controller.config import DeviceConfig, DeviceConfigService

logger = logging.getLogger(__name__)


@dataclass
class _DeviceUse:
    candidate: HardwareCandidate
    config_id: str
    use: ManagedHardwareUse | None = None
    events_scope: anyio.CancelScope | None = None
    events_done: anyio.Event | None = None


class ControllerHardwareService:
    def __init__(
        self,
        *,
        hardware: DeckrHardware,
        config_service: DeviceConfigService,
        callbacks: HardwareServiceCallbacks,
    ) -> None:
        self._hardware = hardware
        self._config_service = config_service
        self._callbacks = callbacks
        self._routes = DeviceRouteRegistry()
        self._devices: dict[tuple[str, str], _DeviceUse] = {}
        self._lock = anyio.Lock()
        self._tasks: OwnedTaskGroup | None = None
        self._closed = False
        self.command_service = HardwareCommandService(
            route_lookup=self.route_for_config,
            claim_lookup=self.claim_for_config,
        )

    async def start(self, tg: anyio.abc.TaskGroup, stopping: anyio.Event) -> None:
        self._tasks = OwnedTaskGroup(
            tg, not_started_message="Controller hardware is not running"
        )
        group = await self._tasks.start()
        await self._hardware.__aenter__()
        group.start_soon(self._discovery_loop, stopping)
        group.start_soon(self._config_loop, stopping)
        group.start_soon(self._stop_loop, stopping)

    async def _stop_loop(self, stopping):
        await stopping.wait()
        await self._hardware.aclose()

    async def aclose(self) -> None:
        self._closed = True
        with anyio.CancelScope(shield=True):
            await self._hardware.aclose()
            if self._tasks is not None:
                await self._tasks.aclose()
            for live in self._routes.all():
                await self._disconnect(live.config_id, "controller stop")

    def claim_for_config(self, config_id: str) -> ManagedHardwareClaim | None:
        live = self._routes.get(config_id)
        if live is None:
            return None
        device = self._devices.get(ref_key(live.ref))
        if device is None or device.use is None:
            return None
        try:
            claim = device.use.claim
            if claim.state.contract == live.contract:
                return claim
        except HardwareUnavailable:
            pass
        return None

    def route_for_config(self, config_id: str) -> LiveDeviceRoute | None:
        return (
            self._routes.get(config_id)
            if self.claim_for_config(config_id) is not None
            else None
        )

    def snapshot(self) -> ControllerHardwareSnapshot:
        return ControllerHardwareSnapshot(
            candidates=self._hardware.directory.state.candidates,
            owned_claims=tuple(
                HardwareClaimSnapshot(
                    device.config_id, device.candidate.device_ref, device.use.state
                )
                for device in self._devices.values()
                if device.use is not None
            ),
            live_routes=tuple(
                live
                for live in self._routes.all()
                if self.route_for_config(live.config_id)
            ),
        )

    async def wait_current(self) -> None:
        async with self._hardware.directory.watch_state() as states:
            async for state in states:
                if state.current:
                    return
        raise HardwareUnavailable("discovery_closed", "Hardware discovery closed")

    async def _discovery_loop(self, stopping):
        async with (
            cancel_on_stopping(stopping),
            self._hardware.directory.watch_state() as states,
        ):
            async for _state in states:
                await self.reconcile(reason="hardware discovery")

    async def _config_loop(self, stopping):
        while not stopping.is_set():
            await self.reconcile(reason="configuration")
            await sleep_until_stopping(stopping, 1.0)

    async def reconcile(self, *, reason: str) -> None:
        if self._closed:
            return
        async with self._lock:
            # Existing agreements remain independent of discovery freshness.
            for device in tuple(self._devices.values()):
                if device.use is not None:
                    await self._route(device, reason)
            state = self._hardware.directory.state
            if not state.current:
                return
            candidate_counts = {}
            for candidate in state.candidates:
                key = ref_key(candidate.device_ref)
                candidate_counts[key] = candidate_counts.get(key, 0) + 1
            for candidate in state.candidates:
                key = ref_key(candidate.device_ref)
                if key in self._devices or candidate_counts[key] != 1:
                    continue
                config = await self._match(candidate)
                if config is None or config.id in {
                    d.config_id for d in self._devices.values()
                }:
                    continue
                device = _DeviceUse(candidate, config.id)
                self._devices[key] = device
                self._tasks.group.start_soon(self._run_device, key, device)

    async def _match(self, candidate: HardwareCandidate) -> DeviceConfig | None:
        try:
            config = await self._config_service.match_device(
                fingerprint=candidate.descriptor.fingerprint,
                labels=candidate.labels,
            )
            return config if config is not None and config.enabled else None
        except ValueError:
            logger.warning(
                "Ambiguous hardware configuration for %s", candidate.device_ref
            )
            return None

    async def _run_device(self, key, device):
        def recover(_previous, candidates):
            matching = [
                candidate
                for candidate in candidates
                if ref_key(candidate.device_ref) == key
            ]
            # Do not choose arbitrarily between competing manager sessions.
            return HardwareClaimSpec((matching[0],)) if len(matching) == 1 else None

        try:
            async with self._hardware.use(
                HardwareClaimSpec((device.candidate,)), recover=recover
            ) as use:
                device.use = use
                async with use.watch_state() as states:
                    async for _state in states:
                        async with self._lock:
                            await self._route(device, "managed hardware state")
        except (HardwareUnavailable, TimeoutError, anyio.ClosedResourceError):
            logger.info("Hardware use ended for %s", key, exc_info=True)
        finally:
            with anyio.CancelScope(shield=True):
                if device.events_scope is not None:
                    device.events_scope.cancel()
                await self._disconnect(device.config_id, "hardware use ended")
                if self._devices.get(key) is device:
                    del self._devices[key]

    async def _route(self, device, reason):
        try:
            await self._route_authorized(device, reason)
        except (HardwareUnavailable, anyio.ClosedResourceError):
            # Configuration lookup and stream startup may race agreement loss.
            # Only this device loses its route; other managed uses keep running.
            if device.events_scope is not None:
                device.events_scope.cancel()
                device.events_scope = None
            await self._disconnect(device.config_id, "hardware authority lost")

    async def _route_authorized(self, device, reason):
        use = device.use
        if not use.state.local_authority:
            if device.events_scope is not None:
                device.events_scope.cancel()
                device.events_scope = None
            await self._disconnect(device.config_id, reason)
            return
        claim = use.claim
        candidate = use.spec.candidates[0]
        device.candidate = candidate
        live = self._routes.get(device.config_id)
        config = None
        # Retain configuration selection only within the same routed agreement.
        # A successor must match its own fingerprint and labels before routing.
        if live is not None and live.contract == claim.state.contract:
            config = await self._config_service.get_config(device.config_id)
        if config is None or not config.enabled:
            config = await self._match(candidate)
        if config is None or not config.enabled:
            await self._disconnect(device.config_id, "configuration unavailable")
            return
        if config.id != device.config_id:
            if any(
                d is not device and d.config_id == config.id
                for d in self._devices.values()
            ):
                await self._disconnect(
                    device.config_id, "configuration already assigned"
                )
                return
            await self._disconnect(device.config_id, "hardware configuration changed")
            device.config_id = config.id
        live = self._routes.get(device.config_id)
        if live is not None and live.contract == claim.state.contract:
            claim.require_authority()
            return
        await self._disconnect(device.config_id, "hardware agreement changed")
        if device.events_scope is not None:
            device.events_scope.cancel()
        if device.events_done is not None:
            await device.events_done.wait()
        claim.require_authority()
        live = self._routes.connect(
            config_id=config.id,
            ref=candidate.device_ref,
            device=candidate.descriptor,
            contract=claim.state.contract,
            manager_session_id=claim.manager_session_id,
        )
        # Subscribe before exposing the new route to application callbacks.
        started = await self._tasks.group.start(self._events, device, claim, live)
        if not started:
            await self._disconnect(device.config_id, "hardware event stream closed")
            return
        claim.require_authority()
        await self._callbacks.on_hardware_connected(live, initial_config=config)

    async def _events(
        self, device, claim, live, *, task_status=anyio.TASK_STATUS_IGNORED
    ):
        with anyio.CancelScope() as scope:
            device.events_scope = scope
            done = device.events_done = anyio.Event()
            started = False
            try:
                async with claim.events() as events:
                    task_status.started(True)
                    started = True
                    async for event in events:
                        if self.route_for_config(live.config_id) != live:
                            return
                        if isinstance(event.body, hw.ControlInputMessage):
                            await self._callbacks.on_hardware_control_input(
                                live, event.message
                            )
                        elif isinstance(event.body, hw.CapabilityStateChangedMessage):
                            await self._callbacks.on_hardware_capability_state_changed(
                                live, event.body
                            )
                        elif isinstance(event.body, hw.CommandRejectedMessage):
                            await self.command_service.render_command_rejected(
                                live, event.message
                            )
                            await self._callbacks.on_hardware_command_rejected(
                                live, event.body
                            )
            except (HardwareUnavailable, anyio.ClosedResourceError):
                if not started:
                    task_status.started(False)
            finally:
                done.set()

    async def _disconnect(self, config_id, reason):
        if self._routes.disconnect_config(config_id) is not None:
            await self._callbacks.on_hardware_disconnected(config_id, reason=reason)

    async def disconnect_config(self, config_id, *, release_claim, reason):
        for device in tuple(self._devices.values()):
            if (
                device.config_id == config_id
                and release_claim
                and device.use is not None
            ):
                await device.use.aclose()
        await self._disconnect(config_id, reason)
