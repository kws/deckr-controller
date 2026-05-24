import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import uuid4

import anyio
from deckr.actions.messages import (
    COMMAND_MESSAGE_TYPES,
    SETTINGS_PATCH,
    SETTINGS_REPLACE,
    SETTINGS_REQUEST,
    SettingsPatchBody,
    SettingsReplaceBody,
    SettingsRequestBody,
    action_message_for_controller,
    subject_config_id,
)
from deckr.beacon import BeaconService, Candidate
from deckr.components import BaseComponent, RunContext
from deckr.concord import (
    ConcordParticipantLease,
    ConcordService,
    ContractHandle,
    ContractValidity,
    ContractValidityStatus,
    ParticipantHandle,
)
from deckr.contracts.messages import (
    DeckrMessage,
    controller_address,
)
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef
from deckr.hardware.profiles import (
    HARDWARE_CLAIM_PROFILE_ID,
    HARDWARE_FEATURE_ID,
    HardwareBeaconPayload,
    HardwareClaimDevice,
    HardwareClaimTerms,
    hardware_payload_from_advertisement,
)
from deckr.lanes import RegisteredEndpointLane
from deckr.state import StateConflict, StateUnavailable

from deckr.controller._device_manager import DeviceManager
from deckr.controller._hardware_service import (
    DeviceRouteRegistry,
    HardwareCommandService,
    LiveDeviceRoute,
)
from deckr.controller._render_dispatcher import (
    ProcessPoolRenderBackend,
    RenderBackend,
)
from deckr.controller.action_provider.action_registry import ActionRegistry
from deckr.controller.action_provider.events import ActionsChangedEvent
from deckr.controller.config import DeviceConfigService
from deckr.controller.settings import SettingsService

logger = logging.getLogger(__name__)

CLAIM_HEARTBEAT_SECONDS = 5.0
_STATE_RECONCILE_SECONDS = 1.0
_WATCH_RETRY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class HardwareCandidate:
    advertisement_key: str
    advertisement_id: str
    payload: HardwareBeaconPayload
    ref: DeviceRef
    device: DeviceDescriptor
    labels: Mapping[str, str]


@dataclass(slots=True)
class OwnedHardwareClaim:
    claim_id: str
    config_id: str
    ref: DeviceRef
    device: DeviceDescriptor
    contract: ContractHandle
    controller_token: ParticipantHandle
    controller_lease: ConcordParticipantLease
    manager_session_id: str
    manager_advertisement_id: str
    live: bool = False


