"""One action lifecycle/input interface with local and managed-service adapters."""

from typing import Protocol

from deckr.actions.messages import (
    BINDING_ATTACHED,
    BINDING_DETACHED,
    CAPABILITY_INPUT,
    BindingAttachedBody,
    BindingDetachedBody,
    CapabilityInputBody,
)


class ActionAdapter(Protocol):
    async def attach(self) -> None: ...
    async def detach(self, reason: str) -> None: ...
    async def input(self, event) -> None: ...


class LocalActionAdapter:
    def __init__(self, action, context):
        self.action = action
        self.context = context

    async def attach(self):
        await self.action.on_bind(self.context)

    async def detach(self, reason):
        await self.action.on_unbind(self.context, reason)

    async def input(self, event):
        await self.action.on_input(self.context, event)


class RemoteActionAdapter:
    def __init__(self, binding, publish, attachment):
        self.binding = binding
        self.publish = publish
        self.attachment = attachment

    async def attach(self):
        await self.publish(BINDING_ATTACHED, BindingAttachedBody(**self.attachment()))

    async def detach(self, reason):
        await self.publish(
            BINDING_DETACHED, BindingDetachedBody(binding=self.binding, reason=reason)
        )

    async def input(self, event):
        await self.publish(
            CAPABILITY_INPUT, CapabilityInputBody(binding=self.binding, event=event)
        )


class ActionGateway:
    """Invoked only as an ordered session effect, never from a state transition."""

    def __init__(self, adapter: ActionAdapter):
        self.adapter = adapter

    async def attach(self):
        await self.adapter.attach()

    async def detach(self, reason):
        await self.adapter.detach(reason)

    async def input(self, event):
        await self.adapter.input(event)
