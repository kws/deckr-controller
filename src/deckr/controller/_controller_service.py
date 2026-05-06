import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

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
from deckr.components import BaseComponent, RunContext
from deckr.contracts.messages import (
    DeckrMessage,
    controller_address,
    hardware_manager_address,
)
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef
from deckr.lanes import RegisteredEndpointLane
from deckr.state import (
    DEFAULT_STATE_LEASE_TTL_SECONDS,
    DeviceClaim,
    EndpointPresence,
    HardwareInventory,
    StateConflict,
    StateEntry,
    StateStore,
    StateUnavailable,
    device_claim_key,
    encode_key_token,
    hardware_inventory_key,
    observe_prefix_current,
    parse_device_claim_key,
    parse_hardware_inventory_key,
    parse_presence_endpoint_key,
    presence_endpoint_key,
)

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

CLAIM_TTL_SECONDS = DEFAULT_STATE_LEASE_TTL_SECONDS
CLAIM_HEARTBEAT_SECONDS = 5.0
_STATE_RECONCILE_SECONDS = 1.0
_WATCH_RETRY_SECONDS = 1.0
_HARDWARE_INVENTORY_PREFIX = "inventory.hardware."
_DEVICE_CLAIM_PREFIX = "claim.device."
_HARDWARE_MANAGER_PRESENCE_PREFIX = ".".join(
    (
        "presence",
        "endpoint",
        encode_key_token("hardware_messages"),
        encode_key_token("hardware_manager"),
        "",
    )
)


@dataclass(frozen=True, slots=True)
class OwnedDeviceClaim:
    key: str
    config_id: str
    ref: DeviceRef
    revision: int


