from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from deckr.components import RunContext, resolve_component_host_plan, start_components
from deckr.core.config import ConfigDocument
from deckr.runtime import Deckr

from deckr.controller._config_watcher_component import (
    CONFIG_WATCHER_COMPONENT_ID,
)
from deckr.controller._config_watcher_component import (
    component as config_watcher_component,
)
from deckr.controller.config import (
    Control,
    DeviceConfig,
    DeviceConfigMatch,
    FileBackedDeviceConfigService,
    MaterializedConfigProjection,
    MaterializedConfigPublisher,
    MaterializedDeviceConfigService,
    Page,
    Profile,
    materialized_config_bucket_policy,
    materialized_config_key,
    materialized_config_result_key,
)
from test_support.memory_lane_substrate import MemoryJsonKvBucket, MemoryLaneSubstrate

CONTROLLER_ID = "controller-main"


def _config(
    config_id: str = "config-1",
    *,
    name: str = "Desk",
    fingerprint: str = "fingerprint:desk",
    labels: dict[str, str] | None = None,
) -> DeviceConfig:
    return DeviceConfig(
        id=config_id,
        name=name,
        match=DeviceConfigMatch(fingerprint=fingerprint, labels=labels or {}),
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action="action.clock",
                            )
                        ]
                    )
                ],
            )
        ],
    )


def _config_to_yaml(config: DeviceConfig) -> str:
    return yaml.safe_dump(
        config.model_dump(by_alias=True, exclude_none=True, mode="json"),
        sort_keys=False,
    )


async def _next_with_timeout(stream) -> Any:
    with anyio.fail_after(1):
        return await anext(stream)


async def _wait_for_result(bucket: MemoryJsonKvBucket, status: str):
    key = materialized_config_result_key(CONTROLLER_ID)
    with anyio.fail_after(1):
        while True:
            entry = await bucket.get(key)
            if entry is not None and entry.value.get("status") == status:
                return entry
            await anyio.sleep(0.01)


@pytest.mark.asyncio
async def test_materialized_service_activates_valid_projection() -> None:
    bucket = MemoryJsonKvBucket(bucket="config")
    await bucket.put(
        materialized_config_key(CONTROLLER_ID),
        MaterializedConfigProjection(
            controllerId=CONTROLLER_ID,
            timestamp=datetime.now(UTC),
            deviceConfigs=(_config(),),
        ),
    )
    service = MaterializedDeviceConfigService(
        controller_id=CONTROLLER_ID,
        bucket=bucket,
    )

    async with anyio.create_task_group() as tg:
        stopping = anyio.Event()
        await service.start(RunContext(tg=tg, stopping=stopping))
        try:
            config = await service.get_config("config-1")
            assert config is not None
            assert config.name == "Desk"
            match = await service.match_device(
                fingerprint="fingerprint:desk",
                labels={"location": "desk"},
            )
            assert match is not None
            assert match.id == "config-1"
            result = await _wait_for_result(bucket, "active")
            assert result.value["activeConfigIds"] == ("config-1",)
        finally:
            stopping.set()
            await service.stop()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_materialized_service_watches_projection_updates() -> None:
    bucket = MemoryJsonKvBucket(bucket="config")
    service = MaterializedDeviceConfigService(
        controller_id=CONTROLLER_ID,
        bucket=bucket,
    )
    publisher = MaterializedConfigPublisher(controller_id=CONTROLLER_ID, bucket=bucket)

    async with anyio.create_task_group() as tg:
        stopping = anyio.Event()
        await service.start(RunContext(tg=tg, stopping=stopping))
        stream = service.subscribe("config-1")
        try:
            assert await _next_with_timeout(stream) is None
            await publisher.publish_configs((_config(name="Updated"),))
            emitted = await _next_with_timeout(stream)
            assert emitted is not None
            assert emitted.name == "Updated"
            result = await _wait_for_result(bucket, "active")
            assert result.value["sourceRevision"] is not None
        finally:
            stopping.set()
            await service.stop()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_materialized_service_rejects_invalid_projection_without_deactivating() -> (
    None
):
    bucket = MemoryJsonKvBucket(bucket="config")
    publisher = MaterializedConfigPublisher(controller_id=CONTROLLER_ID, bucket=bucket)
    await publisher.publish_configs((_config(name="Good"),))
    service = MaterializedDeviceConfigService(
        controller_id=CONTROLLER_ID,
        bucket=bucket,
    )

    async with anyio.create_task_group() as tg:
        stopping = anyio.Event()
        await service.start(RunContext(tg=tg, stopping=stopping))
        try:
            await bucket.put(
                materialized_config_key(CONTROLLER_ID),
                {
                    "schema": "dev.deckr.controller.config.materialized.v1",
                    "controllerId": CONTROLLER_ID,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "deviceConfigs": [{"id": "broken"}],
                },
            )
            result = await _wait_for_result(bucket, "rejected")
            assert result.value["diagnostics"][0]["code"] == "invalid_projection"
            config = await service.get_config("config-1")
            assert config is not None
            assert config.name == "Good"
        finally:
            stopping.set()
            await service.stop()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_materialized_service_delete_means_no_projection() -> None:
    bucket = MemoryJsonKvBucket(bucket="config")
    publisher = MaterializedConfigPublisher(controller_id=CONTROLLER_ID, bucket=bucket)
    await publisher.publish_configs((_config(),))
    service = MaterializedDeviceConfigService(
        controller_id=CONTROLLER_ID,
        bucket=bucket,
    )

    async with anyio.create_task_group() as tg:
        stopping = anyio.Event()
        await service.start(RunContext(tg=tg, stopping=stopping))
        stream = service.subscribe("config-1")
        try:
            assert await _next_with_timeout(stream) is not None
            await bucket.delete(materialized_config_key(CONTROLLER_ID))
            assert await _next_with_timeout(stream) is None
            await _wait_for_result(bucket, "missing")
        finally:
            stopping.set()
            await service.stop()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_materialized_service_write_config_updates_projection() -> None:
    bucket = MemoryJsonKvBucket(bucket="config")
    service = MaterializedDeviceConfigService(
        controller_id=CONTROLLER_ID,
        bucket=bucket,
    )

    written = await service.write_config(_config(name="Written"))

    assert written.name == "Written"
    entry = await bucket.get(materialized_config_key(CONTROLLER_ID))
    assert entry is not None
    assert entry.value["deviceConfigs"][0]["name"] == "Written"
    result = await _wait_for_result(bucket, "active")
    assert result.value["activeConfigIds"] == ("config-1",)


