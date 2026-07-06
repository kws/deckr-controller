"""Tests for the controller settings service boundary."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import anyio
import pytest
import yaml
from deckr.actions.messages import SettingsTargetRef
from pydantic import ValidationError

from deckr.controller._settings_metadata import SettingsActionMetadata
from deckr.controller.action_provider.provider import ActionMetadata
from deckr.controller.config import (
    Control,
    DeviceConfig,
    DeviceConfigMatch,
    FileBackedDeviceConfigService,
    Page,
    Profile,
)
from deckr.controller.settings import (
    ConfigBackedSettingsService,
    derive_action_instance_id,
)

CONTROLLER_ID = "controller-main"
CONFIG_ID = "config-1"
PROVIDER_INSTANCE_ID = "python-dev.deckr.clock"
PROVIDER_ID = "dev.deckr.clock"


def _config() -> DeviceConfig:
    return DeviceConfig(
        id=CONFIG_ID,
        name="Desk",
        match=DeviceConfigMatch(fingerprint="fingerprint:desk"),
        provider_settings={PROVIDER_INSTANCE_ID: {"timezone": "UTC"}},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                id="clock-primary",
                                selector={"control_id": "0,0", "label": "Clock"},
                                action="action.clock",
                                settings={"mode": "time"},
                                template_overrides={"title": "Clock"},
                            ),
                            Control(
                                selector={"control_id": "0,1"},
                                action="action.no_schema",
                                settings={},
                            ),
                        ]
                    )
                ],
            )
        ],
    )


async def _action_provider(
    action_id: str,
    *,
    provider_instance_id: str | None = None,
    provider_labels: dict[str, str] | None = None,
) -> ActionMetadata | None:
    del provider_labels
    if provider_instance_id not in {None, PROVIDER_INSTANCE_ID}:
        return None
    if action_id == "action.clock":
        return ActionMetadata(
            uuid="action.clock",
            name="Clock",
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            settings_schema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string"},
                    "templateOverrides": {"type": "object"},
                },
                "additionalProperties": False,
            },
            provider_settings_schema={
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
                "additionalProperties": False,
            },
        )
    if action_id == "action.no_schema":
        return ActionMetadata(
            uuid="action.no_schema",
            name="No Schema",
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id="no_schema",
        )
    return None


async def _service(
    tmp_path: Path,
) -> tuple[ConfigBackedSettingsService, FileBackedDeviceConfigService]:
    config_service = FileBackedDeviceConfigService(config_dir=tmp_path)
    await config_service.write_config(_config())
    settings_service = ConfigBackedSettingsService(
        controller_id=CONTROLLER_ID,
        config_service=config_service,
        action_provider=_action_provider,
    )
    return settings_service, config_service


def _stable_action_target() -> SettingsTargetRef:
    return SettingsTargetRef(
        scope="action_instance",
        controllerId=CONTROLLER_ID,
        configId=CONFIG_ID,
        providerInstanceId=PROVIDER_INSTANCE_ID,
        providerId=PROVIDER_ID,
        actionId="action.clock",
        actionInstanceId=derive_action_instance_id(
            controller_id=CONTROLLER_ID,
            config_id=CONFIG_ID,
            action_id="action.clock",
            stable_id="clock-primary",
        ),
        stableId="clock-primary",
    )


def _provider_target(
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    provider_id: str = PROVIDER_ID,
) -> SettingsTargetRef:
    return SettingsTargetRef(
        scope="action_provider_instance",
        controllerId=CONTROLLER_ID,
        configId=CONFIG_ID,
        providerInstanceId=provider_instance_id,
        providerId=provider_id,
    )


def _target_with(**updates: Any) -> SettingsTargetRef:
    data = _stable_action_target().to_dict()
    data.update(updates)
    return SettingsTargetRef.model_validate(data)


async def _next_with_timeout(stream: AsyncIterator[Any]) -> Any:
    with anyio.fail_after(1):
        return await anext(stream)


@pytest.mark.asyncio
async def test_action_settings_patch_writes_yaml_and_notifies(
    tmp_path: Path,
) -> None:
    service, _ = await _service(tmp_path)
    target = _stable_action_target()

    stream = service.subscribe(target)
    initial = await _next_with_timeout(stream)
    assert initial.settings == {
        "mode": "time",
        "templateOverrides": {"title": "Clock"},
    }

    patched = await service.patch(
        target,
        {"mode": "date", "templateOverrides": {"title": "Today"}},
    )
    assert patched.settings == {
        "mode": "date",
        "templateOverrides": {"title": "Today"},
    }

    emitted = await _next_with_timeout(stream)
    assert emitted.settings == patched.settings
    await stream.aclose()

    data = yaml.safe_load((tmp_path / f"{CONFIG_ID}.yml").read_text())
    control = data["profiles"][0]["pages"][0]["controls"][0]
    assert control["settings"] == {"mode": "date"}
    assert control["template_overrides"] == {"title": "Today"}


@pytest.mark.asyncio
async def test_schema_validation_rejects_invalid_settings(tmp_path: Path) -> None:
    service, config_service = await _service(tmp_path)

    with pytest.raises(ValueError, match="settings failed schema validation"):
        await service.patch(_stable_action_target(), {"mode": 42})

    reloaded = await config_service.get_config(CONFIG_ID)
    assert reloaded is not None
    assert reloaded.profiles[0].pages[0].controls[0].settings == {"mode": "time"}


@pytest.mark.asyncio
async def test_settings_metadata_reads_from_action_availability_service(
    tmp_path: Path,
) -> None:
    config_service = FileBackedDeviceConfigService(config_dir=tmp_path)
    await config_service.write_config(_config())
    availability = MagicMock()
    availability.settings_action_metadata.return_value = SettingsActionMetadata(
        action=ActionMetadata(
            uuid="action.clock",
            name="Clock",
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            settings_schema={"type": "object"},
            provider_settings_schema={"type": "object"},
        ),
        stale=False,
    )
    service = ConfigBackedSettingsService(
        controller_id=CONTROLLER_ID,
        config_service=config_service,
        availability_service=availability,
    )

    snapshot = await service.get(_stable_action_target())

    assert snapshot.schema_metadata.stale is False
    assert snapshot.schema_metadata.json_schema == {"type": "object"}
    availability.settings_action_metadata.assert_called_once_with(
        "action.clock",
        provider_instance_id=PROVIDER_INSTANCE_ID,
        provider_id=PROVIDER_ID,
        provider_labels={},
    )


@pytest.mark.asyncio
async def test_service_managed_id_insertion_uses_managed_id_and_stable_identity(
    tmp_path: Path,
) -> None:
    service, config_service = await _service(tmp_path)
    old_target = next(
        description.target
        for description in await service.list_targets(config_id=CONFIG_ID)
        if description.target.action_id == "action.no_schema"
    )
    old_action_instance_id = old_target.action_instance_id

    new_target = await service.ensure_service_managed_id(old_target)

    assert new_target.stable_id is not None
    assert re.fullmatch(r"managed-[0-9a-f]{12}", new_target.stable_id)
    assert new_target.action_instance_id != old_action_instance_id
    assert new_target.action_instance_id == derive_action_instance_id(
        controller_id=CONTROLLER_ID,
        config_id=CONFIG_ID,
        action_id="action.no_schema",
        stable_id=new_target.stable_id,
    )
    for forbidden in (
        "context_id",
        "binding_id",
        "page_session_id",
        "device_id",
        "transport_id",
        "manager-local",
    ):
        assert forbidden not in new_target.key()

    reloaded = await config_service.get_config(CONFIG_ID)
    assert reloaded is not None
    assert reloaded.profiles[0].pages[0].controls[1].id == new_target.stable_id


def test_duplicate_explicit_control_ids_are_rejected() -> None:
    data = _config().model_dump(by_alias=True, mode="json")
    data["profiles"][0]["pages"][0]["controls"][1]["id"] = "clock-primary"

    with pytest.raises(ValidationError, match="control ids must be unique"):
        DeviceConfig.model_validate(data)