class ControllerService(BaseComponent):
    def __init__(
        self,
        hardware_endpoint: RegisteredEndpointLane,
        lease_state: StateStore,
        discovery_state: StateStore,
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
        self._lease_state = lease_state
        self._discovery_state = discovery_state
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
        self._owned_claims: dict[str, OwnedDeviceClaim] = {}
        self._blocked_claim_revisions: dict[str, int] = {}
        self._unmatched_inventory_signatures: dict[
            str,
            tuple[str, tuple[tuple[str, str], ...]],
        ] = {}
        self._inventory_by_manager: dict[str, HardwareInventory] = {}
        self._manager_presence_sessions: dict[str, str] = {}
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
                    if live is not None:
                        updated = self._device_registry.update_descriptor(
                            ref=ref,
                            device=event.descriptor,
                        )
                        if updated is not None:
                            self._command_service.register_device(
                                config_id=updated.config_id,
                                ref=updated.ref,
                                device=updated.device,
                            )
                            ctrl_ctx = await self._controller_contexts.get(
                                updated.config_id
                            )
                            if ctrl_ctx is not None:
                                await ctrl_ctx.on_descriptor_changed(updated.device)
                    else:
                        await self._reconcile_hardware_current_state(
                            reason="deviceDescriptorChanged message"
                        )
                    continue
                if isinstance(event, hw_messages.DeviceUnavailableMessage):
                    if live is not None:
                        await self._disconnect_live(
                            live,
                            release_claim=True,
                            reason="deviceUnavailable message",
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

    async def _inventory_loop(self) -> None:
        while True:
            try:
                async with self._discovery_state.watch(
                    _HARDWARE_INVENTORY_PREFIX
                ) as stream:
                    async for change in stream:
                        manager_id = parse_hardware_inventory_key(change.key)
                        if manager_id is None:
                            continue
                        await self._reconcile_hardware_current_state(
                            reason=(
                                f"hardware inventory watch {change.operation} "
                                f"{change.key}"
                            )
                        )
            except StateUnavailable:
                logger.warning("Hardware inventory state unavailable; retrying")
                await anyio.sleep(_WATCH_RETRY_SECONDS)

    async def _try_claim_inventory_device(
        self,
        ref: DeviceRef,
        item,
        labels: Mapping[str, str],
    ) -> None:
        device = self._device_from_inventory(ref, item)
        claim_key = device_claim_key(manager_id=ref.manager_id, device_id=ref.device_id)
        unmatched_signature = _unmatched_inventory_signature(device, labels)
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
                ref.manager_id,
            )
            return
        if config is None:
            if (
                self._unmatched_inventory_signatures.get(claim_key)
                != unmatched_signature
            ):
                logger.info(
                    "No controller config matched hardware fingerprint=%s "
                    "labels=%s manager=%s",
                    device.fingerprint,
                    dict(labels),
                    ref.manager_id,
                )
                self._unmatched_inventory_signatures[claim_key] = unmatched_signature
            return
        self._unmatched_inventory_signatures.pop(claim_key, None)
        try:
            current_claim = await self._lease_state.get(claim_key)
        except StateUnavailable:
            logger.warning("Could not inspect claim %s; retrying later", claim_key)
            return
        if current_claim is not None:
            self._remember_blocked_claim(claim_key, current_claim)
            return
        claim = self._new_claim()
        try:
            entry = await self._lease_state.create(
                claim_key,
                claim,
                ttl=CLAIM_TTL_SECONDS,
            )
        except StateConflict:
            try:
                current_claim = await self._lease_state.get(claim_key)
            except StateUnavailable:
                logger.warning("Could not inspect conflicting claim %s", claim_key)
                return
            if current_claim is not None:
                self._remember_blocked_claim(claim_key, current_claim)
            return
        except StateUnavailable:
            logger.warning("Could not create claim %s; retrying later", claim_key)
            return
        self._blocked_claim_revisions.pop(claim_key, None)
        self._owned_claims[claim_key] = OwnedDeviceClaim(
            key=claim_key,
            config_id=config.id,
            ref=ref,
            revision=entry.revision,
        )
        live = self._device_registry.connect(
            config_id=config.id,
            ref=ref,
            device=device,
        )
        self._command_service.register_device(
            config_id=config.id,
            ref=ref,
            device=device,
        )
        await self.on_device_connected(live, initial_config=config)

    def _device_from_inventory(
        self,
        ref: DeviceRef,
        item,
    ) -> DeviceDescriptor:
        return item.descriptor

    def _new_claim(self) -> DeviceClaim:
        return DeviceClaim(
            claimedByEndpoint=controller_address(self._controller_id),
            claimedBySessionId=self._session_id,
            timestamp=datetime.now(UTC),
            ttlSeconds=CLAIM_TTL_SECONDS,
        )

    def _remember_blocked_claim(self, key: str, entry: StateEntry) -> None:
        if key not in self._blocked_claim_revisions:
            parsed = parse_device_claim_key(key)
            if parsed is not None:
                manager_id, device_id = parsed
                logger.info("Device %s/%s is already claimed", manager_id, device_id)
        self._blocked_claim_revisions[key] = entry.revision

    async def _manager_presence_loop(self) -> None:
        while True:
            try:
                async with self._lease_state.watch(
                    _HARDWARE_MANAGER_PRESENCE_PREFIX
                ) as stream:
                    async for change in stream:
                        parsed = parse_presence_endpoint_key(change.key)
                        if parsed is None:
                            continue
                        lane, endpoint = parsed
                        if (
                            lane != "hardware_messages"
                            or endpoint.family != "hardware_manager"
                        ):
                            continue
                        await self._reconcile_hardware_current_state(
                            reason=(
                                f"manager presence watch {change.operation} "
                                f"{change.key}"
                            )
                        )
            except StateUnavailable:
                logger.warning("Manager presence state unavailable; retrying")
                await anyio.sleep(_WATCH_RETRY_SECONDS)

    async def _claim_watch_loop(self) -> None:
        while True:
            try:
                async with self._lease_state.watch(_DEVICE_CLAIM_PREFIX) as stream:
                    async for change in stream:
                        parsed = parse_device_claim_key(change.key)
                        if parsed is None:
                            continue
                        await self._reconcile_hardware_current_state(
                            reason=f"device claim watch {change.operation} {change.key}"
                        )
            except StateUnavailable:
                logger.warning("Device claim state unavailable; retrying")
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
        presence_observation = await observe_prefix_current(
            self._lease_state,
            _HARDWARE_MANAGER_PRESENCE_PREFIX,
            known_keys=(
                presence_endpoint_key(
                    lane="hardware_messages",
                    endpoint=hardware_manager_address(manager_id),
                )
                for manager_id in self._manager_presence_sessions
            ),
        )
        inventory_observation = await observe_prefix_current(
            self._discovery_state,
            _HARDWARE_INVENTORY_PREFIX,
            known_keys=(
                hardware_inventory_key(manager_id)
                for manager_id in self._inventory_by_manager
            ),
        )
        claim_observation = await observe_prefix_current(
            self._lease_state,
            _DEVICE_CLAIM_PREFIX,
            known_keys=(
                set(self._owned_claims)
                | set(self._blocked_claim_revisions)
                | {
                    device_claim_key(
                        manager_id=live.ref.manager_id,
                        device_id=live.ref.device_id,
                    )
                    for live in self._device_registry.all()
                }
            ),
        )

        next_presence_sessions = dict(self._manager_presence_sessions)
        next_inventory = dict(self._inventory_by_manager)
        current_claims: dict[str, StateEntry] = {}

        for key in presence_observation.confirmed_missing:
            parsed = parse_presence_endpoint_key(key)
            if parsed is None:
                continue
            lane, endpoint = parsed
            if lane == "hardware_messages" and endpoint.family == "hardware_manager":
                next_presence_sessions.pop(endpoint.endpoint_id, None)

        for entry in presence_observation.entries:
            parsed = parse_presence_endpoint_key(entry.key)
            if parsed is None:
                continue
            lane, endpoint = parsed
            if lane != "hardware_messages" or endpoint.family != "hardware_manager":
                continue
            presence = _valid_endpoint_presence(entry)
            if presence is not None:
                next_presence_sessions[endpoint.endpoint_id] = presence.session_id
            else:
                next_presence_sessions.pop(endpoint.endpoint_id, None)

        for key in inventory_observation.confirmed_missing:
            manager_id = parse_hardware_inventory_key(key)
            if manager_id is not None:
                next_inventory.pop(manager_id, None)

        for entry in inventory_observation.entries:
            manager_id = parse_hardware_inventory_key(entry.key)
            if manager_id is None:
                continue
            inventory = _valid_hardware_inventory(entry, manager_id=manager_id)
            if inventory is not None:
                next_inventory[manager_id] = inventory
            else:
                next_inventory.pop(manager_id, None)

        for entry in claim_observation.entries:
            parsed = parse_device_claim_key(entry.key)
            if parsed is None:
                continue
            current_claims[entry.key] = entry

        logger.debug("Reconciling hardware current state via %s", reason)
        self._manager_presence_sessions = next_presence_sessions
        self._inventory_by_manager = next_inventory
        for key in tuple(self._blocked_claim_revisions):
            if key not in current_claims:
                self._blocked_claim_revisions.pop(key, None)

        await self._reconcile_owned_claims(current_claims, reason=reason)
        await self._reconcile_live_hardware(current_claims, reason=reason)
        await self._reconcile_available_inventory(current_claims)

    async def _reconcile_owned_claims(
        self,
        current_claims: Mapping[str, StateEntry],
        *,
        reason: str,
    ) -> None:
        for owned in tuple(self._owned_claims.values()):
            entry = current_claims.get(owned.key)
            if entry is None:
                await self._revoke_owned_claim(
                    owned,
                    release_claim=False,
                    reason=f"owned claim missing during {reason}",
                )
                continue

            claim = _valid_device_claim(entry)
            if claim is None or not self._claim_belongs_to_this_session(claim):
                await self._revoke_owned_claim(
                    owned,
                    release_claim=False,
                    reason=f"owned claim changed during {reason}",
                )
                continue

            self._owned_claims[owned.key] = OwnedDeviceClaim(
                key=owned.key,
                config_id=owned.config_id,
                ref=owned.ref,
                revision=entry.revision,
            )
            if not self._ref_is_available(owned.ref):
                await self._revoke_owned_claim(
                    self._owned_claims[owned.key],
                    release_claim=True,
                    reason=self._ref_unavailable_reason(owned.ref, source=reason),
                )

    async def _reconcile_live_hardware(
        self,
        current_claims: Mapping[str, StateEntry],
        *,
        reason: str,
    ) -> None:
        for live in tuple(self._device_registry.all()):
            if not self._ref_is_available(live.ref):
                await self._disconnect_live(
                    live,
                    release_claim=True,
                    reason=self._ref_unavailable_reason(live.ref, source=reason),
                )
                continue
            claim_key = device_claim_key(
                manager_id=live.ref.manager_id,
                device_id=live.ref.device_id,
            )
            entry = current_claims.get(claim_key)
            claim = _valid_device_claim(entry) if entry is not None else None
            if (
                claim_key not in self._owned_claims
                or claim is None
                or not self._claim_belongs_to_this_session(claim)
            ):
                await self._disconnect_live(
                    live,
                    release_claim=False,
                    reason=f"live claim changed during {reason}",
                )
                continue
            await self._refresh_live_descriptor(live)

    async def _reconcile_available_inventory(
        self,
        current_claims: Mapping[str, StateEntry],
    ) -> None:
        available_claim_keys: set[str] = set()
        for inventory in self._inventory_by_manager.values():
            if not self._inventory_is_usable(inventory):
                continue
            for device_id, item in inventory.devices.items():
                ref = item.device_ref
                if ref.device_id != device_id:
                    continue
                claim_key = device_claim_key(
                    manager_id=ref.manager_id,
                    device_id=ref.device_id,
                )
                available_claim_keys.add(claim_key)
                if self._device_registry.get_by_ref(ref) is not None:
                    self._unmatched_inventory_signatures.pop(claim_key, None)
                    continue
                claim_entry = current_claims.get(claim_key)
                if claim_entry is not None:
                    self._unmatched_inventory_signatures.pop(claim_key, None)
                    claim = _valid_device_claim(claim_entry)
                    if claim is None or not self._claim_belongs_to_this_session(claim):
                        self._blocked_claim_revisions[claim_key] = claim_entry.revision
                    continue
                self._blocked_claim_revisions.pop(claim_key, None)
                await self._try_claim_inventory_device(ref, item, inventory.labels)
        for claim_key in tuple(self._unmatched_inventory_signatures):
            if claim_key not in available_claim_keys:
                self._unmatched_inventory_signatures.pop(claim_key, None)

    def _inventory_is_usable(self, inventory: HardwareInventory) -> bool:
        return (
            self._manager_presence_sessions.get(inventory.manager_id)
            == inventory.session_id
        )

    def _ref_is_available(self, ref: DeviceRef) -> bool:
        inventory = self._inventory_by_manager.get(ref.manager_id)
        if inventory is None or not self._inventory_is_usable(inventory):
            return False
        return (ref.manager_id, ref.device_id) in _hardware_inventory_ref_keys(inventory)

    def _ref_unavailable_reason(self, ref: DeviceRef, *, source: str) -> str:
        inventory = self._inventory_by_manager.get(ref.manager_id)
        if inventory is None:
            return (
                f"hardware ref {ref.manager_id}/{ref.device_id} unavailable: "
                f"missing inventory during {source}"
            )
        manager_session_id = self._manager_presence_sessions.get(ref.manager_id)
        if manager_session_id != inventory.session_id:
            return (
                f"hardware ref {ref.manager_id}/{ref.device_id} unavailable: "
                "manager presence session "
                f"{manager_session_id!r} does not match inventory session "
                f"{inventory.session_id!r} during {source}"
            )
        if (ref.manager_id, ref.device_id) not in _hardware_inventory_ref_keys(
            inventory
        ):
            return (
                f"hardware ref {ref.manager_id}/{ref.device_id} unavailable: "
                f"device missing from inventory during {source}"
            )
        return f"hardware ref {ref.manager_id}/{ref.device_id} unavailable during {source}"

    async def _refresh_live_descriptor(self, live: LiveDeviceRoute) -> None:
        inventory = self._inventory_by_manager.get(live.ref.manager_id)
        if inventory is None or not self._inventory_is_usable(inventory):
            return
        item = inventory.devices.get(live.ref.device_id)
        if item is None:
            return
        descriptor = self._device_from_inventory(live.ref, item)
        if descriptor == live.device:
            return
        updated = self._device_registry.update_descriptor(
            ref=live.ref,
            device=descriptor,
        )
        if updated is None:
            return
        self._command_service.register_device(
            config_id=updated.config_id,
            ref=updated.ref,
            device=updated.device,
        )
        ctrl_ctx = await self._controller_contexts.get(updated.config_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.on_descriptor_changed(updated.device)

    def _claim_belongs_to_this_session(self, claim: DeviceClaim) -> bool:
        return (
            claim.claimed_by_endpoint == controller_address(self._controller_id)
            and claim.claimed_by_session_id == self._session_id
        )

    async def _revoke_owned_claim(
        self,
        owned: OwnedDeviceClaim,
        *,
        release_claim: bool,
        reason: str,
    ) -> None:
        live = self._device_registry.get_by_ref(owned.ref)
        if live is not None:
            await self._disconnect_live(
                live,
                release_claim=release_claim,
                reason=reason,
            )
            return
        self._owned_claims.pop(owned.key, None)
        if release_claim:
            await self._delete_owned_claim(owned)

    async def _claim_refresh_loop(self) -> None:
        while True:
            await anyio.sleep(CLAIM_HEARTBEAT_SECONDS)
            if self._stopping is not None and self._stopping.is_set():
                return
            for owned in tuple(self._owned_claims.values()):
                try:
                    entry = await self._lease_state.update(
                        owned.key,
                        self._new_claim(),
                        revision=owned.revision,
                        ttl=CLAIM_TTL_SECONDS,
                    )
                except StateConflict:
                    live = self._device_registry.get_by_ref(owned.ref)
                    if live is not None:
                        await self._disconnect_live(
                            live,
                            release_claim=False,
                            reason=f"claim refresh conflict for {owned.key}",
                        )
                    self._owned_claims.pop(owned.key, None)
                    continue
                except StateUnavailable:
                    logger.warning(
                        "Could not refresh claim %s; waiting for broker state",
                        owned.key,
                    )
                    continue
                refreshed = OwnedDeviceClaim(
                    key=owned.key,
                    config_id=owned.config_id,
                    ref=owned.ref,
                    revision=entry.revision,
                )
                if self._owned_claims.get(owned.key) != owned:
                    await self._delete_owned_claim(refreshed)
                    continue
                self._owned_claims[owned.key] = refreshed

    async def _disconnect_live(
        self,
        live: LiveDeviceRoute,
        *,
        release_claim: bool,
        reason: str,
    ) -> None:
        self._device_registry.disconnect_config(live.config_id)
        self._command_service.unregister_config(live.config_id)
        claim_key = device_claim_key(
            manager_id=live.ref.manager_id,
            device_id=live.ref.device_id,
        )
        owned = self._owned_claims.pop(claim_key, None)
        if release_claim and owned is not None:
            await self._delete_owned_claim(owned)
        await self.on_device_disconnected(live.config_id, reason=reason)

    async def start(self, ctx: RunContext):
        self._stopping = ctx.stopping
        self._start_soon = ctx.tg.start_soon
        if self._render_backend is None:
            self._render_backend = ProcessPoolRenderBackend()
        if self._actions_endpoint is not None:
            ctx.tg.start_soon(self._actions_subscription_loop)
        ctx.tg.start_soon(self._hardware_input_loop)
        ctx.tg.start_soon(self._inventory_loop)
        ctx.tg.start_soon(self._manager_presence_loop)
        ctx.tg.start_soon(self._claim_watch_loop)
        ctx.tg.start_soon(self._hardware_reconciliation_loop)
        ctx.tg.start_soon(self._claim_refresh_loop)

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
            await self._delete_owned_claim(owned)
            self._owned_claims.pop(owned.key, None)

    async def _delete_owned_claim(self, owned: OwnedDeviceClaim) -> None:
        with anyio.CancelScope(shield=True):
            revision = owned.revision
            while True:
                try:
                    await self._lease_state.delete(owned.key, revision=revision)
                    return
                except StateConflict:
                    try:
                        current = await self._lease_state.get(owned.key)
                    except StateUnavailable:
                        logger.warning("Could not inspect claim %s for release", owned.key)
                        return
                    if current is None:
                        return
                    claim = _valid_device_claim(current)
                    if claim is None or not self._claim_belongs_to_this_session(claim):
                        logger.info("Claim %s changed before release", owned.key)
                        return
                    logger.info(
                        "Claim %s refreshed during release; retrying release",
                        owned.key,
                    )
                    revision = current.revision
                except StateUnavailable:
                    logger.warning("Could not release claim %s", owned.key)
                    return

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
                return
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
                actions_bus=self._actions_endpoint,
                start_soon=self._start_soon,
                render_backend=self._render_backend,
                settings_service=self._settings_service,
                config_stream=stream,
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


def _valid_endpoint_presence(entry: StateEntry) -> EndpointPresence | None:
    parsed = parse_presence_endpoint_key(entry.key)
    if parsed is None:
        return None
    lane, endpoint = parsed
    try:
        presence = EndpointPresence.model_validate(entry.value)
    except ValueError:
        logger.warning("Ignoring invalid endpoint presence %s", entry.key)
        return None
    if presence.endpoint != endpoint or presence.lane != lane:
        logger.warning(
            "Ignoring endpoint presence %s with mismatched payload endpoint=%s lane=%s",
            entry.key,
            presence.endpoint,
            presence.lane,
        )
        return None
    return presence


def _valid_hardware_inventory(
    entry: StateEntry,
    *,
    manager_id: str,
) -> HardwareInventory | None:
    try:
        inventory = HardwareInventory.model_validate(entry.value)
    except ValueError:
        logger.warning("Ignoring invalid hardware inventory %s", entry.key)
        return None
    if (
        inventory.manager_id != manager_id
        or inventory.manager_endpoint != hardware_manager_address(manager_id)
    ):
        logger.warning(
            "Ignoring hardware inventory %s with mismatched payload",
            entry.key,
        )
        return None
    return inventory


def _valid_device_claim(entry: StateEntry) -> DeviceClaim | None:
    try:
        return DeviceClaim.model_validate(entry.value)
    except ValueError:
        logger.warning("Ignoring invalid device claim %s", entry.key)
        return None


def _unmatched_inventory_signature(
    device: DeviceDescriptor,
    labels: Mapping[str, str],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    return device.fingerprint, tuple(sorted(labels.items()))


def _hardware_inventory_ref_keys(
    inventory: HardwareInventory,
) -> set[tuple[str, str]]:
    return {
        (item.device_ref.manager_id, item.device_ref.device_id)
        for device_id, item in inventory.devices.items()
        if item.device_ref.device_id == device_id
    }
