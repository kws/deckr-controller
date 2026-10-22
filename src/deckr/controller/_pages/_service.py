"""Pure navigation rules for a configured route and one temporary page."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from deckr.actions.messages import (
    DynamicPageCommand,
    make_context_id,
    make_dynamic_page_id,
    make_page_session_id,
)

from deckr.controller._actions._models import ActionMetadata
from deckr.controller._actions._requests import ClosePage, OpenPage, UpdatePage
from deckr.controller._binding_resolution import ConfiguredControlBinding
from deckr.controller._pages._configured import configured_bindings
from deckr.controller.config._data import DeviceConfig, Profile

if TYPE_CHECKING:
    from deckr.controller._binding_plan import PagePlan


from deckr.controller._pages._models import (
    DEFAULT_WIDGET_TIMEOUT_MS,
    DynamicPageSession,
    NavigationState,
    PageDeadline,
    PageFrame,
    PageOwnerBinding,
    PageRef,
    PageStateView,
    PageTransitionDraft,
    PageTransitionEffects,
    StaticPageRef,
)


class PageSessionService:
    """Navigation model owned and mutated by one device session; no adapters."""

    def __init__(
        self,
        config: DeviceConfig,
        *,
        clock: Callable[[], float],
    ) -> None:
        self._config = config
        self._clock = clock
        self._config_active = True
        self.state = NavigationState()

    @property
    def config_active(self) -> bool:
        return self._config_active

    def begin_action_request(
        self,
        request: OpenPage | UpdatePage | ClosePage,
        *,
        owner: PageOwnerBinding | None = None,
    ) -> PageTransitionDraft | None:
        """Normalize local and wire-adapted page intents into the same rules."""
        if isinstance(request, OpenPage):
            if owner is None:
                return None
            return self.begin_open_page(descriptor=request.definition, owner=owner)
        session = self.active_dynamic_session()
        grant = request.grant
        if session is None or (
            grant.page_session_id,
            grant.context_id,
            grant.action_instance_id,
            grant.revision,
        ) != (
            session.page_session_id,
            session.context_id,
            session.action_instance_id,
            session.generation,
        ):
            return None
        if isinstance(request, UpdatePage):
            return self.begin_replace_page(
                descriptor=request.definition, context_id=grant.context_id
            )
        return self.begin_close_page(context_id=grant.context_id)

    def view(self) -> PageStateView:
        current_frame = self.current_frame()
        return PageStateView(
            config_active=self._config_active,
            frames=tuple(self.state.frames),
            current_frame=current_frame,
            current_plan=current_frame.committed_plan
            if current_frame is not None
            else None,
            active_dynamic_session=self.active_dynamic_session(),
        )

    def current_frame(self) -> PageFrame | None:
        return self.state.frames[-1] if self.state.frames else None

    def current_plan(self) -> PagePlan | None:
        frame = self.current_frame()
        return frame.committed_plan if frame is not None else None

    def active_dynamic_session(self) -> DynamicPageSession | None:
        for frame in reversed(self.state.frames):
            if frame.page_session is not None:
                return frame.page_session
        return None

    def resolve_static_bindings(
        self, ref: StaticPageRef
    ) -> list[ConfiguredControlBinding]:
        return configured_bindings(self._config, ref)

    def begin_set_page(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageCommand | None = None,
        page_session: DynamicPageSession | None = None,
        close_dynamic: bool = True,
        close_reason: str = "navigate",
    ) -> PageTransitionDraft | None:
        if not self._config_active:
            return None
        if descriptor is not None:
            if page_session is None:
                return None
            entry: PageRef = descriptor
        else:
            profile_name = (
                profile if profile is not None else self._config.profiles[0].name
            )
            page_index = page if page is not None else 0
            try:
                profile_obj = self._find_profile(profile_name)
            except ValueError:
                return None
            if not 0 <= page_index < len(profile_obj.pages):
                return None
            entry = StaticPageRef(
                profile_name=profile_obj.name,
                page_index=page_index,
            )

        current_frame = self.current_frame()
        departing = current_frame.entry if current_frame is not None else None
        replace_dynamic_page = (
            descriptor is not None
            and page_session is not None
            and current_frame is not None
            and current_frame.page_session is page_session
        )
        retained_plan = (
            current_frame.committed_plan
            if (
                current_frame is not None
                and current_frame.entry == entry
                and not replace_dynamic_page
            )
            else None
        )
        preserve_rebound_outputs = isinstance(departing, DynamicPageCommand) and (
            isinstance(entry, StaticPageRef)
            or (page_session is not None and isinstance(entry, DynamicPageCommand))
        )
        return PageTransitionDraft(
            kind="set",
            entry=entry,
            page_session=page_session,
            departing=departing,
            retained_plan=retained_plan,
            preserve_rebound_outputs=preserve_rebound_outputs,
            sessions_to_close=(
                self._dynamic_sessions(reverse=True) if close_dynamic else ()
            ),
            previous_dynamic_plans=self._committed_dynamic_page_plans(),
            cleanup_reason=(
                "page_child_removed" if replace_dynamic_page else close_reason
            ),
        )

    def begin_open_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        owner: PageOwnerBinding,
    ) -> PageTransitionDraft | None:
        if not self._config_active or not descriptor or not descriptor.bindings:
            return None
        current = self.active_dynamic_session()
        try:
            owner_page = int(owner.page_id)
        except ValueError:
            owner_page = current.owner_page if current is not None else 0
        if owner.page_session_id is not None and current is not None:
            owner_profile = current.owner_profile
            owner_page = current.owner_page
        else:
            owner_profile = owner.profile_id

        page_id = descriptor.page_id or make_dynamic_page_id()
        concrete_descriptor = DynamicPageCommand(
            pageId=page_id,
            bindings=descriptor.bindings,
        )
        owner_action_meta = ActionMetadata(
            action_id=owner.action_id,
            provider_instance_id=owner.provider_instance_id,
            provider_id=owner.provider_id,
            provider_session_id=owner.provider_session_id,
            descriptor=owner.action_descriptor,
        )
        session = DynamicPageSession(
            page_id=page_id,
            page_session_id=make_page_session_id(),
            context_id=make_context_id(),
            action_instance_id=owner.action_instance_id,
            owner_context_id=owner.context_id,
            owner_binding_id=owner.binding_id,
            owner_control_id=owner.control_id,
            owner_action_id=owner.action_id,
            owner_provider_instance_id=owner.provider_instance_id,
            owner_provider_id=owner.provider_id,
            owner_provider_session_id=owner.provider_session_id,
            owner_action_meta=owner_action_meta,
            owner_profile=owner_profile,
            owner_page=owner_page,
            timeout_ms=self._resolve_widget_timeout_ms(owner_profile, owner_page),
            last_activity=self._clock(),
            settings_target=owner.settings_target,
            generation=0,
            owner_contract=owner.contract,
        )
        draft = self.begin_set_page(
            descriptor=concrete_descriptor,
            page_session=session,
            close_dynamic=True,
            close_reason="open_page",
        )
        if draft is None:
            return None
        return PageTransitionDraft(
            kind="open",
            entry=draft.entry,
            page_session=draft.page_session,
            departing=draft.departing,
            retained_plan=draft.retained_plan,
            preserve_rebound_outputs=draft.preserve_rebound_outputs,
            sessions_to_close=draft.sessions_to_close,
            previous_dynamic_plans=draft.previous_dynamic_plans,
            cleanup_reason=draft.cleanup_reason,
            page_session_generation=draft.page_session_generation,
        )

    def begin_replace_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
    ) -> PageTransitionDraft | None:
        if not self._config_active or not descriptor or not descriptor.bindings:
            return None
        session = self._page_control_session(context_id)
        current_frame = self.current_frame()
        if (
            session is None
            or current_frame is None
            or current_frame.page_session is not session
        ):
            return None
        if descriptor.page_id != session.page_id:
            return None
        replacement = DynamicPageCommand(
            pageId=session.page_id,
            bindings=descriptor.bindings,
        )
        return PageTransitionDraft(
            kind="replace",
            entry=replacement,
            page_session=session,
            departing=current_frame.entry,
            retained_plan=None,
            preserve_rebound_outputs=True,
            previous_dynamic_plans=self._committed_dynamic_page_plans(),
            cleanup_reason="page_child_removed",
            page_session_generation=session.generation + 1,
        )

    def begin_close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
    ) -> PageTransitionDraft | None:
        session = self._page_control_session(context_id)
        if session is None:
            return None
        if (
            len(self.state.frames) < 2
            or self.state.frames[-1].page_session is not session
        ):
            return None
        departing_frame = self.state.frames[-1]
        restore_frame = next(
            (
                frame
                for frame in reversed(self.state.frames[:-1])
                if frame.page_session is None
            ),
            None,
        )
        if restore_frame is None:
            return None
        return PageTransitionDraft(
            kind="close",
            entry=restore_frame.entry,
            page_session=restore_frame.page_session,
            departing=departing_frame.entry,
            retained_plan=None,
            preserve_rebound_outputs=True,
            sessions_to_close=self._dynamic_sessions(reverse=True),
            previous_dynamic_plans=self._committed_dynamic_page_plans(),
            cleanup_reason=reason,
        )

    def begin_refresh_current(
        self,
        *,
        preserve_rebound_outputs: bool,
    ) -> PageTransitionDraft | None:
        current_frame = self.current_frame()
        if current_frame is None:
            return None
        return PageTransitionDraft(
            kind="refresh",
            entry=current_frame.entry,
            page_session=current_frame.page_session,
            departing=current_frame.entry,
            retained_plan=current_frame.committed_plan,
            preserve_rebound_outputs=preserve_rebound_outputs,
        )

    def commit(
        self,
        draft: PageTransitionDraft,
        plan: PagePlan,
    ) -> PageTransitionEffects:
        if draft.page_session is not None and draft.page_session_generation is not None:
            draft.page_session.generation = draft.page_session_generation
        if plan.page_session is not draft.page_session:
            plan = replace(plan, page_session=draft.page_session)

        frame = PageFrame(draft.entry, draft.page_session, plan)
        if isinstance(draft.entry, StaticPageRef):
            self.state.configured_route = frame
            self.state.temporary_page = None
        else:
            self.state.temporary_page = frame
        self.state.revision += 1

        return PageTransitionEffects(
            sessions_to_close=draft.sessions_to_close,
            previous_dynamic_plans=draft.previous_dynamic_plans,
            cleanup_reason=draft.cleanup_reason,
        )

    def clear(
        self,
        *,
        reason: str = "clear",
        dynamic_only: bool = False,
    ) -> PageTransitionEffects:
        previous_dynamic_plans = self._committed_dynamic_page_plans(reverse=True)
        sessions_to_close = self._dynamic_sessions(reverse=True)
        self.state.temporary_page = None
        if not dynamic_only:
            self.state.configured_route = None
        self.state.revision += 1
        return PageTransitionEffects(
            sessions_to_close=sessions_to_close,
            previous_dynamic_plans=previous_dynamic_plans,
            cleanup_reason=reason,
        )

    def update_config(self, config: DeviceConfig) -> PageTransitionDraft:
        self._config = config
        self._config_active = True
        profile = config.profiles[0]
        root = StaticPageRef(profile_name=profile.name, page_index=0)
        current_frame = self.current_frame()
        return PageTransitionDraft(
            kind="set",
            entry=root,
            page_session=None,
            departing=current_frame.entry if current_frame is not None else None,
            retained_plan=None,
            preserve_rebound_outputs=isinstance(
                current_frame.entry if current_frame is not None else None,
                DynamicPageCommand,
            ),
            sessions_to_close=self._dynamic_sessions(reverse=True),
            previous_dynamic_plans=self._committed_dynamic_page_plans(reverse=True),
            cleanup_reason="config_change",
        )

    def mark_config_inactive(self) -> None:
        self._config_active = False

    def record_activity(self) -> None:
        session = self.active_dynamic_session()
        if session is not None:
            session.last_activity = self._clock()
            session.deadline_revision += 1

    def deadline(self) -> PageDeadline | None:
        session = self.active_dynamic_session()
        if session is None or session.timeout_ms <= 0:
            return None
        return PageDeadline(
            session.page_session_id,
            session.deadline_revision,
            session.last_activity + session.timeout_ms / 1000,
        )

    def accept_expiry(
        self, deadline: PageDeadline, *, now: float
    ) -> DynamicPageSession | None:
        current = self.deadline()
        if current != deadline or now < deadline.due_at:
            return None
        return self.active_dynamic_session()

    def expired_session(self) -> DynamicPageSession | None:
        deadline = self.deadline()
        return self.accept_expiry(deadline, now=self._clock()) if deadline else None

    def _page_control_session(
        self,
        context_id: str,
    ) -> DynamicPageSession | None:
        session = self.active_dynamic_session()
        if session is None:
            return None
        if context_id == session.context_id:
            return session
        return None

    def _find_profile(self, profile_name: str) -> Profile:
        for profile in self._config.profiles:
            if profile.name == profile_name:
                return profile
        raise ValueError(f"Unknown configured profile: {profile_name}")

    def _resolve_widget_timeout_ms(self, profile_name: str, page_index: int) -> int:
        profile = self._find_profile(profile_name)
        timeout_ms: int | None = None
        if 0 <= page_index < len(profile.pages):
            timeout_ms = profile.pages[page_index].widget_timeout_ms
        if timeout_ms is None:
            timeout_ms = profile.widget_timeout_ms
        if timeout_ms is None:
            timeout_ms = DEFAULT_WIDGET_TIMEOUT_MS
        return max(0, int(timeout_ms))

    def _dynamic_sessions(
        self,
        *,
        reverse: bool = False,
    ) -> tuple[DynamicPageSession, ...]:
        frames = reversed(self.state.frames) if reverse else self.state.frames
        return tuple(
            frame.page_session for frame in frames if frame.page_session is not None
        )

    def _committed_dynamic_page_plans(
        self,
        *,
        reverse: bool = False,
    ) -> tuple[PagePlan, ...]:
        frames = reversed(self.state.frames) if reverse else self.state.frames
        return tuple(
            frame.committed_plan for frame in frames if frame.page_session is not None
        )
