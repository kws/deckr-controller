"""Per-device output revisions, coalescing and authorized hardware handoff."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, NamedTuple, Protocol

import anyio

from deckr.controller._render import RenderRequest, RenderResult
from deckr.controller._render_workers import RenderBackend, render_with_retry
from deckr.controller._state_gate import StateGate

if TYPE_CHECKING:
    from deckr.controller._hardware import HardwareCommandService
logger = logging.getLogger(__name__)
MAX_ACTIVE_RENDERS_PER_CONTROL = 2


class ControlOutput(Protocol):
    async def write(self, frame: bytes) -> None: ...

    async def clear(self) -> None: ...

    async def write_tagged(self, frame: bytes, message_id: str) -> None: ...

    async def clear_tagged(self, message_id: str) -> None: ...


@dataclass(slots=True)
class _ControlRenderState:
    generation: int = 0
    base_revision: int = 0
    context_id: str | None = None
    binding_id: str | None = None
    output: ControlOutput | None = None
    active_render_count: int = 0
    pending_request: RenderRequest | None = None
    io_lock: StateGate = field(default_factory=StateGate)
    scopes: set[anyio.CancelScope] = field(default_factory=set)


class RenderTicket(NamedTuple):
    control_id: str
    context_id: str
    binding_id: str
    base_revision: int


class RenderDispatcher:
    """Per-device dispatcher that enforces last-write-wins by control generation."""

    def __init__(
        self,
        *,
        command_service: HardwareCommandService,
        config_id: str,
        backend: RenderBackend,
        start_soon,
        result_authorizer: Callable[[str, str | None, str | None], bool] | None = None,
        completion_transaction: Callable | None = None,
    ):
        self._command_service = command_service
        self._config_id = config_id
        self._backend = backend
        self._start_soon = start_soon
        self._result_authorizer = result_authorizer
        self._completion_transaction = completion_transaction
        self._lock = StateGate()
        self._controls: dict[str, _ControlRenderState] = {}
        self._pending_clears: dict[str, int] = {}
        self._clearing: set[str] = set()

    def set_runner(self, start_soon) -> None:
        self._start_soon = start_soon

    @asynccontextmanager
    async def output_transaction(self):
        """Enter the application's presentation-result admission boundary."""
        if self._completion_transaction is None:
            yield
        else:
            async with self._completion_transaction():
                yield

    async def reserve_base(self, *, control_id, context_id, binding_id, output):
        """Admit a base update; even a later decoder failure invalidates old work."""
        async with self._lock:
            state = self._controls.setdefault(control_id, _ControlRenderState())
            state.base_revision += 1
            state.generation += 1
            state.context_id = context_id
            state.binding_id = binding_id
            state.output = output
            state.pending_request = None
            for scope in tuple(state.scopes):
                scope.cancel()
            return RenderTicket(control_id, context_id, binding_id, state.base_revision)

    def base_is_current(self, ticket):
        control_id, context_id, binding_id, revision = ticket
        state = self._controls.get(control_id)
        return bool(
            state is not None
            and (state.context_id, state.binding_id, state.base_revision)
            == (context_id, binding_id, revision)
            and (
                self._result_authorizer is None
                or self._result_authorizer(control_id, binding_id, context_id)
            )
        )

    def composition_revision(self, ticket):
        return self._controls[ticket[0]].generation

    async def publish_base(
        self,
        ticket,
        composition_revision,
        frame,
        *,
        message_id,
        authorize,
        committed,
        recompose,
    ):
        """Validate at ordered handoff, then send without holding device state.

        The per-control output lock orders dispatched bytes. A revoked queued
        result is refused before handoff. An initiated send cannot be recalled.
        """
        state = self._controls[ticket.control_id]
        async with state.io_lock:
            await authorize()
            async with self.output_transaction(), self._lock:
                if not self.base_is_current(ticket):
                    raise ValueError("superseded")
                if state.generation != composition_revision:
                    return False
                output = state.output
                if output is None:
                    raise ValueError("target_not_current")
            if frame is None:
                await output.clear_tagged(message_id)
            else:
                await output.write_tagged(frame, message_id)
            async with self.output_transaction():
                changed = False
                async with self._lock:
                    if self.base_is_current(ticket):
                        changed = state.generation != composition_revision
                        state.generation += 1
                        state.pending_request = None
                        committed()
                if changed:
                    # Overlay changes may commit while bytes are in flight.
                    # Recompose with the newly accepted base, invalidating the
                    # overlay render that still contained the previous base.
                    await recompose()
            return True

    async def submit_request(
        self,
        *,
        control_id: str,
        context_id: str,
        binding_id: str | None = None,
        request: RenderRequest | None,
        output: ControlOutput | None = None,
        base_update: bool = True,
    ) -> int:
        """Submit a request for a control, replacing any older pending work."""

        async with self._lock:
            state = self._controls.setdefault(control_id, _ControlRenderState())
            if state.binding_id != binding_id or state.context_id != context_id:
                for scope in tuple(state.scopes):
                    scope.cancel()
            if base_update:
                state.base_revision += 1
            state.generation += 1
            generation = state.generation
            state.context_id = context_id
            state.binding_id = binding_id
            if output is not None:
                state.output = output

            if request is None:
                state.pending_request = None
                logger.debug(
                    "Render dispatcher route claimed config=%s control=%s "
                    "binding=%s context=%s generation=%s",
                    self._config_id,
                    control_id,
                    binding_id,
                    context_id,
                    generation,
                )
                return generation

            request = replace(
                request,
                config_id=self._config_id,
                context_id=context_id,
                binding_id=binding_id,
                control_id=control_id,
                generation=generation,
            )
            if state.active_render_count >= MAX_ACTIVE_RENDERS_PER_CONTROL:
                replaced_pending = state.pending_request is not None
                state.pending_request = request
                logger.debug(
                    "Render dispatcher request queued config=%s control=%s "
                    "binding=%s context=%s generation=%s active=%s "
                    "replaced_pending=%s start_now=False",
                    self._config_id,
                    control_id,
                    binding_id,
                    context_id,
                    generation,
                    state.active_render_count,
                    replaced_pending,
                )
            else:
                state.active_render_count += 1
                self._start_soon(self._run_control, control_id, request)
                logger.debug(
                    "Render dispatcher request queued config=%s control=%s "
                    "binding=%s context=%s generation=%s active=%s "
                    "replaced_pending=False start_now=True",
                    self._config_id,
                    control_id,
                    binding_id,
                    context_id,
                    generation,
                    state.active_render_count,
                )
            return generation

    async def clear_control(
        self,
        control_id: str,
        *,
        context_id: str | None = None,
        binding_id: str | None = None,
        output: ControlOutput | None = None,
        clear_output: bool = True,
        base_update: bool = True,
    ) -> int:
        """Invalidate queued/running renders for a control and clear its output."""

        async with self._lock:
            state = self._controls.setdefault(control_id, _ControlRenderState())
            if base_update:
                state.base_revision += 1
            state.generation += 1
            generation = state.generation
            if context_id is not None:
                state.context_id = context_id
            state.binding_id = binding_id
            if output is not None:
                state.output = output
            state.pending_request = None
            for scope in tuple(state.scopes):
                scope.cancel()

        logger.debug(
            "Render dispatcher clear queued config=%s control=%s binding=%s "
            "context=%s generation=%s clear_output=%s",
            self._config_id,
            control_id,
            binding_id,
            context_id,
            generation,
            clear_output,
        )

        if clear_output:
            self._pending_clears[control_id] = generation
            if control_id not in self._clearing:
                self._clearing.add(control_id)
                self._start_soon(self._clear_output, control_id)
        return generation

    async def _clear_output(self, control_id: str) -> None:
        try:
            while control_id in self._pending_clears:
                generation = self._pending_clears.pop(control_id)
                try:
                    state = self._controls[control_id]
                    async with state.io_lock:
                        async with self.output_transaction(), self._lock:
                            if state.generation != generation:
                                continue
                            if (
                                self._result_authorizer is not None
                                and not self._result_authorizer(
                                    control_id, state.binding_id, state.context_id
                                )
                            ):
                                continue
                            output = state.output
                        if output is not None:
                            with anyio.fail_after(2):
                                await output.clear()
                except Exception:
                    logger.warning(
                        "Hardware clear failed for %s", control_id, exc_info=True
                    )
        finally:
            self._clearing.discard(control_id)

    async def _run_control(self, control_id: str, request: RenderRequest) -> None:
        state = self._controls[control_id]
        current = request
        try:
            while True:
                with anyio.CancelScope() as scope:
                    state.scopes.add(scope)
                    try:
                        result = await render_with_retry(
                            self._backend,
                            current,
                            is_current=lambda current=current: state.generation
                            == current.generation,
                        )
                        if result is not None:
                            await self._apply_result(result)
                    except Exception:
                        logger.warning(
                            "Render or output failed for %s", control_id, exc_info=True
                        )
                    finally:
                        state.scopes.discard(scope)
                async with self._lock:
                    current = state.pending_request
                    state.pending_request = None
                    if current is None:
                        return
        finally:
            with anyio.CancelScope(shield=True):
                async with self._lock:
                    state.active_render_count = max(0, state.active_render_count - 1)

    async def _apply_result(self, result: RenderResult) -> None:
        async with self._lock:
            state = self._controls.get(result.control_id)
            if state is None:
                return
            io_lock = state.io_lock
            target_output = state.output

        async with io_lock:
            async with self.output_transaction(), self._lock:
                state = self._controls.get(result.control_id)
                if state is None:
                    return
                if state.generation != result.generation:
                    return
                if state.context_id != result.context_id:
                    return
                if state.binding_id != result.binding_id:
                    return
                if self._result_authorizer is not None and not (
                    self._result_authorizer(
                        result.control_id,
                        result.binding_id,
                        result.context_id,
                    )
                ):
                    return
                target_output = state.output

            if result.frame is None:
                return
            if target_output is not None:
                with anyio.fail_after(2):
                    await target_output.write(result.frame)
