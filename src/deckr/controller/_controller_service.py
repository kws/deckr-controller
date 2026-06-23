import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import uuid4

import anyio
from deckr.actions.messages import (
    ACTION_AVAILABILITY_CHANGED,
    ACTION_AVAILABILITY_SNAPSHOT,
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
from deckr.beacon import Beacon, Candidate
from deckr.components import BaseComponent, RunContext
from deckr.concord import (
    DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
    Concord,
    ConcordAgreementLease,
    ConcordAgreementSpec,
    ConcordConflict,
    ConcordUnavailable,
    ContractHandle,
    ContractValidity,
    ContractValidityStatus,
    ParticipantHandle,
)
from deckr.contracts.messages import (
    ACTIONS_LANE,
    HARDWARE_MESSAGES_LANE,
    DeckrMessage,
    EndpointAddress,
    controller_address,
    hardware_manager_address,
)
from deckr.core.util.anyio import AsyncMap, CoalescedTrigger
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
from deckr.lanes import EndpointSession
from deckr.substrates.nats_kv import KvUnavailable

from deckr.controller._action_availability import ActionAvailabilityService
from deckr.controller._action_provider_sessions import ActionProviderSessionManager
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
from deckr.controller._stop_aware import cancel_on_stopping, sleep_until_stopping
from deckr.controller.action_provider.action_registry import ActionRegistry
from deckr.controller.action_provider.events import ActionCatalogChangedEvent
from deckr.controller.config import DeviceConfigService
from deckr.controller.settings import SettingsService

logger = logging.getLogger(__name__)

CLAIM_HEARTBEAT_SECONDS = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS
_STATE_RECONCILE_SECONDS = 15.0
_STATE_NOTIFICATION_BATCH_SECONDS = 0.05
_WATCH_RETRY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class HardwareCandidate:
    advertisement_key: str
    advertisement_id: str
    advertisement_endpoint: EndpointAddress
    advertisement_session_id: str
    advertisement_revision: int
    advertisement_refresh_seq: int
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
    agreement: ConcordAgreementLease
    current_sessions: dict[str, str]
    controller_token: ParticipantHandle | None = None
    live: bool = False

    @property
    def contract(self) -> ContractHandle:
        return self.agreement.contract


class ControllerService(BaseComponent):
    def __init__(
        self,
        endpoint: EndpointSession,
        beacon: Beacon,
        concord: Concord,
        config_service: DeviceConfigService,
        settings_service: SettingsService,
        *,
        controller_id: str,
        action_registry: ActionRegistry | None = None,
        action_availability_service: ActionAvailabilityService | None = None,
        render_backend: RenderBackend | None = None,
    ):
        super().__init__()
        self._endpoint = endpoint
        self._beacon = beacon
        self._concord = concord
        self._device_registry = DeviceRouteRegistry()
        self._config_service = config_service
        self._settings_service = settings_service
        self._controller_id = controller_id
        self._command_service = HardwareCommandService(
            endpoint,
            controller_id=controller_id,
        )
        self._controller_contexts = AsyncMap[str, DeviceManager]()
        self._device_disconnect_events: dict[str, anyio.Event] = {}
        self._action_registry = action_registry
        self._action_availability_service = action_availability_service
        self._start_soon: Callable | None = None
        self._render_backend = render_backend
        self._session_id = endpoint.session_id
        self._owned_claims: dict[tuple[str, str], OwnedHardwareClaim] = {}
        self._unmatched_hardware_signatures: dict[
            tuple[str, str],
            tuple[str, tuple[tuple[str, str], ...]],
        ] = {}
        self._hardware_candidates: dict[tuple[str, str], HardwareCandidate] = {}
        self._hardware_reconcile_lock = anyio.Lock()
        self._hardware_reconcile_notifications = CoalescedTrigger(
            batch_interval=_STATE_NOTIFICATION_BATCH_SECONDS
        )
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

    async def handle_action_catalog_changed_event(
        self,
        event: ActionCatalogChangedEvent,
    ) -> None:
        changed_keys = frozenset()
        if self._action_availability_service is not None:
            changed_keys = await self._action_availability_service.ingest_catalog_changed(
                event
            )
        controller_contexts = await self._controller_contexts.values()
        logger.debug(
            "Action catalog changed handoff changed_keys=%s devices=%s",
            len(changed_keys),
            len(controller_contexts),
        )
        logger.log(
            logging.INFO if controller_contexts else logging.DEBUG,
            "Applying ActionCatalogChangedEvent to %d device(s): +%s -%s ~%s successor=%s",
            len(controller_contexts),
            event.catalog_added,
            event.catalog_removed,
            event.catalog_updated,
            event.provider_session_successions,
        )
        for ctrl_ctx in controller_contexts:
            await ctrl_ctx.on_action_availability_changed(changed_keys)

    async def _handle_action_availability_message(self, msg: DeckrMessage) -> None:
        if self._action_availability_service is None:
            return
        changed_keys = await self._action_availability_service.handle_availability_message(
            msg
        )
        if not changed_keys:
            logger.debug(
                "Action availability handoff skipped type=%s changed_keys=0",
                msg.message_type,
            )
            return
        controller_contexts = await self._controller_contexts.values()
        logger.debug(
            "Action availability handoff type=%s changed_keys=%s devices=%s",
            msg.message_type,
            len(changed_keys),
            len(controller_contexts),
        )
        for ctrl_ctx in controller_contexts:
            await ctrl_ctx.on_action_availability_changed(changed_keys)

    async def _actions_subscription_loop(self, stopping: anyio.Event) -> None:
        """Subscribe to action lane and route command messages to DeviceManagers."""
        async with (
            self._endpoint.subscribe(ACTIONS_LANE) as stream,
            cancel_on_stopping(stopping),
        ):
            async for event in stream:
                try:
                    if not isinstance(event, DeckrMessage):
                        continue
                    if not action_message_for_controller(event, self._controller_id):
                        continue
                    if event.message_type in {
                        ACTION_AVAILABILITY_SNAPSHOT,
                        ACTION_AVAILABILITY_CHANGED,
                    }:
                        await self._handle_action_availability_message(event)
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

    async def _hardware_input_loop(self, stopping: anyio.Event) -> None:
        async with (
            self._endpoint.subscribe(HARDWARE_MESSAGES_LANE) as subscribe,
            cancel_on_stopping(stopping),
        ):
            async for message in subscribe:
                event = hw_messages.hardware_body_from_message(message)
                ref = hw_messages.hardware_device_ref_from_message(message)
                if ref is None:
                    continue
                live = self._device_registry.get_by_ref(ref)
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

    async def _hardware_beacon_loop(self, stopping: anyio.Event) -> None:
        while not stopping.is_set():
            try:
                async with (
                    self._beacon.watch(HARDWARE_FEATURE_ID) as stream,
                    cancel_on_stopping(stopping),
                ):
                    async for event in stream:
                        await self._hardware_reconcile_notifications.request(
                            f"hardware Beacon {event.event_type.value} {event.key}"
                        )
            except KvUnavailable:
                logger.warning("Hardware Beacon advertisements unavailable; retrying")
                await sleep_until_stopping(stopping, _WATCH_RETRY_SECONDS)

    async def _hardware_reconciliation_loop(self, stopping: anyio.Event) -> None:
        while not stopping.is_set():
            try:
                await self._reconcile_hardware_current_state(reason="broker snapshot")
            except (ConcordUnavailable, KvUnavailable):
                logger.warning(
                    "Hardware current state unavailable; reconciliation will retry",
                    exc_info=True,
                )
            await sleep_until_stopping(stopping, _STATE_RECONCILE_SECONDS)

    async def _hardware_claim_event_loop(self, stopping: anyio.Event) -> None:
        while not stopping.is_set():
            try:
                async with (
                    self._concord.watch(HARDWARE_CLAIM_PROFILE_ID) as stream,
                    cancel_on_stopping(stopping),
                ):
                    async for event in stream:
                        await self._hardware_reconcile_notifications.request(
                            f"hardware claim {event.event_type.value}"
                        )
            except ConcordUnavailable:
                await sleep_until_stopping(stopping, _WATCH_RETRY_SECONDS)

    async def _hardware_notification_reconciliation_loop(
        self,
        stopping: anyio.Event,
    ) -> None:
        async def close_on_stopping() -> None:
            await stopping.wait()
            await self._hardware_reconcile_notifications.aclose()

        async with anyio.create_task_group() as tg:
            tg.start_soon(close_on_stopping)
            try:
                await self._hardware_reconcile_notifications.run(
                    self._reconcile_hardware_notification,
                    reason_prefix="hardware notifications",
                )
            finally:
                tg.cancel_scope.cancel()

    async def _reconcile_hardware_notification(self, reason: str) -> None:
        try:
            await self._reconcile_hardware_current_state(reason=reason)
        except (ConcordUnavailable, KvUnavailable):
            logger.warning(
                "Hardware current state unavailable; notification will retry",
                exc_info=True,
            )

    async def _reconcile_hardware_current_state(self, *, reason: str) -> None:
        async with self._hardware_reconcile_lock:
            await self._reconcile_hardware_current_state_locked(reason=reason)

    async def _reconcile_hardware_current_state_locked(self, *, reason: str) -> None:
        logger.debug("Reconciling hardware current state via %s", reason)
        candidates = await self._hardware_candidates_from_beacon()
        self._hardware_candidates = candidates

        for owned in tuple(self._owned_claims.values()):
            candidate = candidates.get(_ref_key(owned.ref))
            if candidate is not None and _manager_session_changed(owned, candidate):
                validity = await self._hardware_claim_contract_validity(
                    owned,
                    current_sessions=_hardware_claim_current_sessions(
                        self._controller_id,
                        self._session_id,
                        candidate,
                    ),
                )
                owned.controller_token = owned.agreement.local_token
                if validity.status == ContractValidityStatus.UNAVAILABLE:
                    continue
                await self._revoke_owned_claim(
                    owned,
                    cancel_contract=True,
                    reason=(
                        f"hardware manager session changed during {reason}: "
                        f"{candidate.payload.session_id}"
                    ),
                )
                continue

            validity = await self._hardware_claim_contract_validity(owned)
            owned.controller_token = owned.agreement.local_token

            if candidate is None:
                if validity.status == ContractValidityStatus.UNAVAILABLE:
                    continue
                if validity.status == ContractValidityStatus.VALID:
                    if not owned.live:
                        await self._connect_owned_claim(owned, None)
                    continue
                if self._pending_hardware_claim_validity(owned, validity):
                    continue
                await self._revoke_owned_claim(
                    owned,
                    cancel_contract=True,
                    reason=f"hardware claim invalid during {reason}: {validity.status}",
                )
                continue

            if validity.status == ContractValidityStatus.VALID:
                if await self._rematch_owned_claim_if_needed(owned, candidate):
                    continue
                if not owned.live:
                    await self._connect_owned_claim(owned, candidate)
                else:
                    await self._refresh_live_descriptor(owned, candidate)
                continue

            if owned.live:
                if validity.status == ContractValidityStatus.UNAVAILABLE:
                    continue

            if not owned.live and self._pending_hardware_claim_validity(owned, validity):
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
        try:
            beacon_candidates = self._beacon.candidates(HARDWARE_FEATURE_ID)
        except KvUnavailable:
            beacon_candidates = await self._beacon.candidates_exact(HARDWARE_FEATURE_ID)
        candidates: dict[tuple[str, str], HardwareCandidate] = {}
        for candidate in beacon_candidates:
            payload = _valid_hardware_payload(candidate)
            if payload is None:
                continue
            for device_id, item in payload.devices.items():
                if item.device_ref.device_id != device_id:
                    continue
                hardware_candidate = HardwareCandidate(
                    advertisement_key=candidate.key,
                    advertisement_id=candidate.advertisement.advertisement_id,
                    advertisement_endpoint=candidate.advertisement.endpoint,
                    advertisement_session_id=candidate.advertisement.session_id,
                    advertisement_revision=candidate.revision,
                    advertisement_refresh_seq=candidate.advertisement.refresh_seq,
                    payload=payload,
                    ref=item.device_ref,
                    device=item.descriptor,
                    labels=payload.labels,
                )
                key = _ref_key(item.device_ref)
                selected = candidates.get(key)
                if selected is not None:
                    _log_duplicate_hardware_candidate(
                        item.device_ref,
                        selected=selected,
                        skipped=hardware_candidate,
                    )
                    continue
                candidates[key] = hardware_candidate
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
            devices=(
                HardwareClaimDevice(
                    deviceRef=candidate.ref,
                    instanceCount=1,
                ),
            ),
        )
        current_sessions = _hardware_claim_current_sessions(
            self._controller_id,
            self._session_id,
            candidate,
        )
        try:
            agreement = await self._concord.propose(
                ConcordAgreementSpec(
                    profile=HARDWARE_CLAIM_PROFILE_ID,
                    participants=(
                        controller_address(self._controller_id),
                        candidate.payload.manager_endpoint,
                    ),
                    local_participant=controller_address(self._controller_id),
                    local_session_id=self._session_id,
                    terms=terms,
                    current_sessions=current_sessions,
                    refresh_interval=CLAIM_HEARTBEAT_SECONDS,
                    log_label="ControllerHardware",
                ),
                start_soon=self._start_soon,
            )
        except (ConcordConflict, ConcordUnavailable):
            logger.warning("Could not create hardware claim for %s", candidate.ref)
            return

        owned = OwnedHardwareClaim(
            claim_id=claim_id,
            config_id=config.id,
            ref=candidate.ref,
            device=device,
            agreement=agreement,
            current_sessions=current_sessions,
        )
        self._owned_claims[key] = owned
        validity = await self._hardware_claim_contract_validity(owned)
        owned.controller_token = owned.agreement.local_token
        if validity.status == ContractValidityStatus.VALID:
            await self._connect_owned_claim(owned, candidate)

    async def _hardware_claim_contract_validity(
        self,
        owned: OwnedHardwareClaim,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        try:
            await owned.agreement.refresh()
        except ConcordConflict:
            pass
        sessions = current_sessions or owned.current_sessions
        try:
            validity = await self._concord.validate_exact(
                owned.contract,
                current_sessions=sessions,
            )
        except ConcordUnavailable:
            return ContractValidity(ContractValidityStatus.UNAVAILABLE)
        owned.agreement._validity = validity  # noqa: SLF001
        return validity

    async def _connect_owned_claim(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate | None,
    ) -> None:
        config = await self._config_service.get_config(owned.config_id)
        if config is None or not config.enabled:
            logger.info(
                "Config %s is unavailable; keeping hardware claim %s idle",
                owned.config_id,
                owned.claim_id,
            )
            return
        device = candidate.device if candidate is not None else owned.device
        manager_session_id = (
            candidate.payload.session_id
            if candidate is not None
            else _owned_manager_session_id(owned)
        )
        if manager_session_id is None:
            logger.warning(
                "Claim %s is valid but manager session is unknown; keeping idle",
                owned.claim_id,
            )
            return
        live = self._device_registry.connect(
            config_id=owned.config_id,
            ref=owned.ref,
            device=device,
            manager_session_id=manager_session_id,
        )
        owned.device = device
        owned.live = True
        self._command_service.register_device(
            config_id=owned.config_id,
            ref=owned.ref,
            device=device,
            manager_session_id=manager_session_id,
        )
        await self.on_device_connected(live, initial_config=config)

    def _pending_hardware_claim_validity(
        self,
        owned: OwnedHardwareClaim,
        validity: ContractValidity,
    ) -> bool:
        return (
            validity.status == ContractValidityStatus.NOT_YET_FULFILLED
            and str(controller_address(self._controller_id)) in validity.tokens
        )

    async def _rematch_owned_claim_if_needed(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate,
    ) -> bool:
        current_config = await self._config_service.get_config(owned.config_id)
        if current_config is not None and current_config.enabled:
            return False
        try:
            matched = await self._config_service.match_device(
                fingerprint=candidate.device.fingerprint,
                labels=candidate.labels,
            )
        except ValueError:
            logger.exception(
                "Ambiguous rematch config for claimed hardware fingerprint=%s "
                "labels=%s manager=%s",
                candidate.device.fingerprint,
                dict(candidate.labels),
                candidate.ref.manager_id,
            )
            return False
        if matched is None:
            return False
        if matched.id == owned.config_id:
            return False
        await self._migrate_owned_claim_config(
            owned,
            candidate,
            next_config=matched,
        )
        return True

    async def _migrate_owned_claim_config(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate,
        *,
        next_config,
    ) -> None:
        previous_config_id = owned.config_id
        logger.info(
            "Rematching claimed hardware %s/%s from config %s to %s",
            owned.ref.manager_id,
            owned.ref.device_id,
            previous_config_id,
            next_config.id,
        )
        if self._device_registry.get(previous_config_id) is not None:
            self._device_registry.disconnect_config(previous_config_id)
            self._command_service.unregister_config(previous_config_id)
        await self.on_device_disconnected(
            previous_config_id,
            reason=f"hardware rematched to config {next_config.id}",
        )
        owned.config_id = next_config.id
        owned.device = candidate.device
        live = self._device_registry.connect(
            config_id=next_config.id,
            ref=owned.ref,
            device=candidate.device,
            manager_session_id=candidate.payload.session_id,
        )
        owned.live = True
        self._command_service.register_device(
            config_id=next_config.id,
            ref=owned.ref,
            device=candidate.device,
            manager_session_id=candidate.payload.session_id,
        )
        await self.on_device_connected(live, initial_config=next_config)

    async def _refresh_live_descriptor(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate,
    ) -> None:
        live = self._device_registry.get_by_ref(owned.ref)
        if live is None:
            return
        if (
            candidate.device == live.device
            and candidate.payload.session_id == live.manager_session_id
        ):
            return
        updated = self._device_registry.update_descriptor(
            ref=owned.ref,
            device=candidate.device,
            manager_session_id=candidate.payload.session_id,
        )
        if updated is None:
            return
        owned.device = candidate.device
        self._command_service.register_device(
            config_id=updated.config_id,
            ref=updated.ref,
            device=updated.device,
            manager_session_id=updated.manager_session_id,
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
        else:
            await owned.agreement.aclose()

    async def _cancel_hardware_claim(
        self,
        owned: OwnedHardwareClaim,
        *,
        reason: str,
    ) -> None:
        with anyio.CancelScope(shield=True):
            try:
                await owned.agreement.cancel(reason=reason)
            except (ConcordConflict, ConcordUnavailable, ValueError):
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
        logger.info(
            "Config %s removed; preserving live hardware claim for %s/%s",
            config_id,
            ref.manager_id,
            ref.device_id,
        )

    async def start(self, ctx: RunContext):
        self._stopping = ctx.stopping
        self._start_soon = ctx.tg.start_soon
        if (
            self._action_availability_service is None
            and self._action_registry is not None
        ):
            self._action_availability_service = ActionAvailabilityService(
                controller_id=self._controller_id,
                controller_session_id=self._session_id,
                actions_bus=self._endpoint,
                manager=self._action_registry,
                start_soon=ctx.tg.start_soon,
                provider_sessions=ActionProviderSessionManager(
                    controller_id=self._controller_id,
                    controller_session_id=self._session_id,
                    concord=self._concord,
                    start_soon=ctx.tg.start_soon,
                ),
            )
        if self._action_availability_service is not None:
            await self._action_availability_service.start(ctx.tg, ctx.stopping)
        if self._render_backend is None:
            self._render_backend = ProcessPoolRenderBackend()
        ctx.tg.start_soon(self._actions_subscription_loop, ctx.stopping)
        ctx.tg.start_soon(self._hardware_input_loop, ctx.stopping)
        ctx.tg.start_soon(self._hardware_beacon_loop, ctx.stopping)
        ctx.tg.start_soon(self._hardware_claim_event_loop, ctx.stopping)
        ctx.tg.start_soon(
            self._hardware_notification_reconciliation_loop,
            ctx.stopping,
        )
        ctx.tg.start_soon(self._hardware_reconciliation_loop, ctx.stopping)

    async def stop(self):
        if self._stopping is not None:
            self._stopping.set()
        await self._hardware_reconcile_notifications.aclose()
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
        if self._action_availability_service is not None:
            await self._action_availability_service.aclose()
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
            initial_config_removed = first is None
            if first is None:
                logger.info(
                    "Config %s is currently removed; keeping device claimed but idle",
                    live.config_id,
                )
                first = initial_config
            if (
                disconnect_event.is_set()
                or self._device_registry.get(live.config_id) is not live
            ):
                return

            ctrl_ctx = DeviceManager(
                controller_id=self._controller_id,
                device=live.device,
                hardware_ref=live.ref,
                command_service=self._command_service,
                config=first,
                manager=self._action_registry,
                actions_bus=self._endpoint,
                start_soon=self._start_soon,
                availability_service=self._action_availability_service,
                render_backend=self._render_backend,
                settings_service=self._settings_service,
                config_stream=stream,
            )
            await self._controller_contexts.set(live.config_id, ctrl_ctx)
            async with anyio.create_task_group() as device_tg:
                await ctrl_ctx.start(device_tg, disconnect_event)
                device_tg.start_soon(ctrl_ctx._config_listener)
                if initial_config_removed:
                    await ctrl_ctx._on_config_changed(None)
                    await disconnect_event.wait()
                    device_tg.cancel_scope.cancel()
                    return

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


def _log_duplicate_hardware_candidate(
    ref: DeviceRef,
    *,
    selected: HardwareCandidate,
    skipped: HardwareCandidate,
) -> None:
    logger.info(
        "Multiple hardware Beacon advertisements describe device %s/%s; "
        "selected key=%s endpoint=%s session=%s revision=%s refreshSeq=%s; "
        "skipped key=%s endpoint=%s session=%s revision=%s refreshSeq=%s",
        ref.manager_id,
        ref.device_id,
        selected.advertisement_key,
        selected.advertisement_endpoint,
        selected.advertisement_session_id,
        selected.advertisement_revision,
        selected.advertisement_refresh_seq,
        skipped.advertisement_key,
        skipped.advertisement_endpoint,
        skipped.advertisement_session_id,
        skipped.advertisement_revision,
        skipped.advertisement_refresh_seq,
    )


def _ref_key(ref: DeviceRef) -> tuple[str, str]:
    return ref.manager_id, ref.device_id


def _unmatched_hardware_signature(
    device: DeviceDescriptor,
    labels: Mapping[str, str],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    return device.fingerprint, tuple(sorted(labels.items()))


def _manager_session_changed(
    owned: OwnedHardwareClaim,
    candidate: HardwareCandidate,
) -> bool:
    return (
        owned.current_sessions.get(str(candidate.payload.manager_endpoint))
        != candidate.payload.session_id
    )


def _hardware_claim_current_sessions(
    controller_id: str,
    controller_session_id: str,
    candidate: HardwareCandidate,
) -> dict[str, str]:
    return {
        str(controller_address(controller_id)): controller_session_id,
        str(candidate.payload.manager_endpoint): candidate.payload.session_id,
    }


def _owned_manager_session_id(owned: OwnedHardwareClaim) -> str | None:
    return owned.current_sessions.get(
        str(hardware_manager_address(owned.ref.manager_id))
    )
