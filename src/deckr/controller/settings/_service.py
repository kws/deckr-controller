from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
from deckr.actions.messages import (
    SettingsProvenance,
    SettingsSchemaMetadata,
    SettingsSnapshot,
    SettingsTargetDescription,
    SettingsTargetRef,
)
from deckr.contracts.models import thaw_json
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema

from deckr.controller.action_provider.provider import ActionMetadata
from deckr.controller.config import DeviceConfigService
from deckr.controller.config._data import Control, DeviceConfig
from deckr.controller.settings._identity import derive_action_instance_id

logger = logging.getLogger(__name__)

ActionProvider = Callable[..., Awaitable[ActionMetadata | None]]


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
    async def patch(
        self, target: SettingsTargetRef, patch: Mapping[str, Any]
    ) -> SettingsSnapshot: ...
    async def replace(
        self, target: SettingsTargetRef, settings: Mapping[str, Any]
    ) -> SettingsSnapshot: ...
    def subscribe(self, target: SettingsTargetRef) -> AsyncIterator[SettingsSnapshot]: ...
    async def ensure_service_managed_id(
        self, target: SettingsTargetRef
    ) -> SettingsTargetRef: ...


def _settings_copy(value: Mapping[str, Any] | None) -> dict[str, Any]:
    copied = thaw_json(value or {})
    return dict(copied) if isinstance(copied, dict) else {}


def _action_settings_from_control(control: Control) -> dict[str, Any]:
    settings = _settings_copy(control.settings)
    if control.template_overrides:
        settings["templateOverrides"] = _settings_copy(control.template_overrides)
    return settings