@pytest.mark.asyncio
async def test_file_backed_service_publishes_materialized_snapshots(
    tmp_path: Path,
) -> None:
    bucket = MemoryJsonKvBucket(bucket="config")
    publisher = MaterializedConfigPublisher(controller_id=CONTROLLER_ID, bucket=bucket)
    service = FileBackedDeviceConfigService(
        config_dir=tmp_path,
        materialized_publisher=publisher,
    )
    path = tmp_path / "config-1.yml"
    path.write_text(_config_to_yaml(_config(name="From File")))

    await service.refresh()

    entry = await bucket.get(materialized_config_key(CONTROLLER_ID))
    assert entry is not None
    assert entry.value["deviceConfigs"][0]["name"] == "From File"

    path.unlink()
    await service.refresh()

    entry = await bucket.get(materialized_config_key(CONTROLLER_ID))
    assert entry is not None
    assert entry.value["deviceConfigs"] == ()


@pytest.mark.asyncio
async def test_config_watcher_component_publishes_materialized_snapshot(
    tmp_path: Path,
) -> None:
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "config-1.yml").write_text(
        _config_to_yaml(_config(name="Component"))
    )
    document = ConfigDocument(
        raw={
            "deckr": {
                "components": {
                    "instances": {
                        "local_config_watcher": {
                            "component": CONFIG_WATCHER_COMPONENT_ID,
                            "instance_id": "local",
                            "config": {
                                "path": "settings",
                                "target_controller_id": CONTROLLER_ID,
                            },
                        }
                    }
                }
            }
        },
        source_path=None,
        base_dir=tmp_path,
    )
    plan = resolve_component_host_plan(
        document,
        definitions={CONFIG_WATCHER_COMPONENT_ID: config_watcher_component},
    )
    substrate = MemoryLaneSubstrate(lane_contracts=plan.lane_contracts)

    async with (
        Deckr(
            lane_contracts=plan.lane_contracts,
            lanes=plan.lane_names,
            message_bus=substrate,
        ) as deckr,
        start_components(deckr, plan),
    ):
        key = materialized_config_key(CONTROLLER_ID)
        bucket = deckr.kv_bucket(materialized_config_bucket_policy())
        with anyio.fail_after(1):
            while True:
                entry = await bucket.get(key)
                if entry is not None:
                    break
                await anyio.sleep(0.01)

    assert entry.value["deviceConfigs"][0]["name"] == "Component"
    assert entry.value["producer"]["kind"] == "file_watcher"