class ControllerService(BaseComponent):
    def __init__(
        self,
        hardware_endpoint: RegisteredEndpointLane,
        beacon: BeaconService,
        concord: ConcordService,
        config_service: DeviceConfigService,
        settings_service: SettingsService,
        *,
        controller_id: str,
        action_registry: ActionRegistry | None = None,
        actions_endpoint: RegisteredEndpointLane | None = None,
        render_backend: RenderBackend | None = None,
    ):
        super().__init__()
        self._hardware_endpoint = hardware_endpoint
        self._beacon = beacon
        self._concord = concord
        self._device_registry = DeviceRouteRegistry()
        self._config_service = config_service
        self._settings_service = settings_service
        self._controller_id = controller_id
        self._command_service = HardwareCommandService(
            hardware_endpoint,
            controller_id=controller_id,
        )
        self._controller_contexts = AsyncMap[str, DeviceManager]()
        self._device_disconnect_events: dict[str, anyio.Event] = {}
        self._action_registry = action_registry
        self._actions_endpoint = actions_endpoint
        self._start_soon: Callable | None = None
        self._render_backend = render_backend
        self._session_id = hardware_endpoint.session_id
        self._owned_claims: dict[tuple[str, str], OwnedHardwareClaim] = {}
        self._unmatched_hardware_signatures: dict[
            tuple[str, str],
            tuple[str, tuple[tuple[str, str], ...]],
        ] = {}
        self._hardware_candidates: dict[tuple[str, str], HardwareCandidate] = {}
        self._hardware_reconcile_lock = anyio.Lock()
        self._stopping: anyio.Event | None = None

    async def _handle_action_command(self, msg: DeckrMessage) -> None:
        """Route command messages to the appropriate DeviceManager."""
        if msg.message_type not in COMMAND_MESSAGE_TYPES:
            return
        config_id = subject_config_id(msg.subject)
        if config_id is None and msg.message_type in {
            SETTINGS_REQUEST,
            SETTINGS_PATCH,
            SETTINGS_REPLACE,
        }:
            body_type = {
                SETTINGS_REQUEST: SettingsRequestBody,
                SETTINGS_PATCH: SettingsPatchBody,
                SETTINGS_REPLACE: SettingsReplaceBody,
            }[msg.message_type]
            try:
                config_id = body_type.model_validate(msg.body).target.config_id
            except ValueError:
                logger.warning(
                    "Ignoring invalid settings command %s from %s",
                    msg.message_type,
                    msg.sender,
                    exc_info=True,
                )
                return
        if config_id is None:
            logger.warning(
                "Ignoring action command %s without config subject from %s",
                msg.message_type,
                msg.sender,
            )
            return
        ctrl_ctx = await self._controller_contexts.get(config_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.handle_command(msg)

    async def handle_actions_changed_event(self, event: ActionsChangedEvent) -> None:
        controller_contexts = await self._controller_contexts.values()
        logger.info(
            "Applying ActionsChangedEvent to %d device(s): +%s -%s",
            len(controller_contexts),
            event.registered,
            event.unregistered,
        )
        for ctrl_ctx in controller_contexts:
            await ctrl_ctx.on_actions_changed(event.registered, event.unregistered)

    async def _actions_subscription_loop(self) -> None:
        """Subscribe to action lane and route command messages to DeviceManagers."""
        if self._actions_endpoint is None:
            return
        async with self._actions_endpoint.subscribe() as stream:
            async for event in stream:
                try:
                    if not isinstance(event, DeckrMessage):
                        continue
                    if not action_message_for_controller(event, self._controller_id):
                        continue
                    if event.message_type in COMMAND_MESSAGE_TYPES:
                        await self._handle_action_command(event)
                except Exception:
                    if isinstance(event, DeckrMessage):
                        logger.exception(
                            "Error handling action message %s from %s",
                            event.message_type,
                            event.sender,
                        )
                    else:
                        logger.exception("Error handling action lane event")

    async def _hardware_input_loop(self) -> None:
        async with self._hardware_endpoint.subscribe() as subscribe:
            async for message in subscribe:
                event = hw_messages.hardware_body_from_message(message)
                ref = hw_messages.hardware_device_ref_from_message(message)
                if ref is None:
                    continue
                live = self._device_registry.get_by_ref(ref)
                if isinstance(event, hw_messages.DeviceAvailableMessage):
                    await self._reconcile_hardware_current_state(
                        reason="deviceAvailable message"
                    )
                    continue
                if isinstance(event, hw_messages.DeviceDescriptorChangedMessage):
                    await self._reconcile_hardware_current_state(
                        reason="deviceDescriptorChanged message"
                    )
                    continue
                if isinstance(event, hw_messages.DeviceUnavailableMessage):
                    await self._reconcile_hardware_current_state(
                        reason="deviceUnavailable message"
                    )
                    continue
                if live is None:
                    continue
                ctrl_ctx = await self._controller_contexts.get(live.config_id)
                if ctrl_ctx is None:
                    continue
                if isinstance(event, hw_messages.ControlInputMessage):
                    await ctrl_ctx.on_event(message)
                elif isinstance(event, hw_messages.CapabilityStateChangedMessage):
                    await ctrl_ctx.on_capability_state_changed(event)
                elif isinstance(event, hw_messages.CommandRejectedMessage):
                    await ctrl_ctx.on_command_rejected(event)

    async def _hardware_beacon_loop(self) -> None:
        while True:
            try:
                async with self._beacon.watch_feature(HARDWARE_FEATURE_ID) as stream:
                    async for event in stream:
                        await self._reconcile_hardware_current_state(
                            reason=(
                                f"hardware Beacon {event.event_type.value} {event.key}"
                            )
                        )
            except StateUnavailable:
                logger.warning("Hardware Beacon advertisements unavailable; retrying")
                await anyio.sleep(_WATCH_RETRY_SECONDS)

    async def _hardware_reconciliation_loop(self) -> None:
        while True:
            try:
                await self._reconcile_hardware_current_state(reason="broker snapshot")
            except StateUnavailable:
                logger.warning(
                    "Hardware current state unavailable; reconciliation will retry",
                    exc_info=True,
                )
            await anyio.sleep(_STATE_RECONCILE_SECONDS)

    async def _reconcile_hardware_current_state(self, *, reason: str) -> None:
        async with self._hardware_reconcile_lock:
            await self._reconcile_hardware_current_state_locked(reason=reason)

    async def _reconcile_hardware_current_state_locked(self, *, reason: str) -> None:
        logger.debug("Reconciling hardware current state via %s", reason)
        candidates = await self._hardware_candidates_from_beacon()
        self._hardware_candidates = candidates

        for owned in tuple(self._owned_claims.values()):
            candidate = candidates.get(_ref_key(owned.ref))
            if (
                candidate is None
                or candidate.advertisement_id != owned.manager_advertisement_id
                or candidate.payload.session_id != owned.manager_session_id
            ):
                await self._revoke_owned_claim(
                    owned,
                    cancel_contract=True,
                    reason=f"hardware Beacon candidate changed during {reason}",
                )
                continue

            validity = await self._hardware_claim_validity(owned, candidate)
            if validity.status == ContractValidityStatus.VALID:
                if not owned.live:
                    await self._connect_owned_claim(owned, candidate)
                else:
                    await self._refresh_live_descriptor(owned, candidate)
                continue

            if validity.status == ContractValidityStatus.MISSING_TOKEN and not owned.live:
                continue

            await self._revoke_owned_claim(
                owned,
                cancel_contract=True,
                reason=f"hardware claim invalid during {reason}: {validity.status}",
            )

        for candidate in candidates.values():
            key = _ref_key(candidate.ref)
            if key in self._owned_claims:
                continue
            if self._device_registry.get_by_ref(candidate.ref) is not None:
                continue
            await self._try_claim_hardware_candidate(candidate)

        for key in tuple(self._unmatched_hardware_signatures):
            if key not in candidates:
                self._unmatched_hardware_signatures.pop(key, None)

    async def _hardware_candidates_from_beacon(
        self,
    ) -> dict[tuple[str, str], HardwareCandidate]:
        candidates: dict[tuple[str, str], HardwareCandidate] = {}
        for candidate in await self._beacon.find(HARDWARE_FEATURE_ID):
            payload = _valid_hardware_payload(candidate)
            if payload is None:
                continue
            for device_id, item in payload.devices.items():
                if item.device_ref.device_id != device_id:
                    continue
                hardware_candidate = HardwareCandidate(
                    advertisement_key=candidate.key,
                    advertisement_id=candidate.advertisement.advertisement_id,
                    payload=payload,
                    ref=item.device_ref,
                    device=item.descriptor,
                    labels=payload.labels,
                )
                candidates[_ref_key(item.device_ref)] = hardware_candidate
        return candidates

    async def _try_claim_hardware_candidate(
        self,
        candidate: HardwareCandidate,
    ) -> None:
        device = candidate.device
        labels = candidate.labels
        key = _ref_key(candidate.ref)
        unmatched_signature = _unmatched_hardware_signature(device, labels)
        try:
            config = await self._config_service.match_device(
                fingerprint=device.fingerprint,
                labels=labels,
            )
        except ValueError:
            logger.exception(
                "Ambiguous config for hardware fingerprint=%s labels=%s manager=%s",
                device.fingerprint,
                dict(labels),
                candidate.ref.manager_id,
            )
            return
        if config is None:
            if self._unmatched_hardware_signatures.get(key) != unmatched_signature:
                logger.info(
                    "No controller config matched hardware fingerprint=%s "
                    "labels=%s manager=%s",
                    device.fingerprint,
                    dict(labels),
                    candidate.ref.manager_id,
                )
                self._unmatched_hardware_signatures[key] = unmatched_signature
            return
        self._unmatched_hardware_signatures.pop(key, None)

        claim_id = str(uuid4())
        terms = HardwareClaimTerms(
            claimId=claim_id,
            controllerEndpoint=controller_address(self._controller_id),
            managerEndpoint=candidate.payload.manager_endpoint,
            managerAdvertisementId=candidate.advertisement_id,
            devices=(
                HardwareClaimDevice(
                    deviceRef=candidate.ref,
                    instanceCount=1,
                ),
            ),
        )
        try:
            contract = await self._concord.create_contract(
                (
                    controller_address(self._controller_id),
                    candidate.payload.manager_endpoint,
                ),
                contract_id=claim_id,
                profile=HARDWARE_CLAIM_PROFILE_ID,
                terms=terms,
                created_by=controller_address(self._controller_id),
                log_label="ControllerHardware",
            )
            lease = self._concord.participant_lease(
                contract=contract,
                participant=controller_address(self._controller_id),
                session_id=self._session_id,
                refresh_interval=CLAIM_HEARTBEAT_SECONDS,
                log_label="ControllerHardware",
            )
            if self._start_soon is not None:
                lease.start_soon(self._start_soon)
            token = await lease.attach_or_refresh()
        except StateConflict:
            logger.info("Hardware claim contract already exists for %s", claim_id)
            return
        except StateUnavailable:
            logger.warning("Could not create hardware claim for %s", candidate.ref)
            return

        owned = OwnedHardwareClaim(
            claim_id=claim_id,
            config_id=config.id,
            ref=candidate.ref,
            device=device,
            contract=contract,
            controller_token=token,
            controller_lease=lease,
            manager_session_id=candidate.payload.session_id,
            manager_advertisement_id=candidate.advertisement_id,
        )
        self._owned_claims[key] = owned
        validity = await self._hardware_claim_validity(owned, candidate)
        if validity.status == ContractValidityStatus.VALID:
            await self._connect_owned_claim(owned, candidate)

    async def _hardware_claim_validity(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate,
    ) -> ContractValidity:
        return await self._concord.validate(
            owned.contract,
            current_sessions={
                str(controller_address(self._controller_id)): self._session_id,
                str(candidate.payload.manager_endpoint): candidate.payload.session_id,
            },
            log_label="ControllerHardware",
        )

    async def _connect_owned_claim(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate,
    ) -> None:
        live = self._device_registry.connect(
            config_id=owned.config_id,
            ref=owned.ref,
            device=candidate.device,
        )
        owned.device = candidate.device
        owned.live = True
        self._command_service.register_device(
            config_id=owned.config_id,
            ref=owned.ref,
            device=candidate.device,
        )
        config = await self._config_service.get_config(owned.config_id)
        if config is None:
            await self._revoke_owned_claim(
                owned,
                cancel_contract=True,
                reason="matched config disappeared before hardware claim became valid",
            )
            return
        await self.on_device_connected(live, initial_config=config)

    async def _refresh_live_descriptor(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate,
    ) -> None:
        live = self._device_registry.get_by_ref(owned.ref)
        if live is None:
            return
        if candidate.device == live.device:
            return
        updated = self._device_registry.update_descriptor(
            ref=owned.ref,
            device=candidate.device,
        )
        if updated is None:
            return
        owned.device = candidate.device
        self._command_service.register_device(
            config_id=updated.config_id,
            ref=updated.ref,
            device=updated.device,
        )
        ctrl_ctx = await self._controller_contexts.get(updated.config_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.on_descriptor_changed(updated.device)

    async def _revoke_owned_claim(
        self,
        owned: OwnedHardwareClaim,
        *,
        cancel_contract: bool,
        reason: str,
    ) -> None:
        self._owned_claims.pop(_ref_key(owned.ref), None)
        live = self._device_registry.get_by_ref(owned.ref)
        if live is not None:
            self._device_registry.disconnect_config(live.config_id)
            self._command_service.unregister_config(live.config_id)
            owned.live = False
            await self.on_device_disconnected(live.config_id, reason=reason)
        if cancel_contract:
            await self._cancel_hardware_claim(owned, reason=reason)
        await owned.controller_lease.aclose()

    async def _cancel_hardware_claim(
        self,
        owned: OwnedHardwareClaim,
        *,
        reason: str,
    ) -> None:
        with anyio.CancelScope(shield=True):
            try:
                await self._concord.cancel(
                    owned.contract,
                    controller_address(self._controller_id),
                    reason=reason,
                    log_label="ControllerHardware",
                )
            except (StateConflict, StateUnavailable, ValueError):
                logger.info("Could not cancel hardware claim %s", owned.claim_id)

    async def _disconnect_live(
        self,
        live: LiveDeviceRoute,
        *,
        release_claim: bool,
        reason: str,
    ) -> None:
        owned = self._owned_claims.get(_ref_key(live.ref))
        if release_claim and owned is not None:
            await self._revoke_owned_claim(
                owned,
                cancel_contract=True,
                reason=reason,
            )
            return
        self._device_registry.disconnect_config(live.config_id)
        self._command_service.unregister_config(live.config_id)
        if owned is not None:
            owned.live = False
        await self.on_device_disconnected(live.config_id, reason=reason)

    async def _handle_device_config_removed(
        self,
        config_id: str,
        ref: DeviceRef,
    ) -> None:
        live = self._device_registry.get(config_id)
        if live is None or live.ref != ref:
            return
        await self._disconnect_live(
            live,
            release_claim=True,
            reason="config removed",
        )

    def _hardware_claim_id_for_ref(self, ref: DeviceRef) -> str:
        owned = self._owned_claims.get(_ref_key(ref))
        if owned is None:
            return "missing-hardware-claim"
        return owned.claim_id

    async def start(self, ctx: RunContext):
        self._stopping = ctx.stopping
        self._start_soon = ctx.tg.start_soon
        if self._render_backend is None:
            self._render_backend = ProcessPoolRenderBackend()
        if self._actions_endpoint is not None:
            ctx.tg.start_soon(self._actions_subscription_loop)
        ctx.tg.start_soon(self._hardware_input_loop)
        ctx.tg.start_soon(self._hardware_beacon_loop)
        ctx.tg.start_soon(self._hardware_reconciliation_loop)

    async def stop(self):
        if self._stopping is not None:
            self._stopping.set()
        for live in self._device_registry.all():
            await self._disconnect_live(
                live,
                release_claim=True,
                reason="controller stop",
            )
        for ctrl_ctx in await self._controller_contexts.values():
            await ctrl_ctx.clear_page(clear_outputs=False)
        await self._controller_contexts.clear()
        await self._release_owned_claims()
        if self._render_backend is not None:
            await self._render_backend.aclose()

    async def _release_owned_claims(self) -> None:
        for owned in tuple(self._owned_claims.values()):
            await self._revoke_owned_claim(
                owned,
                cancel_contract=True,
                reason="controller stop",
            )

    async def _device_lifecycle(
        self,
        live: LiveDeviceRoute,
        initial_config,
    ) -> None:
        """Run device setup, config listener, and wait for disconnect."""
        stream = self._config_service.subscribe(live.config_id)
        disconnect_event = anyio.Event()
        self._device_disconnect_events[live.config_id] = disconnect_event
        try:
            try:
                first = await anext(stream)
            except StopAsyncIteration:
                first = initial_config
            if first is None:
                logger.error("Config not found for %s", live.config_id)
                await self._handle_device_config_removed(live.config_id, live.ref)
                return
            if (
                disconnect_event.is_set()
                or self._device_registry.get(live.config_id) is not live
            ):
                return

            async def handle_config_removed(config_id: str) -> None:
                await self._handle_device_config_removed(config_id, live.ref)

            ctrl_ctx = DeviceManager(
                controller_id=self._controller_id,
                device=live.device,
                hardware_ref=live.ref,
                command_service=self._command_service,
                config=first,
                manager=self._action_registry,
                actions_bus=self._actions_endpoint,
                start_soon=self._start_soon,
                render_backend=self._render_backend,
                settings_service=self._settings_service,
                config_stream=stream,
                on_config_removed=handle_config_removed,
                binding_concord=self._concord,
                hardware_claim_id=self._hardware_claim_id_for_ref(live.ref),
            )
            await self._controller_contexts.set(live.config_id, ctrl_ctx)
            async with anyio.create_task_group() as device_tg:
                page_ready = anyio.Event()

                async def set_initial_page() -> None:
                    try:
                        await ctrl_ctx.set_page()
                    except Exception:
                        if disconnect_event.is_set():
                            return
                        raise
                    finally:
                        page_ready.set()

                device_tg.start_soon(set_initial_page)
                while not page_ready.is_set():
                    if disconnect_event.is_set():
                        device_tg.cancel_scope.cancel()
                        return
                    await anyio.sleep(0.01)
                if disconnect_event.is_set():
                    device_tg.cancel_scope.cancel()
                    return
                device_tg.start_soon(ctrl_ctx._config_listener)
                await disconnect_event.wait()
                device_tg.cancel_scope.cancel()
        finally:
            if self._device_disconnect_events.get(live.config_id) is disconnect_event:
                self._device_disconnect_events.pop(live.config_id, None)

    async def on_device_connected(
        self,
        live: LiveDeviceRoute,
        *,
        initial_config,
    ):
        if await self._controller_contexts.get(live.config_id) is not None:
            await self.on_device_disconnected(
                live.config_id,
                reason="replacement live device connected",
            )
        logger.info(
            "Starting controller service for config %s from %s/%s",
            live.config_id,
            live.ref.manager_id,
            live.ref.device_id,
        )
        self._start_soon(self._device_lifecycle, live, initial_config)

    async def on_device_disconnected(self, config_id: str, *, reason: str = "unknown"):
        ctrl_ctx = await self._controller_contexts.pop(config_id)
        disconnect_ev = self._device_disconnect_events.get(config_id)
        if disconnect_ev is not None:
            disconnect_ev.set()
        try:
            if ctrl_ctx is not None:
                await ctrl_ctx.clear_page(clear_outputs=False)
        finally:
            logger.info(
                "Stopped controller service for config %s (reason: %s)",
                config_id,
                reason,
            )


def _valid_hardware_payload(candidate: Candidate) -> HardwareBeaconPayload | None:
    try:
        return hardware_payload_from_advertisement(candidate.advertisement)
    except ValueError:
        logger.warning(
            "Ignoring invalid hardware Beacon advertisement %s",
            candidate.key,
        )
        return None


def _ref_key(ref: DeviceRef) -> tuple[str, str]:
    return ref.manager_id, ref.device_id


def _unmatched_hardware_signature(
    device: DeviceDescriptor,
    labels: Mapping[str, str],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    return device.fingerprint, tuple(sorted(labels.items()))