def _split_action_settings(
    settings: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    copied = _settings_copy(settings)
    raw_template_overrides = copied.pop("templateOverrides", {})
    template_overrides = (
        _settings_copy(raw_template_overrides)
        if isinstance(raw_template_overrides, Mapping)
        else {}
    )
    return copied, template_overrides


def _schema_metadata(
    action: ActionMetadata | None,
    *,
    scope: str,
) -> SettingsSchemaMetadata:
    schema = None
    if action is not None:
        schema = (
            action.provider_settings_schema
            if scope == "action_provider_instance"
            else action.settings_schema
        )
    return SettingsSchemaMetadata(
        schema=schema,
        stale=schema is None,
    )


def _validate_settings(
    settings: Mapping[str, Any],
    metadata: SettingsSchemaMetadata,
) -> None:
    if metadata.json_schema is None:
        return
    try:
        validate_json_schema(
            instance=thaw_json(settings),
            schema=thaw_json(metadata.json_schema),
        )
    except JsonSchemaValidationError as exc:
        raise ValueError(f"settings failed schema validation: {exc.message}") from exc


class ConfigBackedSettingsService:
    """Settings API backed by controller-owned device config YAML."""

    def __init__(
        self,
        *,
        controller_id: str,
        config_service: DeviceConfigService,
        action_provider: ActionProvider | None = None,
    ) -> None:
        self._controller_id = controller_id
        self._config_service = config_service
        self._action_provider = action_provider
        self._subscribers: dict[
            str, set[anyio.abc.ObjectSendStream[SettingsSnapshot]]
        ] = {}
        self._lock = anyio.Lock()

    async def list_targets(
        self, *, config_id: str
    ) -> tuple[SettingsTargetDescription, ...]:
        config = await self._require_config(config_id)
        descriptions: list[SettingsTargetDescription] = []
        provider_targets: dict[tuple[str, str], SettingsTargetRef] = {}
        for location in self._control_locations(config):
            action = await self._action_metadata_for_control(location.control)
            if action is None:
                continue
            provider_targets[(action.provider_instance_id, action.provider_id)] = (
                self.provider_target(
                    config,
                    provider_instance_id=action.provider_instance_id,
                    provider_id=action.provider_id,
                )
            )
            descriptions.append(
                await self.describe_target(
                    self.action_target(
                        config,
                        location,
                        provider_instance_id=action.provider_instance_id,
                        provider_id=action.provider_id,
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
            action = await self._action_for_target(target)
            return SettingsTargetDescription(
                target=target,
                providerInstanceId=target.provider_instance_id,
                providerId=target.provider_id,
                label=target.provider_id,
                schemaMetadata=_schema_metadata(
                    action,
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
            action = await self._action_for_target(target)
            settings = _settings_copy(
                config.provider_settings.get(target.provider_instance_id)
            )
            metadata = _schema_metadata(action, scope="action_provider_instance")
            return SettingsSnapshot(
                target=target,
                settings=settings,
                provenance=("config_default",),
                schemaMetadata=metadata,
            )
        location, action = await self._find_verified_control(config, target)
        metadata = _schema_metadata(action, scope="action_instance")
        return SettingsSnapshot(
            target=target,
            settings=_action_settings_from_control(location.control),
            provenance=self._action_provenance(location.control),
            schemaMetadata=metadata,
        )

    async def patch(
        self, target: SettingsTargetRef, patch: Mapping[str, Any]
    ) -> SettingsSnapshot:
        current = await self.get(target)
        merged = _settings_copy(current.settings)
        merged.update(_settings_copy(patch))
        return await self.replace(target, merged)

    async def replace(
        self, target: SettingsTargetRef, settings: Mapping[str, Any]
    ) -> SettingsSnapshot:
        config = await self._require_config(target.config_id)
        if target.scope == "action_provider_instance":
            location = None
            action = await self._action_for_target(target)
        else:
            location, action = await self._find_verified_control(config, target)
        metadata = _schema_metadata(action, scope=target.scope)
        next_settings = _settings_copy(settings)
        _validate_settings(next_settings, metadata)
        if target.scope == "action_provider_instance":
            provider_settings = dict(config.provider_settings)
            provider_settings[target.provider_instance_id] = next_settings
            next_config = config.model_copy(
                update={"provider_settings": provider_settings}
            )
        else:
            action_settings, template_overrides = _split_action_settings(next_settings)
            next_config = self._replace_control(
                config,
                location,
                settings=action_settings,
                template_overrides=template_overrides,
            )
        await self._config_service.write_config(next_config)
        snapshot = await self.get(target)
        await self._notify(target, snapshot)
        return snapshot

    def subscribe(self, target: SettingsTargetRef) -> AsyncIterator[SettingsSnapshot]:
        return self._subscribe_impl(target)

    async def ensure_service_managed_id(
        self, target: SettingsTargetRef
    ) -> SettingsTargetRef:
        if target.scope != "action_instance":
            return target
        if target.stable_id:
            return target
        config = await self._require_config(target.config_id)
        location, _ = await self._find_verified_control(config, target)
        managed_id = self._new_managed_id(config)
        next_config = self._replace_control(
            config,
            location,
            stable_id=managed_id,
        )
        await self._config_service.write_config(next_config)
        next_action_instance_id = derive_action_instance_id(
            controller_id=self._controller_id,
            config_id=config.id,
            action_id=location.control.action,
            stable_id=managed_id,
        )
        return SettingsTargetRef(
            scope="action_instance",
            controllerId=self._controller_id,
            configId=config.id,
            providerInstanceId=target.provider_instance_id,
            providerId=target.provider_id,
            actionId=target.action_id,
            actionInstanceId=next_action_instance_id,
            stableId=managed_id,
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
    ) -> ActionMetadata | None:
        if self._action_provider is None:
            return None
        return await self._action_provider(
            action_id,
            provider_instance_id=provider_instance_id,
            provider_labels=provider_labels,
        )

    async def _action_metadata_for_control(
        self,
        control: Control,
    ) -> ActionMetadata | None:
        return await self._action_metadata(
            control.action,
            provider_instance_id=control.provider_instance_id,
            provider_labels=control.provider_labels,
        )

    async def _action_for_target(self, target: SettingsTargetRef) -> ActionMetadata | None:
        if target.scope == "action_provider_instance":
            config = await self._require_config(target.config_id)
            for location in self._control_locations(config):
                action = await self._action_metadata_for_control(location.control)
                if (
                    action is not None
                    and action.provider_instance_id == target.provider_instance_id
                    and action.provider_id == target.provider_id
                ):
                    return action
            return None
        if not target.action_id:
            return None
        return await self._action_metadata(
            target.action_id,
            provider_instance_id=target.provider_instance_id,
        )

    def _control_locations(self, config: DeviceConfig) -> tuple[_ControlLocation, ...]:
        locations: list[_ControlLocation] = []
        for profile_index, profile in enumerate(config.profiles):
            for page_index, page in enumerate(profile.pages):
                page_id = str(page_index)
                for control_index, control in enumerate(page.controls):
                    control_id = control.selector.control_id or str(control_index)
                    locations.append(
                        _ControlLocation(
                            profile_id=profile.name,
                            profile_index=profile_index,
                            page_id=page_id,
                            page_index=page_index,
                            control_index=control_index,
                            control=control,
                            action_instance_id=derive_action_instance_id(
                                controller_id=self._controller_id,
                                config_id=config.id,
                                action_id=control.action,
                                stable_id=control.id,
                                profile_id=profile.name,
                                page_id=page_id,
                                control_id=control_id,
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
    ) -> tuple[_ControlLocation, ActionMetadata | None]:
        location = self._find_control(config, target)
        action = await self._action_metadata_for_control(location.control)
        if target.action_id != location.control.action:
            raise KeyError(f"Unknown action settings target {target.key()!r}")
        if target.stable_id != location.control.id:
            raise KeyError(f"Unknown action settings target {target.key()!r}")
        if action is not None and (
            target.provider_instance_id != action.provider_instance_id
            or target.provider_id != action.provider_id
        ):
            raise KeyError(f"Unknown action settings target {target.key()!r}")
        return location, action

    def _replace_control(
        self,
        config: DeviceConfig,
        location: _ControlLocation,
        *,
        settings: Mapping[str, Any] | None = None,
        stable_id: str | None = None,
        template_overrides: Mapping[str, Any] | None = None,
    ) -> DeviceConfig:
        profiles = list(config.profiles)
        profile = profiles[location.profile_index]
        pages = list(profile.pages)
        page = pages[location.page_index]
        controls = list(page.controls)
        control = controls[location.control_index]
        update: dict[str, Any] = {}
        if settings is not None:
            update["settings"] = _settings_copy(settings)
        if stable_id is not None:
            update["id"] = stable_id
        if template_overrides is not None:
            update["template_overrides"] = _settings_copy(template_overrides)
        controls[location.control_index] = control.model_copy(update=update)
        pages[location.page_index] = page.model_copy(update={"controls": controls})
        profiles[location.profile_index] = profile.model_copy(update={"pages": pages})
        return config.model_copy(update={"profiles": profiles})

    def _new_managed_id(self, config: DeviceConfig) -> str:
        existing = {
            control.id
            for profile in config.profiles
            for page in profile.pages
            for control in page.controls
            if control.id is not None
        }
        while True:
            candidate = "managed-" + secrets.token_hex(6)
            if candidate not in existing:
                return candidate

    def _action_provenance(
        self, control: Control
    ) -> tuple[SettingsProvenance, ...]:
        provenance: list[SettingsProvenance] = ["config_default"]
        if control.template_overrides:
            provenance.append("template_override")
        return tuple(provenance)

    async def _subscribe_impl(
        self, target: SettingsTargetRef
    ) -> AsyncIterator[SettingsSnapshot]:
        initial = await self.get(target)
        send, receive = anyio.create_memory_object_stream[SettingsSnapshot](
            max_buffer_size=32
        )
        key = target.key()
        async with self._lock:
            self._subscribers.setdefault(key, set()).add(send)
        await send.send(initial)

        try:
            async for value in receive:
                yield value
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(key)
                if subscribers is not None:
                    subscribers.discard(send)
                    if not subscribers:
                        self._subscribers.pop(key, None)
            await send.aclose()

    async def _notify(self, target: SettingsTargetRef, snapshot: SettingsSnapshot) -> None:
        async with self._lock:
            subscribers = set(self._subscribers.get(target.key(), set()))
        for send in subscribers:
            try:
                await send.send(snapshot)
            except Exception:
                logger.exception("Failed to notify settings subscriber")
