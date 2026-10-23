import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio
from deckr.actions.messages import (
    DynamicPageCommand,
    PageSessionMetadata,
)
from deckr.hardware.descriptors import (
    DeviceDescriptor,
)

from deckr.controller._actions._requests import OpenPage
from deckr.controller._binding_plan import PagePlan
from deckr.controller._pages import (
    DynamicPageSession,
    PageOwnerBinding,
    PageTransitionDraft,
    PageTransitionEffects,
    StaticPageRef,
)
from deckr.controller.config._data import DeviceConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NavigationBindings:
    build_plan: Callable
    prepare_commit: Callable
    apply_commit: Callable
    release_children: Callable
    sync_interest: Callable
    cancel_inputs: Callable
    revoke_bindings: Callable
    clear_outputs: Callable


class DeviceNavigation:
    """Execute local navigation decisions using explicit binding and lifecycle ports."""

    def __init__(self, *, state, boundary, bindings, lifecycle, clock, interval):
        self.state = state
        self.boundary = boundary
        self.bindings = bindings
        self.lifecycle = lifecycle
        self.clock = clock
        self.interval = interval

    async def _page_timeout_loop(self, stopping: anyio.Event) -> None:
        while not stopping.is_set():
            deadline = self.state.navigation.deadline()
            await anyio.sleep(self.interval)
            if stopping.is_set() or deadline is None:
                continue
            await self.page_expired(deadline, now=self.clock())

    async def page_expired(self, deadline, *, now: float) -> None:
        async with self.boundary:
            session = self.state.navigation.accept_expiry(deadline, now=now)
            if session is not None:
                await self._close_page_locked(
                    context_id=session.context_id, reason="timeout"
                )

    def page_session_metadata(
        self,
        session: DynamicPageSession,
    ) -> PageSessionMetadata:
        bindings = tuple(
            lease.context.metadata
            for lease in self.state.attachments.binding_leases.values()
            # Attachment effects precede the page notification on the owner's
            # queue, but have not run when this immutable snapshot is built.
            # Include their reserved identities; only binding_attached grants
            # the runtime access to an actual child binding.
            if lease.page_session_id == session.page_session_id
        )
        return PageSessionMetadata(
            providerInstanceId=session.owner_provider_instance_id,
            providerId=session.owner_provider_id,
            actionInstanceId=session.action_instance_id,
            configId=self.state.config.id,
            pageId=session.page_id,
            pageSessionId=session.page_session_id,
            generation=session.generation,
            contextId=session.context_id,
            ownerBindingId=session.owner_binding_id,
            bindings=bindings,
        )

    async def _apply_page_transition_effects(
        self,
        effects: PageTransitionEffects,
        *,
        causation_id: str | None = None,
    ) -> None:
        for session in effects.sessions_to_close:
            await self.lifecycle.emit_page_closed(
                session,
                effects.cleanup_reason,
                causation_id=causation_id,
            )
        await self.bindings.release_children(
            effects.previous_dynamic_plans,
            reason=effects.cleanup_reason,
        )

    async def _finalize_dynamic_page(
        self,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> tuple[PagePlan, ...]:
        effects = self.state.navigation.clear(reason=reason, dynamic_only=True)
        self.bindings.sync_interest()
        for session in effects.sessions_to_close:
            await self.lifecycle.emit_page_closed(
                session,
                reason,
                causation_id=causation_id,
            )
        return effects.previous_dynamic_plans

    async def build_transition_plan(
        self, draft: PageTransitionDraft
    ) -> PagePlan | None:
        plan = await self.bindings.build_plan(
            draft.entry,
            page_session=draft.page_session,
            page_session_generation=draft.page_session_generation,
            retained_plan=draft.retained_plan,
        )
        if (
            plan is None
            and draft.kind == "close"
            and isinstance(draft.entry, StaticPageRef)
        ):
            # Losing the underlying layout must not retain an expired or orphaned
            # temporary page. Remember the route, with no live bindings, so a
            # later descriptor/config change can plan it again from current facts.
            logger.warning(
                "Closing temporary page onto an unavailable configured route"
            )
            return PagePlan(
                entry=draft.entry,
                profile_id=draft.entry.profile_name,
                page_id=str(draft.entry.page_index),
                page_session=None,
                bindings=(),
            )
        return plan

    async def _execute_page_draft(
        self,
        draft: PageTransitionDraft,
        *,
        causation_id: str | None = None,
        apply_effects: bool = True,
        validate_commit: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[bool, PageTransitionEffects | None]:
        plan = await self.build_transition_plan(draft)
        if plan is None:
            return False, None
        if validate_commit is not None:
            await validate_commit()
        return await self.commit_page_draft(
            draft, plan, causation_id=causation_id, apply_effects=apply_effects
        )

    async def commit_page_draft(
        self,
        draft: PageTransitionDraft,
        plan: PagePlan,
        *,
        causation_id: str | None = None,
        apply_effects: bool = True,
    ) -> tuple[bool, PageTransitionEffects | None]:
        """Commit a locally planned transition while the session boundary is held."""
        commit = self.bindings.prepare_commit(
            plan,
            departing=draft.departing,
            preserve_rebound_outputs=draft.preserve_rebound_outputs,
        )
        effects = self.state.navigation.commit(draft, plan)
        await self.bindings.apply_commit(commit)
        self.bindings.sync_interest()
        if apply_effects:
            await self._apply_page_transition_effects(
                effects,
                causation_id=causation_id,
            )
        return True, effects

    async def set_page(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageCommand | None = None,
        causation_id: str | None = None,
    ) -> bool:
        """Navigate to a static page (profile, page) or dynamic page (descriptor)."""
        async with self.boundary:
            draft = self.state.navigation.begin_set_page(
                profile=profile,
                page=page,
                descriptor=descriptor,
                close_dynamic=True,
                close_reason="navigate",
            )
            if draft is None:
                if not self.state.navigation.config_active:
                    logger.info(
                        "Ignoring page transition while config %s is inactive",
                        self.state.config.id,
                    )
                return False
            ok, _ = await self._execute_page_draft(
                draft,
                causation_id=causation_id,
            )
            return ok

    async def open_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        binding_id: str | None = None,
        causation_id: str | None = None,
    ) -> DynamicPageSession | None:
        """Open or claim the dynamic page context for the sending action context."""
        if not descriptor or not descriptor.bindings:
            return None

        async with self.boundary:
            if binding_id is not None:
                owner_lease = self.state.attachments.binding_leases.get(binding_id)
                if owner_lease is not None and (
                    owner_lease.context_id != context_id
                    or not self.state.attachments.binding_command_authorized(
                        owner_lease
                    )
                ):
                    owner_lease = None
            else:
                owner_lease = self.state.attachments.binding_for_context(context_id)

            if owner_lease is None:
                logger.warning(
                    "open_page ignored: no active context for %s", context_id
                )
                return None

            draft = self.state.navigation.begin_action_request(
                OpenPage(descriptor),
                owner=PageOwnerBinding(
                    context_id=context_id,
                    binding_id=owner_lease.binding_id,
                    control_id=owner_lease.control_id,
                    action_id=owner_lease.action_id,
                    provider_instance_id=owner_lease.provider_instance_id,
                    provider_id=owner_lease.provider_id,
                    provider_session_id=owner_lease.provider_session_id,
                    action_instance_id=owner_lease.action_instance_id,
                    profile_id=owner_lease.profile_id,
                    page_id=owner_lease.page_id,
                    page_session_id=owner_lease.page_session_id,
                    settings_target=owner_lease.settings_target,
                    action_descriptor=owner_lease.action_descriptor,
                    contract=owner_lease.context.contract,
                ),
            )
            if draft is None or draft.page_session is None:
                return None
            ok, _ = await self._execute_page_draft(
                draft,
                causation_id=causation_id,
            )
            if ok:
                session = draft.page_session
                await self.lifecycle.emit_page_opened(
                    session,
                    causation_id=causation_id,
                )
                return session
            return None

    async def replace_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        causation_id: str | None = None,
    ) -> None:
        """Replace the active page session with a new concrete session."""
        if not descriptor or not descriptor.bindings:
            return
        async with self.boundary:
            draft = self.state.navigation.begin_replace_page(
                descriptor=descriptor,
                context_id=context_id,
            )
            if draft is None:
                logger.warning(
                    "replace_page ignored: no active page for %s", context_id
                )
                return
            ok, _ = await self._execute_page_draft(
                draft,
                causation_id=causation_id,
            )
            if ok:
                await self.lifecycle.emit_page_opened(
                    draft.page_session, causation_id=causation_id
                )

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None:
        """Close the active widget page and return to its owner profile page."""
        async with self.boundary:
            await self._close_page_locked(
                context_id=context_id,
                reason=reason,
                causation_id=causation_id,
            )

    async def _close_page_locked(
        self,
        *,
        context_id: str,
        reason: str,
        causation_id: str | None = None,
    ) -> bool:
        draft = self.state.navigation.begin_close_page(
            context_id=context_id,
            reason=reason,
        )
        if draft is None:
            logger.info("No owner for dynamic page")
            return False
        ok, _ = await self._execute_page_draft(
            draft,
            causation_id=causation_id,
        )
        if not ok:
            logger.warning(
                "Dynamic page close rejected because restore frame is invalid"
            )
        return ok

    async def clear_page(
        self,
        *,
        clear_outputs: bool = True,
        reason: str = "clear",
    ) -> None:
        async with self.boundary:
            await self.bindings.cancel_inputs()
            effects = self.state.navigation.clear(reason=reason)
            self.bindings.sync_interest()
            for session in effects.sessions_to_close:
                await self.lifecycle.emit_page_closed(session, reason)
            await self.bindings.revoke_bindings(
                clear_outputs=clear_outputs,
                reason=f"{reason}_page",
            )
            await self.bindings.release_children(
                effects.previous_dynamic_plans,
                reason=reason,
            )
            await self.lifecycle.destroy_all_action_instances(reason=reason)
            if clear_outputs:
                await self.bindings.clear_outputs()
            self.bindings.sync_interest()

    async def on_device_descriptor_changed(self, descriptor: DeviceDescriptor) -> None:
        """Re-resolve the active page against a changed device descriptor."""

        async with self.boundary:
            if self.state.stopped:
                return
            self.state.device = descriptor
            draft = self.state.navigation.begin_refresh_current(
                preserve_rebound_outputs=True,
            )
            if draft is None:
                return
            ok, _ = await self._execute_page_draft(
                draft,
                apply_effects=False,
            )
            if not ok:
                finalized_plans = await self._finalize_dynamic_page(
                    reason="device_descriptor_changed"
                )
                await self.bindings.revoke_bindings(
                    clear_outputs=False,
                    reason="device_descriptor_changed_failed",
                )
                await self.bindings.release_children(
                    finalized_plans,
                    reason="device_descriptor_changed",
                )
                return

    async def on_config_changed(self, config: DeviceConfig | None) -> None:
        """Handle config update or removal."""
        if config is not None:
            try:
                config = DeviceConfig.model_validate(config.model_dump(by_alias=True))
            except ValueError:
                logger.warning("Ignoring invalid configuration replacement")
                return
        async with self.boundary:
            if self.state.stopped:
                return
            if config is None:
                self.state.navigation.mark_config_inactive()
                await self.clear_page()
                return
            if config == self.state.config and self.state.navigation.config_active:
                return
            await self.bindings.cancel_inputs()
            self.state.config = config
            draft = self.state.navigation.update_config(config)
            for session in draft.sessions_to_close:
                await self.lifecycle.emit_page_closed(session, "config_change")
            await self.bindings.revoke_bindings(
                clear_outputs=False,
                reason="config_change",
            )
            await self.bindings.release_children(
                draft.previous_dynamic_plans,
                reason="config_change",
            )
            await self.lifecycle.destroy_all_action_instances(reason="config_change")
            ok, _ = await self._execute_page_draft(
                draft,
                apply_effects=False,
            )
            if not ok:
                self.state.navigation.clear(reason="config_change")
                self.bindings.sync_interest()
                return
