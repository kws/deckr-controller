import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import anyio
from deckr.components import BaseComponent, RunContext
from deckr.contracts.messages import (
    DeckrMessage,
    controller_address,
    hardware_manager_address,
)
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import messages as hw_messages
from deckr.lanes import EndpointLane
from deckr.pluginhost.messages import (
    COMMAND_MESSAGE_TYPES,
    plugin_message_for_controller,
    subject_config_id,
)
from deckr.state import (
    DeviceClaim,
    EndpointPresence,
    HardwareInventory,
    StateConflict,
    StateEntry,
    StateStore,
    StateUnavailable,
    device_claim_key,
    parse_device_claim_key,
    parse_hardware_inventory_key,
    parse_presence_endpoint_key,
    presence_endpoint_key,
)

from deckr.controller._device_manager import DeviceManager
from deckr.controller._hardware_service import (
    HardwareCommandService,
    HardwareDeviceRegistry,
    LiveHardwareDevice,
)
from deckr.controller._render_dispatcher import (
    ProcessPoolRenderBackend,
    RenderBackend,
)
from deckr.controller.config import DeviceConfigService
from deckr.controller.plugin.action_registry import ActionRegistry
from deckr.controller.plugin.events import ActionsChangedEvent
from deckr.controller.settings import SettingsService

logger = logging.getLogger(__name__)

PRESENCE_HEARTBEAT_SECONDS = 5.0
PRESENCE_TTL_SECONDS = 15


@dataclass(frozen=True, slots=True)
class OwnedDeviceClaim:
    key: str
    config_id: str
    ref: hw_messages.HardwareDeviceRef
    revision: int


