from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from deckr.actions.messages import (
    SettingsProvenance,
    SettingsSchemaMetadata,
    SettingsTargetDescription,
    SettingsTargetRef,
)
from deckr.contracts.models import thaw_json

from deckr.controller._actions._models import ActionMetadata, SettingsActionMetadata
from deckr.controller.config import DeviceConfigService
from deckr.controller.config._data import Control, DeviceConfig
from deckr.controller.settings._identity import (
    derive_static_action_instance_id,
)
from deckr.controller.settings._models import SettingsSnapshot

ActionProvider = Callable[..., Awaitable[ActionMetadata | None]]


class ActionMetadataResolver(Protocol):
    def settings_action_metadata(
        self,
        action_uuid: str,
        *,
        provider_instance_id: str | None = None,
        provider_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
        now: float | None = None,
    ) -> SettingsActionMetadata: ...


@dataclass(frozen=True, slots=True)
class _ControlLocation:
    profile_id: str
    profile_index: int
    page_id: str
    page_index: int
    control_index: int
    control: Control
    action_instance_id: str


class SettingsService(Protocol):
    async def list_targets(
        self, *, config_id: str
    ) -> tuple[SettingsTargetDescription, ...]: ...
    async def describe_target(
        self, target: SettingsTargetRef
    ) -> SettingsTargetDescription: ...
    async def exists(self, target: SettingsTargetRef) -> bool: ...
    async def get(self, target: SettingsTargetRef) -> SettingsSnapshot: ...


def _settings_copy(value: Mapping[str, Any] | None) -> dict[str, Any]:
    copied = thaw_json(value or {})
    return dict(copied) if isinstance(copied, dict) else {}


def _action_settings_from_control(control: Control) -> dict[str, Any]:
    settings = _settings_copy(control.settings)
    if control.template_overrides:
        settings["templateOverrides"] = _settings_copy(control.template_overrides)
    return settings


def _schema_metadata(
    metadata: SettingsActionMetadata,
    *,
    scope: str,
) -> SettingsSchemaMetadata:
    action = metadata.action
    schema = None
    if action is not None:
        schema = (
            action.provider_settings_schema
            if scope == "action_provider_instance"
            else action.settings_schema
        )
    return SettingsSchemaMetadata(
        schema=schema,
        stale=metadata.stale or schema is None,
    )


