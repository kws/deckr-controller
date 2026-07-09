"""Builtin action metadata resolver for the controller runtime."""

from __future__ import annotations

from collections.abc import Mapping

from deckr.actions.endpoints import (
    BUILTIN_ACTION_PROVIDER_ID,
    RESERVED_BUILTIN_PROVIDER_IDS,
)
from deckr.actions.messages import ActionDescriptor
from deckr.components import BaseComponent, RunContext
from deckr.contracts.models import thaw_json

from deckr.controller._actions import ActionMetadata
from deckr.controller.action_provider.builtin import (
    BuiltinAction,
    BuiltinRegistry,
)


class ActionRegistry(BaseComponent):
    """Resolve controller-builtin actions.

    External provider actions are resolved through ``ControllerActionService``.
    """

    def __init__(
        self,
        *,
        controller_id: str,
    ):
        super().__init__(name="ActionRegistry")
        del controller_id
        self._builtin_registry = BuiltinRegistry()
        self._builtin_action_registry: dict[str, ActionDescriptor] = {}

    async def get_action(
        self,
        address: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
    ) -> ActionMetadata | None:
        if provider_labels:
            return None
        if "::" in address:
            qualified_provider_id, _, action_uuid = address.partition("::")
            if (
                provider_instance_id is not None
                and qualified_provider_id != provider_instance_id
            ):
                return None
            if qualified_provider_id not in RESERVED_BUILTIN_PROVIDER_IDS:
                return None
            return self._builtin_action_metadata(action_uuid)
        if provider_instance_id is not None and (
            provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS
        ):
            return None
        return self._builtin_action_metadata(address)

    async def get_action_descriptor(
        self,
        address: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
    ) -> ActionDescriptor | None:
        meta = await self.get_action(
            address,
            provider_instance_id=provider_instance_id,
            provider_labels=provider_labels,
        )
        if meta is None:
            return None
        return self._builtin_action_registry.get(meta.uuid)

    def get_builtin_action(self, uuid: str) -> BuiltinAction | None:
        return self._builtin_registry.get_action(uuid)

    async def start(self, ctx: RunContext) -> None:
        del ctx
        self._builtin_action_registry.clear()
        for action_uuid in self._builtin_registry.provides_actions():
            descriptor = self._builtin_registry.get_action_descriptor(action_uuid)
            if descriptor:
                self._builtin_action_registry[action_uuid] = descriptor

    async def stop(self) -> None:
        self._builtin_action_registry.clear()

    def _builtin_action_metadata(self, action_uuid: str) -> ActionMetadata | None:
        descriptor = self._builtin_action_registry.get(action_uuid)
        if descriptor is None:
            return None
        return _metadata(descriptor)


def _metadata(descriptor: ActionDescriptor) -> ActionMetadata:
    return ActionMetadata(
        uuid=descriptor.action_id,
        provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
        provider_id=descriptor.provider_id or BUILTIN_ACTION_PROVIDER_ID,
        name=descriptor.name,
        provider_labels={},
        settings_schema=(
            thaw_json(descriptor.settings_schema)
            if descriptor.settings_schema is not None
            else None
        ),
        provider_settings_schema=(
            thaw_json(descriptor.provider_settings_schema)
            if descriptor.provider_settings_schema is not None
            else None
        ),
    )
