import logging

from deckr.controller._actions._requests import (
    BindingGrant,
    ClosePage,
    Navigate,
    OpenPage,
    PageGrant,
    SetRasterImage,
    SetTitle,
    UpdatePage,
)

logger = logging.getLogger(__name__)


class BindingRequestHandler:
    """Execute local typed requests only within their current binding/page grant."""

    def __init__(
        self,
        *,
        state,
        boundary,
        hardware,
        navigation,
        current_contract,
        provider_session_key_for_session,
        emit_page_opened,
        agreement_current,
        set_page,
        open_page,
    ):
        self.state, self.boundary, self.hardware = state, boundary, hardware
        self.navigation, self.current_contract = navigation, current_contract
        self.provider_session_key_for_session = provider_session_key_for_session
        self.emit_page_opened, self.agreement_current = (
            emit_page_opened,
            agreement_current,
        )
        self.set_page, self.open_page = set_page, open_page

    def _page_grant(self, session) -> PageGrant:
        return PageGrant(
            self.state.identity.session_id,
            session.page_session_id,
            session.context_id,
            session.action_instance_id,
            session.generation,
        )

    async def handle(self, grant: BindingGrant, request):
        async with self.boundary:
            if (
                self.state.stopped
                or grant.device_session_id != self.state.identity.session_id
                or not self.state.navigation.config_active
                or not self.state.hardware_current(self.hardware)
            ):
                return None
            if isinstance(request, UpdatePage | ClosePage):
                session = self.state.navigation.active_dynamic_session()
                if (
                    session is None
                    or request.grant != self._page_grant(session)
                    or grant.action_instance_id != session.action_instance_id
                ):
                    return None
                key = self.provider_session_key_for_session(
                    provider_instance_id=session.owner_provider_instance_id,
                    provider_id=session.owner_provider_id,
                    provider_session_id=session.owner_provider_session_id,
                )
                if (
                    session.owner_contract is not None
                    and self.current_contract(key) != session.owner_contract
                ):
                    return None
                draft = self.state.navigation.begin_action_request(request)
                if draft is None:
                    return None
                ok, _ = await self.navigation._execute_page_draft(draft)
                if not ok:
                    return None
                if isinstance(request, ClosePage):
                    return True
                await self.emit_page_opened(session)
                return self._page_grant(session)
            lease = self.state.attachments.binding_leases.get(grant.binding_id)
            if (
                lease is None
                or lease.context_id != grant.context_id
                or not self.state.attachments.binding_command_authorized(lease)
                or not self.agreement_current(lease)
            ):
                return None
            if isinstance(request, Navigate):
                if grant.may_navigate:
                    return await self.set_page(
                        profile=request.profile, page=request.page
                    )
            elif isinstance(request, OpenPage):
                session = await self.open_page(
                    descriptor=request.definition,
                    context_id=grant.context_id,
                    binding_id=grant.binding_id,
                )
                return self._page_grant(session) if session is not None else None
            elif isinstance(request, SetTitle):
                if self.state.attachments.binding_output_authorized(lease):
                    await lease.context.set_title(request.text)
            elif isinstance(request, SetRasterImage):
                if self.state.attachments.binding_output_authorized(lease):
                    await lease.context.set_raster_image(request.image)
            return None


class ScopedActionRequests:
    def __init__(self, session, grant):
        self._submit = session.handle_action_request
        self._grant = grant

    async def request(self, request):
        return await self._submit(self._grant, request)
