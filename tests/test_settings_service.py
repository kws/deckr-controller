"""Tests for the controller settings service boundary."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from deckr.pluginhost.messages import ActionDescriptor, SettingsTargetRef
from pydantic import ValidationError

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


def _config() -> DeviceConfig:
    return DeviceConfig(
        id=CONFIG_ID,
        name="Desk",
        match=DeviceConfigMatch(fingerprint="fingerprint:desk"),
        plugin_settings={"plugin.clock": {"timezone": "UTC"}},
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


async def _descriptor(action_id: str) -> ActionDescriptor | None:
    if action_id == "action.clock":
        return ActionDescriptor(
            uuid="action.clock",
            name="Clock",
            pluginUuid="plugin.clock",
            settingsSchema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string"},
                    "templateOverrides": {"type": "object"},
                },
                "additionalProperties": False,
            },
            pluginSettingsSchema={
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
                "additionalProperties": False,
            },
        )
    if action_id == "action.no_schema":
        return ActionDescriptor(
            uuid="action.no_schema",
            name="No Schema",
            pluginUuid="plugin.no_schema",
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
        action_descriptor_provider=_descriptor,
    )
    return settings_service, config_service


def _stable_action_target() -> SettingsTargetRef:
    return SettingsTargetRef(
        scope="action_instance",
        controllerId=CONTROLLER_ID,
        configId=CONFIG_ID,
        pluginId="plugin.clock",
        actionId="action.clock",
        actionInstanceId=derive_action_instance_id(
            controller_id=CONTROLLER_ID,
            config_id=CONFIG_ID,
            action_id="action.clock",
            stable_id="clock-primary",
        ),
        stableId="clock-primary",
    )


def _plugin_target(plugin_id: str = "plugin.clock") -> SettingsTargetRef:
    return SettingsTargetRef(
        scope="plugin",
        controllerId=CONTROLLER_ID,
        configId=CONFIG_ID,
        pluginId=plugin_id,
    )


async def _next_with_timeout(stream: AsyncIterator[Any]) -> Any:
    with anyio.fail_after(1):
        return await anext(stream)


@pytest.mark.asyncio
async def test_list_and_describe_plugin_and_action_targets(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)

    descriptions = await service.list_targets(config_id=CONFIG_ID)
    targets = {description.target.key(): description for description in descriptions}

    action_description = targets[_stable_action_target().key()]
    assert action_description.plugin_id == "plugin.clock"
    assert action_description.action_id == "action.clock"
    assert action_description.label == "clock-primary"
    assert action_description.placement == {
        "profile": "default",
        "page": "0",
        "control": "0,0",
    }
    assert action_description.provenance == ("config_default", "template_override")
    assert action_description.schema_metadata.stale is False
    assert action_description.schema_metadata.json_schema is not None

    plugin_description = targets[_plugin_target().key()]
    assert plugin_description.plugin_id == "plugin.clock"
    assert plugin_description.schema_metadata.stale is False

    missing_schema = next(
        description
        for description in descriptions
        if description.target.action_id == "action.no_schema"
    )
    assert missing_schema.plugin_id == "plugin.no_schema"
    assert missing_schema.schema_metadata.stale is True


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
async def test_plugin_settings_replace_writes_plugin_settings(tmp_path: Path) -> None:
    service, config_service = await _service(tmp_path)
    target = _plugin_target()

    snapshot = await service.replace(target, {"timezone": "Europe/Amsterdam"})

    assert snapshot.settings == {"timezone": "Europe/Amsterdam"}
    reloaded = await config_service.get_config(CONFIG_ID)
    assert reloaded is not None
    assert reloaded.plugin_settings == {
        "plugin.clock": {"timezone": "Europe/Amsterdam"}
    }


@pytest.mark.asyncio
async def test_schema_validation_rejects_invalid_settings(tmp_path: Path) -> None:
    service, config_service = await _service(tmp_path)

    with pytest.raises(ValueError, match="settings failed schema validation"):
        await service.patch(_stable_action_target(), {"mode": 42})

    reloaded = await config_service.get_config(CONFIG_ID)
    assert reloaded is not None
    assert reloaded.profiles[0].pages[0].controls[0].settings == {"mode": "time"}


@pytest.mark.asyncio
async def test_missing_schema_allows_write_and_marks_metadata_stale(
    tmp_path: Path,
) -> None:
    service, _ = await _service(tmp_path)
    target = next(
        description.target
        for description in await service.list_targets(config_id=CONFIG_ID)
        if description.target.action_id == "action.no_schema"
    )

    snapshot = await service.patch(target, {"freeform": {"ok": True}})

    assert snapshot.settings == {"freeform": {"ok": True}}
    assert snapshot.schema_metadata.stale is True


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