class ControllerService(BaseComponent):
    def __init__(
        self,
        hardware_endpoint: EndpointLane,
        state: StateStore,
        config_service: DeviceConfigService,
        settings_service: SettingsService,
        *,
        controller_id: str,
        action_registry: ActionRegistry | None = None,
        plugin_endpoint: EndpointLane | None = None,
        render_backend: RenderBackend | None = None,
    ):
        super().__init__()
        self._hardware_endpoint = hardware_endpoint
        self._state = state
        self._device_registry = HardwareDeviceRegistry()
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
        self._plugin_endpoint = plugin_endpoint
        self._start_soon: Callable | None = None
        self._render_backend = render_backend
        self._session_id = str(uuid.uuid4())
        self._owned_claims: dict[str, OwnedDeviceClaim] = {}
        self._blocked_claim_revisions: dict[str, int] = {}
        self._inventory_by_manager: dict[str, HardwareInventory] = {}
        self._manager_presence_sessions: dict[str, str] = {}
        self._owned_presence_revisions: dict[str, int] = {}
        self._stopping: anyio.Event | None = None

    async def _handle_plugin_command(self, msg: DeckrMessage) -> None:
        """Route command messages to the appropriate DeviceManager."""
        if msg.message_type not in COMMAND_MESSAGE_TYPES:
            return
        config_id = subject_config_id(msg.subject)
        if config_id is None:
            logger.warning(
                "Ignoring plugin command %s without config subject from %s",
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

    async def _plugin_subscription_loop(self) -> None:
        """Subscribe to plugin bus and route command messages to DeviceManagers."""
        if self._plugin_endpoint is None:
            return
        async with self._plugin_endpoint.subscribe() as stream:
            async for event in stream:
                try:
                    if not isinstance(event, DeckrMessage):
                        continue
                    if not plugin_message_for_controller(event, self._controller_id):
                        continue
                    if event.message_type in COMMAND_MESSAGE_TYPES:
                        await self._handle_plugin_command(event)
                except Exception:
                    if isinstance(event, DeckrMessage):
                        logger.exception(
                            "Error handling plugin message %s from %s",
                            event.message_type,
                            event.sender,
                        )
                    else:
                        logger.exception("Error handling plugin bus event")

    async def _hardware_input_loop(self) -> None:
        async with self._hardware_endpoint.subscribe() as subscribe:
            async for message in subscribe:
                event = hw_messages.hardware_body_from_message(message)
                if not isinstance(event, hw_messages.HARDWARE_INPUT_MESSAGE_TYPES):
                    continue
                if isinstance(
                    event,
                    hw_messages.DeviceConnectedMessage
                    | hw_messages.DeviceDisconnectedMessage,
                ):
                    continue
                ref = hw_messages.hardware_device_ref_from_message(message)
                if ref is None:
                    continue
                live = self._device_registry.get_by_ref(ref)
                if live is None:
                    continue
                ctrl_ctx = await self._controller_contexts.get(live.config_id)
                if ctrl_ctx is not None:
                    await ctrl_ctx.on_event(message)

    async def _inventory_loop(self) -> None:
        while True:
            try:
                async with self._state.watch("inventory.hardware.") as stream:
                    async for change in stream:
                        manager_id = parse_hardware_inventory_key(change.key)
                        if manager_id is None:
                            continue
                        if change.entry is None:
                            self._inventory_by_manager.pop(manager_id, None)
                            await self._handle_manager_unreachable(manager_id)
                            continue
                        try:
                            inventory = HardwareInventory.model_validate(
                                change.entry.value
                            )
                        except ValueError:
                            logger.warning(
                                "Ignoring invalid hardware inventory %s", change.key
                            )
                            continue
                        if (
                            inventory.manager_id != manager_id
                            or inventory.manager_endpoint
                            != hardware_manager_address(manager_id)
                        ):
                            logger.warning(
                                "Ignoring hardware inventory %s with mismatched payload",
                                change.key,
                            )
                            continue
                        await self._handle_inventory(inventory)
            except StateUnavailable:
                logger.warning("Hardware inventory state unavailable; retrying")
                await anyio.sleep(1.0)

    async def _handle_inventory(self, inventory: HardwareInventory) -> None:
        manager_id = inventory.manager_id
        self._inventory_by_manager[manager_id] = inventory
        if self._manager_presence_sessions.get(manager_id) != inventory.session_id:
            return
        seen_refs: set[hw_messages.HardwareDeviceRef] = set()
        for device_id, item in inventory.devices.items():
            ref = hw_messages.HardwareDeviceRef(
                manager_id=manager_id,
                device_id=item.device_id or device_id,
            )
            seen_refs.add(ref)
            if self._device_registry.get_by_ref(ref) is not None:
                continue
            await self._try_claim_inventory_device(ref, item)

        for live in self._device_registry.for_manager(manager_id):
            if live.ref not in seen_refs:
                await self._disconnect_live(live, release_claim=True)

    async def _try_claim_inventory_device(
        self,
        ref: hw_messages.HardwareDeviceRef,
        item,
    ) -> None:
        device = self._device_from_inventory(ref, item)
        try:
            config = await self._config_service.match_device(
                fingerprint=device.fingerprint,
                manager_id=ref.manager_id,
            )
        except ValueError:
            logger.exception(
                "Ambiguous config for hardware fingerprint=%s manager=%s",
                device.fingerprint,
                ref.manager_id,
            )
            return
        if config is None:
            logger.info(
                "No controller config matched hardware fingerprint=%s manager=%s",
                device.fingerprint,
                ref.manager_id,
            )
            return
        claim_key = device_claim_key(manager_id=ref.manager_id, device_id=ref.device_id)
        try:
            current_claim = await self._state.get(claim_key)
        except StateUnavailable:
            logger.warning("Could not inspect claim %s; retrying later", claim_key)
            return
        if current_claim is not None:
            self._remember_blocked_claim(claim_key, current_claim)
            return
        claim = self._new_claim()
        try:
            entry = await self._state.create(claim_key, claim, ttl=PRESENCE_TTL_SECONDS)
        except StateConflict:
            try:
                current_claim = await self._state.get(claim_key)
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
        self._command_service.register_device(config_id=config.id, ref=ref)
        await self.on_device_connected(live, initial_config=config)

    def _device_from_inventory(
        self,
        ref: hw_messages.HardwareDeviceRef,
        item,
    ) -> hw_messages.HardwareDevice:
        descriptor = dict(item.descriptor or {})
        if descriptor:
            return hw_messages.HardwareDevice.model_validate(descriptor)
        return hw_messages.HardwareDevice(
            id=ref.device_id,
            fingerprint=item.fingerprint,
            hid=item.fingerprint,
            slots=(),
            name=item.hardware_type,
        )

    def _new_claim(self) -> DeviceClaim:
        return DeviceClaim(
            claimedByEndpoint=controller_address(self._controller_id),
            claimedBySessionId=self._session_id,
            timestamp=datetime.now(UTC),
            ttlSeconds=PRESENCE_TTL_SECONDS,
        )

    def _remember_blocked_claim(self, key: str, entry: StateEntry) -> None:
        if key not in self._blocked_claim_revisions:
            parsed = parse_device_claim_key(key)
            if parsed is not None:
                manager_id, device_id = parsed
                logger.info("Device %s/%s is already claimed", manager_id, device_id)
        self._blocked_claim_revisions[key] = entry.revision

    async def _presence_loop(
        self,
        endpoint: EndpointLane,
        stopping: anyio.Event,
    ) -> None:
        key = presence_endpoint_key(lane=endpoint.lane.name, endpoint=endpoint.endpoint)
        while not stopping.is_set():
            try:
                entry = await self._state.put(
                    key,
                    EndpointPresence(
                        endpoint=endpoint.endpoint,
                        lane=endpoint.lane.name,
                        sessionId=self._session_id,
                        timestamp=datetime.now(UTC),
                        ttlSeconds=PRESENCE_TTL_SECONDS,
                        metadata={"runtime": "deckr-controller"},
                    ),
                    ttl=PRESENCE_TTL_SECONDS,
                )
                self._owned_presence_revisions[key] = entry.revision
                await anyio.sleep(PRESENCE_HEARTBEAT_SECONDS)
            except StateUnavailable:
                logger.warning(
                    "Could not refresh controller endpoint presence %s; retrying",
                    key,
                )
                await anyio.sleep(PRESENCE_HEARTBEAT_SECONDS)
        await self._withdraw_presence_key(key, endpoint.endpoint)

    async def _withdraw_presence_key(self, key: str, endpoint) -> None:
        revision = self._owned_presence_revisions.pop(key, None)
        if revision is None:
            return
        with anyio.CancelScope(shield=True):
            try:
                await self._state.delete(key, revision=revision)
            except StateConflict:
                logger.debug(
                    "Controller endpoint presence changed before withdrawal for %s",
                    endpoint,
                )
            except Exception:
                logger.debug(
                    "Failed to withdraw controller endpoint presence for %s",
                    endpoint,
                    exc_info=True,
                )

    async def _manager_presence_loop(self) -> None:
        while True:
            try:
                async with self._state.watch("presence.endpoint.") as stream:
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
                        manager_id = endpoint.endpoint_id
                        if change.entry is None:
                            self._manager_presence_sessions.pop(manager_id, None)
                            await self._handle_manager_unreachable(
                                manager_id,
                                drop_inventory=False,
                            )
                            continue
                        try:
                            presence = EndpointPresence.model_validate(
                                change.entry.value
                            )
                        except ValueError:
                            self._manager_presence_sessions.pop(manager_id, None)
                            await self._handle_manager_unreachable(
                                manager_id,
                                drop_inventory=False,
                            )
                            continue
                        if presence.endpoint != endpoint or presence.lane != lane:
                            logger.warning(
                                "Ignoring manager presence %s with mismatched payload",
                                change.key,
                            )
                            self._manager_presence_sessions.pop(manager_id, None)
                            await self._handle_manager_unreachable(
                                manager_id,
                                drop_inventory=False,
                            )
                            continue
                        previous = self._manager_presence_sessions.get(manager_id)
                        if previous is not None and previous != presence.session_id:
                            await self._handle_manager_unreachable(
                                manager_id,
                                drop_inventory=False,
                            )
                        self._manager_presence_sessions[manager_id] = presence.session_id
                        inventory = self._inventory_by_manager.get(manager_id)
                        if (
                            inventory is not None
                            and inventory.session_id == presence.session_id
                        ):
                            await self._handle_inventory(inventory)
            except StateUnavailable:
                logger.warning("Manager presence state unavailable; retrying")
                await anyio.sleep(1.0)

    async def _claim_watch_loop(self) -> None:
        while True:
            try:
                async with self._state.watch("claim.device.") as stream:
                    async for change in stream:
                        parsed = parse_device_claim_key(change.key)
                        if parsed is None:
                            continue
                        manager_id, device_id = parsed
                        owned = self._owned_claims.get(change.key)
                        if owned is None:
                            if change.entry is None:
                                self._blocked_claim_revisions.pop(change.key, None)
                                await self._try_claim_after_claim_loss(
                                    manager_id, device_id
                                )
                            else:
                                self._blocked_claim_revisions[change.key] = (
                                    change.entry.revision
                                )
                            continue
                        if change.entry is None:
                            self._blocked_claim_revisions.pop(change.key, None)
                            live = self._device_registry.get_by_ref(owned.ref)
                            if live is not None:
                                await self._disconnect_live(live, release_claim=False)
                            self._owned_claims.pop(change.key, None)
                            continue
                        await self._handle_owned_claim_update(change.key, change.entry)
            except StateUnavailable:
                logger.warning("Device claim state unavailable; retrying")
                await anyio.sleep(1.0)

    async def _try_claim_after_claim_loss(
        self,
        manager_id: str,
        device_id: str,
    ) -> None:
        inventory = self._inventory_by_manager.get(manager_id)
        if inventory is None:
            return
        if self._manager_presence_sessions.get(manager_id) != inventory.session_id:
            return
        item = inventory.devices.get(device_id)
        if item is None:
            return
        ref = hw_messages.HardwareDeviceRef(
            manager_id=manager_id,
            device_id=item.device_id or device_id,
        )
        if self._device_registry.get_by_ref(ref) is not None:
            return
        await self._try_claim_inventory_device(ref, item)

    async def _handle_owned_claim_update(self, key: str, entry: StateEntry) -> None:
        owned = self._owned_claims.get(key)
        if owned is None:
            return
        try:
            claim = DeviceClaim.model_validate(entry.value)
        except ValueError:
            live = self._device_registry.get_by_ref(owned.ref)
            if live is not None:
                await self._disconnect_live(live, release_claim=False)
            self._owned_claims.pop(key, None)
            return
        if (
            claim.claimed_by_endpoint != controller_address(self._controller_id)
            or claim.claimed_by_session_id != self._session_id
        ):
            live = self._device_registry.get_by_ref(owned.ref)
            if live is not None:
                await self._disconnect_live(live, release_claim=False)
            self._owned_claims.pop(key, None)
            return
        self._owned_claims[key] = OwnedDeviceClaim(
            key=owned.key,
            config_id=owned.config_id,
            ref=owned.ref,
            revision=entry.revision,
        )

    async def _claim_refresh_loop(self) -> None:
        while True:
            await anyio.sleep(PRESENCE_HEARTBEAT_SECONDS)
            if self._stopping is not None and self._stopping.is_set():
                return
            for owned in tuple(self._owned_claims.values()):
                try:
                    entry = await self._state.update(
                        owned.key,
                        self._new_claim(),
                        revision=owned.revision,
                        ttl=PRESENCE_TTL_SECONDS,
                    )
                except StateConflict:
                    live = self._device_registry.get_by_ref(owned.ref)
                    if live is not None:
                        await self._disconnect_live(live, release_claim=False)
                    self._owned_claims.pop(owned.key, None)
                    continue
                except StateUnavailable:
                    logger.warning(
                        "Could not refresh claim %s; waiting for broker state",
                        owned.key,
                    )
                    continue
                self._owned_claims[owned.key] = OwnedDeviceClaim(
                    key=owned.key,
                    config_id=owned.config_id,
                    ref=owned.ref,
                    revision=entry.revision,
                )

    async def _handle_manager_unreachable(
        self,
        manager_id: str,
        *,
        drop_inventory: bool = True,
    ) -> None:
        if drop_inventory:
            self._inventory_by_manager.pop(manager_id, None)
        for live in self._device_registry.for_manager(manager_id):
            await self._disconnect_live(live, release_claim=True)

    async def _disconnect_live(
        self,
        live: LiveHardwareDevice,
        *,
        release_claim: bool,
    ) -> None:
        self._device_registry.disconnect_config(live.config_id)
        self._command_service.unregister_config(live.config_id)
        claim_key = device_claim_key(
            manager_id=live.ref.manager_id,
            device_id=live.ref.device_id,
        )
        owned = self._owned_claims.pop(claim_key, None)
        if release_claim and owned is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await self._state.delete(claim_key, revision=owned.revision)
                except StateConflict:
                    logger.info("Claim %s changed before release", claim_key)
                except StateUnavailable:
                    logger.warning("Could not release claim %s", claim_key)
        await self.on_device_disconnected(live.config_id)

    async def start(self, ctx: RunContext):
        self._stopping = ctx.stopping
        self._start_soon = ctx.tg.start_soon
        if self._render_backend is None:
            self._render_backend = ProcessPoolRenderBackend()
        ctx.tg.start_soon(self._presence_loop, self._hardware_endpoint, ctx.stopping)
        if self._plugin_endpoint is not None:
            ctx.tg.start_soon(self._presence_loop, self._plugin_endpoint, ctx.stopping)
            ctx.tg.start_soon(self._plugin_subscription_loop)
        ctx.tg.start_soon(self._hardware_input_loop)
        ctx.tg.start_soon(self._inventory_loop)
        ctx.tg.start_soon(self._manager_presence_loop)
        ctx.tg.start_soon(self._claim_watch_loop)
        ctx.tg.start_soon(self._claim_refresh_loop)

    async def stop(self):
        if self._stopping is not None:
            self._stopping.set()
        for live in self._device_registry.all():
            await self._disconnect_live(live, release_claim=True)
        for ctrl_ctx in await self._controller_contexts.values():
            await ctrl_ctx.clear_page(clear_outputs=False)
        await self._controller_contexts.clear()
        await self._release_owned_claims()
        for key in tuple(self._owned_presence_revisions):
            await self._withdraw_presence_key(key, key)
        if self._render_backend is not None:
            await self._render_backend.aclose()

    async def _release_owned_claims(self) -> None:
        for owned in tuple(self._owned_claims.values()):
            with anyio.CancelScope(shield=True):
                try:
                    await self._state.delete(owned.key, revision=owned.revision)
                except StateConflict:
                    logger.info("Claim %s changed before release", owned.key)
                except StateUnavailable:
                    logger.warning("Could not release claim %s", owned.key)
            self._owned_claims.pop(owned.key, None)

    async def _device_lifecycle(
        self,
        live: LiveHardwareDevice,
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
                plugin_bus=self._plugin_endpoint,
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
        live: LiveHardwareDevice,
        *,
        initial_config,
    ):
        if await self._controller_contexts.get(live.config_id) is not None:
            await self.on_device_disconnected(live.config_id)
        logger.info(
            "Starting controller service for config %s from %s/%s",
            live.config_id,
            live.ref.manager_id,
            live.ref.device_id,
        )
        self._start_soon(self._device_lifecycle, live, initial_config)

    async def on_device_disconnected(self, config_id: str):
        ctrl_ctx = await self._controller_contexts.pop(config_id)
        disconnect_ev = self._device_disconnect_events.get(config_id)
        if disconnect_ev is not None:
            disconnect_ev.set()
        try:
            if ctrl_ctx is not None:
                await ctrl_ctx.clear_page(clear_outputs=False)
        finally:
            logger.info("Stopped controller service for config %s", config_id)