class ConfigBackedSettingsService:
    """Read-only settings snapshots backed by controller-owned device config YAML."""

    def __init__(
        self,
        *,
        controller_id: str,
        config_service: DeviceConfigService,
        action_provider: ActionProvider | None = None,
        availability_service: ActionMetadataResolver | None = None,
    ) -> None:
        self._controller_id = controller_id
        self._config_service = config_service
        self._action_provider = action_provider
        self._availability_service = availability_service

    async def list_targets(
        self, *, config_id: str
    ) -> tuple[SettingsTargetDescription, ...]:
        config = await self._require_config(config_id)
        descriptions: list[SettingsTargetDescription] = []
        provider_targets: dict[tuple[str, str], SettingsTargetRef] = {}
        for location in self._control_locations(config):
            metadata = await self._action_metadata_for_control(location.control)
            action = metadata.action
            provider_instance_id, provider_id = self._provider_ids_for_control(
                location.control,
                action,
            )
            if provider_instance_id is None or provider_id is None:
                continue
            provider_targets[(provider_instance_id, provider_id)] = self.provider_target(
                config,
                provider_instance_id=provider_instance_id,
                provider_id=provider_id,
            )
            descriptions.append(
                await self.describe_target(
                    self.action_target(
                        config,
                        location,
                        provider_instance_id=provider_instance_id,
                        provider_id=provider_id,
                    )
                )
            )
        for target in provider_targets.values():
            descriptions.append(await self.describe_target(target))
        return tuple(descriptions)

    async def describe_target(
        self, target: SettingsTargetRef
    ) -> SettingsTargetDescription:
        config = await self._require_config(target.config_id)
        if target.scope == "action_provider_instance":
            metadata = await self._action_for_target(target)
            return SettingsTargetDescription(
                target=target,
                providerInstanceId=target.provider_instance_id,
                providerId=target.provider_id,
                label=target.provider_id,
                schemaMetadata=_schema_metadata(
                    metadata,
                    scope="action_provider_instance",
                ),
                provenance=("config_default",),
            )
        location, action = await self._find_verified_control(config, target)
        return SettingsTargetDescription(
            target=target,
            providerInstanceId=target.provider_instance_id,
            providerId=target.provider_id,
            actionId=target.action_id,
            label=location.control.id or location.control.selector.label,
            placement={
                "profile": location.profile_id,
                "page": location.page_id,
                "control": location.control.selector.control_id,
            },
            schemaMetadata=_schema_metadata(action, scope="action_instance"),
            provenance=self._action_provenance(location.control),
        )

    async def exists(self, target: SettingsTargetRef) -> bool:
        try:
            await self.get(target)
        except KeyError:
            return False
        return True

    async def get(self, target: SettingsTargetRef) -> SettingsSnapshot:
        config = await self._require_config(target.config_id)
        if target.scope == "action_provider_instance":
            metadata = await self._action_for_target(target)
            settings = _settings_copy(
                config.provider_settings.get(target.provider_instance_id)
            )
            return SettingsSnapshot(
                target=target,
                settings=settings,
                provenance=("config_default",),
                schemaMetadata=_schema_metadata(
                    metadata,
                    scope="action_provider_instance",
                ),
            )
        location, metadata = await self._find_verified_control(config, target)
        return SettingsSnapshot(
            target=target,
            settings=_action_settings_from_control(location.control),
            provenance=self._action_provenance(location.control),
            schemaMetadata=_schema_metadata(metadata, scope="action_instance"),
        )

    def provider_target(
        self,
        config: DeviceConfig,
        *,
        provider_instance_id: str,
        provider_id: str,
    ) -> SettingsTargetRef:
        return SettingsTargetRef(
            scope="action_provider_instance",
            controllerId=self._controller_id,
            configId=config.id,
            providerInstanceId=provider_instance_id,
            providerId=provider_id,
        )

    def action_target(
        self,
        config: DeviceConfig,
        location: _ControlLocation,
        *,
        provider_instance_id: str,
        provider_id: str,
    ) -> SettingsTargetRef:
        return SettingsTargetRef(
            scope="action_instance",
            controllerId=self._controller_id,
            configId=config.id,
            providerInstanceId=provider_instance_id,
            providerId=provider_id,
            actionId=location.control.action,
            actionInstanceId=location.action_instance_id,
            stableId=location.control.id,
        )

    async def _require_config(self, config_id: str) -> DeviceConfig:
        config = await self._config_service.get_config(config_id)
        if config is None:
            raise KeyError(f"Unknown device config {config_id!r}")
        return config

    async def _action_metadata(
        self,
        action_id: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
        provider_id: str | None = None,
    ) -> SettingsActionMetadata:
        if self._availability_service is not None:
            return self._availability_service.settings_action_metadata(
                action_id,
                provider_instance_id=provider_instance_id,
                provider_id=provider_id,
                provider_labels=provider_labels,
            )
        if self._action_provider is None:
            return SettingsActionMetadata(action=None, stale=True)
        action = await self._action_provider(
            action_id,
            provider_instance_id=provider_instance_id,
            provider_labels=provider_labels,
        )
        if (
            action is not None
            and provider_id is not None
            and action.provider_id != provider_id
        ):
            action = None
        return SettingsActionMetadata(action=action, stale=action is None)

    async def _action_metadata_for_control(
        self,
        control: Control,
    ) -> SettingsActionMetadata:
        return await self._action_metadata(
            control.action,
            provider_instance_id=control.provider_instance_id,
            provider_labels=control.provider_labels,
        )

    async def _action_for_target(
        self, target: SettingsTargetRef
    ) -> SettingsActionMetadata:
        if target.scope == "action_provider_instance":
            config = await self._require_config(target.config_id)
            for location in self._control_locations(config):
                metadata = await self._action_metadata_for_control(location.control)
                action = metadata.action
                if (
                    action is not None
                    and action.provider_instance_id == target.provider_instance_id
                    and action.provider_id == target.provider_id
                ):
                    return metadata
            return SettingsActionMetadata(action=None, stale=True)
        if not target.action_id:
            return SettingsActionMetadata(action=None, stale=True)
        return await self._action_metadata(
            target.action_id,
            provider_instance_id=target.provider_instance_id,
            provider_id=target.provider_id,
        )

    def _provider_ids_for_control(
        self,
        control: Control,
        action: ActionMetadata | None,
    ) -> tuple[str | None, str | None]:
        provider_instance_id = (
            action.provider_instance_id
            if action is not None
            else control.provider_instance_id
        )
        provider_id = action.provider_id if action is not None else None
        return provider_instance_id, provider_id

    def _control_locations(self, config: DeviceConfig) -> tuple[_ControlLocation, ...]:
        locations: list[_ControlLocation] = []
        for profile_index, profile in enumerate(config.profiles):
            for page_index, page in enumerate(profile.pages):
                page_id = str(page_index)
                for control_index, control in enumerate(page.controls):
                    locations.append(
                        _ControlLocation(
                            profile_id=profile.name,
                            profile_index=profile_index,
                            page_id=page_id,
                            page_index=page_index,
                            control_index=control_index,
                            control=control,
                            action_instance_id=derive_static_action_instance_id(
                                controller_id=self._controller_id,
                                config_id=config.id,
                                action_id=control.action,
                                stable_id=control.id,
                                profile_id=profile.name,
                                page_id=page_id,
                                selector_control_id=control.selector.control_id,
                                control_index=control_index,
                            ),
                        )
                    )
        return tuple(locations)

    def _find_control(
        self,
        config: DeviceConfig,
        target: SettingsTargetRef,
    ) -> _ControlLocation:
        for location in self._control_locations(config):
            if location.action_instance_id == target.action_instance_id:
                return location
        raise KeyError(f"Unknown action settings target {target.key()!r}")

    async def _find_verified_control(
        self,
        config: DeviceConfig,
        target: SettingsTargetRef,
    ) -> tuple[_ControlLocation, SettingsActionMetadata]:
        location = self._find_control(config, target)
        if target.action_id != location.control.action:
            raise KeyError(f"Unknown action settings target {target.key()!r}")
        if target.stable_id != location.control.id:
            raise KeyError(f"Unknown action settings target {target.key()!r}")
        metadata = await self._action_metadata(
            location.control.action,
            provider_instance_id=target.provider_instance_id,
            provider_id=target.provider_id,
            provider_labels=location.control.provider_labels,
        )
        return location, metadata

    def _action_provenance(self, control: Control) -> tuple[SettingsProvenance, ...]:
        provenance: list[SettingsProvenance] = ["config_default"]
        if control.template_overrides:
            provenance.append("template_override")
        return tuple(provenance)
