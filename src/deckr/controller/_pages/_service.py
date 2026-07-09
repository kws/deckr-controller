"""Page stack, dynamic page sessions, and transition decisions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from deckr.actions.messages import (
    DynamicPageCommand,
    SettingsTargetRef,
    make_context_id,
    make_dynamic_page_id,
    make_page_session_id,
)

from deckr.controller._actions._models import ActionMetadata
from deckr.controller._binding_resolution import ConfiguredControlBinding
from deckr.controller.config._data import DeviceConfig, Profile
from deckr.controller.settings import static_action_identity_fallback

if TYPE_CHECKING:
    from deckr.controller._binding_planner import PagePlan


DEFAULT_WIDGET_TIMEOUT_MS = 60_000


@dataclass(frozen=True, slots=True)
class StaticPageRef:
    profile_name: str
    page_index: int


PageStackEntry = StaticPageRef | DynamicPageCommand


@dataclass(slots=True)
class DynamicPageSession:
    page_id: str
    page_session_id: str
    context_id: str
    action_instance_id: str
    owner_context_id: str
    owner_binding_id: str
    owner_control_id: str
    owner_action_uuid: str
    owner_provider_instance_id: str
    owner_provider_id: str
    owner_provider_session_id: str | None
    owner_action_meta: ActionMetadata
    owner_profile: str
    owner_page: int
    timeout_ms: int
    last_activity: float
    settings_target: SettingsTargetRef | None
    generation: int = 0


@dataclass(slots=True)
class PageFrame:
    entry: PageStackEntry
    page_session: DynamicPageSession | None
    committed_plan: PagePlan


@dataclass(frozen=True, slots=True)
class PageOwnerBinding:
    context_id: str
    binding_id: str
    control_id: str
    action_uuid: str
    provider_instance_id: str
    provider_id: str
    provider_session_id: str | None
    action_instance_id: str
    profile_id: str
    page_id: str
    page_session_id: str | None
    settings_target: SettingsTargetRef | None


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    config_active: bool
    frames: tuple[PageFrame, ...]
    current_frame: PageFrame | None
    current_plan: PagePlan | None
    active_dynamic_session: DynamicPageSession | None


@dataclass(frozen=True, slots=True)
class PageTransitionDraft:
    kind: Literal["set", "open", "replace", "close", "refresh"]
    entry: PageStackEntry
    page_session: DynamicPageSession | None
    departing: PageStackEntry | None
    retained_plan: PagePlan | None
    preserve_rebound_outputs: bool
    refresh_actions: bool
    sessions_to_close: tuple[DynamicPageSession, ...] = ()
    previous_dynamic_plans: tuple[PagePlan, ...] = ()
    cleanup_reason: str = "navigate"
    page_session_generation: int | None = None


@dataclass(frozen=True, slots=True)
class PageTransitionEffects:
    sessions_to_close: tuple[DynamicPageSession, ...] = ()
    previous_dynamic_plans: tuple[PagePlan, ...] = ()
    cleanup_reason: str = "navigate"


class PageSessionService:
    """Owns controller page stack and dynamic-page session state."""

    def __init__(
        self,
        config: DeviceConfig,
        *,
        clock: Callable[[], float],
    ) -> None:
        self._config = config
        self._clock = clock
        self._config_active = True
        self._frames: list[PageFrame] = []

    @property
    def config_active(self) -> bool:
        return self._config_active

    def snapshot(self) -> PageSnapshot:
        current_frame = self.current_frame()
        return PageSnapshot(
            config_active=self._config_active,
            frames=tuple(self._frames),
            current_frame=current_frame,
            current_plan=current_frame.committed_plan
            if current_frame is not None
            else None,
            active_dynamic_session=self.active_dynamic_session(),
        )

    def current_frame(self) -> PageFrame | None:
        return self._frames[-1] if self._frames else None

    def current_plan(self) -> PagePlan | None:
        frame = self.current_frame()
        return frame.committed_plan if frame is not None else None

    def active_dynamic_session(self) -> DynamicPageSession | None:
        for frame in reversed(self._frames):
            if frame.page_session is not None:
                return frame.page_session
        return None

    def resolve_static_bindings(
        self,
        ref: StaticPageRef,
    ) -> list[ConfiguredControlBinding]:
        profile = self._find_profile(ref.profile_name)
        page = profile.pages[ref.page_index]
        return [
            ConfiguredControlBinding(
                selector=control.selector,
                action_uuid=control.action,
                provider_instance_id=control.provider_instance_id,
                provider_labels=dict(control.provider_labels),
                settings=dict(control.settings),
                stable_id=control.id,
                identity_fallback=static_action_identity_fallback(
                    selector_control_id=control.selector.control_id,
                    control_index=control_index,
                ),
                template_overrides=dict(control.template_overrides),
            )
            for control_index, control in enumerate(page.controls)
        ]

    def begin_set_page(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageCommand | None = None,
        page_session: DynamicPageSession | None = None,
        close_dynamic: bool = True,
        close_reason: str = "navigate",
        refresh_actions: bool = True,
    ) -> PageTransitionDraft | None:
        if not self._config_active:
            return None
        if descriptor is not None:
            if page_session is None:
                return None
            entry: PageStackEntry = descriptor
        else:
            profile_name = profile or "default"
            page_index = page if page is not None else 0
            profile_obj = self._find_profile(profile_name)
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
            refresh_actions=refresh_actions,
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
            uuid=owner.action_uuid,
            provider_instance_id=owner.provider_instance_id,
            provider_id=owner.provider_id,
            provider_session_id=owner.provider_session_id,
        )
        session = DynamicPageSession(
            page_id=page_id,
            page_session_id=make_page_session_id(),
            context_id=make_context_id(),
            action_instance_id=owner.action_instance_id,
            owner_context_id=owner.context_id,
            owner_binding_id=owner.binding_id,
            owner_control_id=owner.control_id,
            owner_action_uuid=owner.action_uuid,
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
            refresh_actions=draft.refresh_actions,
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
            refresh_actions=True,
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
        if len(self._frames) < 2 or self._frames[-1].page_session is not session:
            return None
        departing_frame = self._frames[-1]
        restore_frame = next(
            (
                frame
                for frame in reversed(self._frames[:-1])
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
            retained_plan=restore_frame.committed_plan,
            preserve_rebound_outputs=True,
            refresh_actions=False,
            sessions_to_close=self._dynamic_sessions(reverse=True),
            previous_dynamic_plans=self._committed_dynamic_page_plans(),
            cleanup_reason=reason,
        )

    def begin_refresh_current(
        self,
        *,
        refresh_actions: bool,
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
            refresh_actions=refresh_actions,
        )

    def commit(
        self,
        draft: PageTransitionDraft,
        plan: PagePlan,
    ) -> PageTransitionEffects:
        if (
            draft.page_session is not None
            and draft.page_session_generation is not None
        ):
            draft.page_session.generation = draft.page_session_generation
        if plan.page_session is not draft.page_session:
            plan.page_session = draft.page_session

        frame = PageFrame(draft.entry, draft.page_session, plan)
        if draft.kind == "close":
            static_frames = [
                existing for existing in self._frames if existing.page_session is None
            ]
            if static_frames:
                static_frames[-1] = frame
                self._frames = static_frames
            else:
                self._frames = [frame]
        elif draft.kind == "refresh":
            if self._frames:
                self._frames[-1] = frame
            else:
                self._frames = [frame]
        elif isinstance(draft.entry, StaticPageRef):
            self._frames = [frame]
        elif draft.kind == "replace":
            if self._frames:
                self._frames[-1] = frame
            else:
                self._frames = [frame]
        elif draft.kind == "open":
            self._frames = [
                existing for existing in self._frames if existing.page_session is None
            ]
            self._frames.append(frame)
        else:
            self._frames.append(frame)

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
        if dynamic_only:
            self._frames = [
                frame for frame in self._frames if frame.page_session is None
            ]
        else:
            self._frames.clear()
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
            refresh_actions=True,
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

    def expired_session(self) -> DynamicPageSession | None:
        session = self.active_dynamic_session()
        if session is None or session.timeout_ms <= 0:
            return None
        elapsed_ms = int((self._clock() - session.last_activity) * 1000)
        if elapsed_ms >= session.timeout_ms:
            return session
        return None

    def move_owner_provider_session(self, action_meta: ActionMetadata) -> bool:
        session = self.active_dynamic_session()
        if session is None:
            return False
        if (
            action_meta.uuid != session.owner_action_uuid
            or action_meta.provider_instance_id
            != session.owner_provider_instance_id
        ):
            return False
        if (
            action_meta.provider_id == session.owner_provider_id
            and action_meta.provider_session_id == session.owner_provider_session_id
        ):
            return False
        session.owner_provider_id = action_meta.provider_id
        session.owner_provider_session_id = action_meta.provider_session_id
        session.owner_action_meta = action_meta
        return True

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
        return self._config.profiles[0]

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
        frames = reversed(self._frames) if reverse else self._frames
        return tuple(
            frame.page_session
            for frame in frames
            if frame.page_session is not None
        )

    def _committed_dynamic_page_plans(
        self,
        *,
        reverse: bool = False,
    ) -> tuple[PagePlan, ...]:
        frames = reversed(self._frames) if reverse else self._frames
        return tuple(
            frame.committed_plan
            for frame in frames
            if frame.page_session is not None
        )
