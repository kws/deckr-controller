"""Tests for the controller settings service boundary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from deckr.actions.messages import SettingsTargetRef
from pydantic import ValidationError

from deckr.controller._actions import ActionMetadata, SettingsActionMetadata
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
    derive_static_action_instance_id,
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
                            Control(
                                selector={"label": "Selector Only"},
                                action="action.no_schema",
                                settings={"mode": "fallback"},
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


@pytest.mark.asyncio
async def test_action_settings_get_reads_config_snapshot(
    tmp_path: Path,
) -> None:
    service, _ = await _service(tmp_path)

    snapshot = await service.get(_stable_action_target())

    assert snapshot.settings == {
        "mode": "time",
        "templateOverrides": {"title": "Clock"},
    }
    assert snapshot.provenance == ("config_default", "template_override")
    assert snapshot.schema_metadata.json_schema == {
        "type": "object",
        "properties": {
            "mode": {"type": "string"},
            "templateOverrides": {"type": "object"},
        },
        "additionalProperties": False,
    }


@pytest.mark.asyncio
async def test_provider_settings_get_reads_config_snapshot(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)

    snapshot = await service.get(_provider_target())

    assert snapshot.settings == {"timezone": "UTC"}
    assert snapshot.provenance == ("config_default",)
    assert snapshot.schema_metadata.json_schema == {
        "type": "object",
        "properties": {"timezone": {"type": "string"}},
        "additionalProperties": False,
    }


@pytest.mark.asyncio
async def test_settings_service_does_not_expose_mutation_api(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)

    assert not hasattr(service, "patch")
    assert not hasattr(service, "replace")
    assert not hasattr(service, "subscribe")
    assert not hasattr(service, "ensure_service_managed_id")


@pytest.mark.asyncio
async def test_settings_metadata_reads_from_action_service(
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
async def test_selector_only_action_target_uses_static_config_position(
    tmp_path: Path,
) -> None:
    service, _ = await _service(tmp_path)

    selector_only = next(
        description
        for description in await service.list_targets(config_id=CONFIG_ID)
        if description.label == "Selector Only"
    )

    assert selector_only.target.stable_id is None
    assert selector_only.target.action_instance_id == derive_static_action_instance_id(
        controller_id=CONTROLLER_ID,
        config_id=CONFIG_ID,
        action_id="action.no_schema",
        profile_id="default",
        page_id="0",
        selector_control_id=None,
        control_index=2,
    )


def test_duplicate_explicit_control_ids_are_rejected() -> None:
    data = _config().model_dump(by_alias=True, mode="json")
    data["profiles"][0]["pages"][0]["controls"][1]["id"] = "clock-primary"

    with pytest.raises(ValidationError, match="control ids must be unique"):
        DeviceConfig.model_validate(data)
