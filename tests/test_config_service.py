"""Tests for FileBackedDeviceConfigService subscribe and matching behavior."""

from unittest.mock import AsyncMock

import anyio
import pytest
import yaml
from deckr.components import RunContext

from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from deckr.controller.config._service import FileBackedDeviceConfigService


@pytest.mark.asyncio
async def test_materialized_snapshot_retries_failure_without_source_changes(tmp_path):
    config = _make_config("dev1")
    (tmp_path / "dev1.yml").write_text(_config_to_yaml(config))
    publisher = AsyncMock()
    publisher.publish_configs.side_effect = [RuntimeError("offline"), None]
    service = FileBackedDeviceConfigService(tmp_path, materialized_publisher=publisher)
    await service.refresh()
    await service.refresh()
    await service.refresh()
    assert publisher.publish_configs.await_count == 2
    publisher.publish_configs.assert_awaited_with((config,))


@pytest.mark.asyncio
async def test_materialized_initial_empty_snapshot_requires_a_successful_scan(tmp_path):
    source = tmp_path / "configs"
    publisher = AsyncMock()
    service = FileBackedDeviceConfigService(source, materialized_publisher=publisher)
    await service.refresh()
    publisher.publish_configs.assert_not_awaited()
    source.mkdir()
    await service.refresh()
    await service.refresh()
    publisher.publish_configs.assert_awaited_once_with(())


@pytest.mark.asyncio
async def test_materialized_snapshot_publication_preserves_source_order(tmp_path):
    first = _make_config("dev1")
    second = _make_config("dev1", name="Changed")
    path = tmp_path / "dev1.yml"
    path.write_text(_config_to_yaml(first))
    entered, release = anyio.Event(), anyio.Event()
    publications = []

    async def publish(configs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        publications.append(configs)

    publisher = AsyncMock()
    publisher.publish_configs.side_effect = publish
    service = FileBackedDeviceConfigService(tmp_path, materialized_publisher=publisher)
    async with anyio.create_task_group() as tg:
        tg.start_soon(service.refresh)
        await entered.wait()
        path.write_text(_config_to_yaml(second))
        tg.start_soon(service.refresh)
        while await service.get_config("dev1") != second:
            await anyio.sleep(0)
        release.set()
    assert publications == [(first,), (second,)]


@pytest.fixture
def config_service(tmp_path):
    """FileBackedDeviceConfigService with temp config dir (not started)."""
    return FileBackedDeviceConfigService(config_dir=tmp_path)


def _make_config(
    config_id: str,
    name: str = "Test",
    *,
    fingerprint: str | None = None,
    labels: dict[str, str] | None = None,
    enabled: bool = True,
) -> DeviceConfig:
    return DeviceConfig(
        id=config_id,
        name=name,
        match={
            "fingerprint": fingerprint or f"fingerprint-{config_id}",
            "labels": labels or {},
        },
        enabled=enabled,
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action="action.a",
                                settings={},
                            ),
                        ]
                    ),
                ],
            ),
        ],
    )


def _config_to_yaml(cfg: DeviceConfig) -> str:
    return yaml.safe_dump(
        cfg.model_dump(by_alias=True, mode="json"),
        default_flow_style=False,
    )


@pytest.mark.asyncio
async def test_subscribe_receives_none_on_file_delete(config_service, tmp_path):
    """Subscribe receives None when config file is deleted."""
    cfg = _make_config("dev1")
    (tmp_path / "dev1.yml").write_text(_config_to_yaml(cfg))

    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=anyio.Event())
        await config_service.start(ctx)
        try:
            stream = config_service.subscribe("dev1")
            first = await anext(stream)
            assert first is not None

            (tmp_path / "dev1.yml").unlink()

            await anyio.sleep(0.3)  # Allow watch to detect change
            second = await anext(stream)
            assert second is None
        finally:
            await config_service.stop()


@pytest.mark.asyncio
async def test_subscribe_receives_config_on_file_add(config_service, tmp_path):
    """Subscribe receives config when file is added after subscribe."""
    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=anyio.Event())
        await config_service.start(ctx)
        try:
            stream = config_service.subscribe("dev1")
            first = await anext(stream)
            assert first is None

            cfg = _make_config("dev1")
            (tmp_path / "dev1.yml").write_text(_config_to_yaml(cfg))

            await anyio.sleep(0.3)  # Allow watch to detect change
            second = await anext(stream)
            assert second is not None
            assert second.id == "dev1"
        finally:
            await config_service.stop()


@pytest.mark.asyncio
async def test_file_config_slow_subscriber_converges_after_identity_overflow(
    config_service,
    tmp_path,
) -> None:
    stream = config_service.subscribe("config-256")
    assert await anext(stream) is None

    for index in range(257):
        config = _make_config(f"config-{index:03d}", name=f"Version {index}")
        await config_service._cache_and_notify(  # noqa: SLF001
            tmp_path / f"config-{index:03d}.yml",
            config,
        )
    final = _make_config("config-256", name="Final")
    await config_service._cache_and_notify(  # noqa: SLF001
        tmp_path / "config-256.yml",
        final,
    )

    with anyio.fail_after(1):
        converged = await anext(stream)
    assert converged is not None
    assert converged.name == "Final"

    await stream.aclose()
    await config_service.stop()


@pytest.mark.asyncio
async def test_invalid_yaml_does_not_emit(config_service, tmp_path):
    """An invalid file change preserves the last valid subscriber state."""
    from watchfiles import Change

    cfg = _make_config("dev1")
    path = tmp_path / "dev1.yml"
    path.write_text(_config_to_yaml(cfg))
    stream = config_service.subscribe("dev1")
    try:
        assert await anext(stream) == cfg
        version = config_service._state.version
        path.write_text("invalid: yaml: [")
        await config_service._process_changes({(Change.modified, str(path))})
        assert config_service._state.version == version
        assert await config_service.get_config("dev1") == cfg

        recovered = _make_config("dev1", name="Recovered")
        path.write_text(_config_to_yaml(recovered))
        await config_service._process_changes({(Change.modified, str(path))})
        with anyio.fail_after(1):
            assert await anext(stream) == recovered
    finally:
        await stream.aclose()
        await config_service.stop()


@pytest.mark.asyncio
async def test_match_device_uses_fingerprint_only_config_for_other_labels(
    config_service,
    tmp_path,
):
    generic = _make_config("generic", fingerprint="serial-a")
    specific = _make_config(
        "specific",
        fingerprint="serial-a",
        labels={"location": "room-a"},
    )
    (tmp_path / "generic.yml").write_text(_config_to_yaml(generic))
    (tmp_path / "specific.yml").write_text(_config_to_yaml(specific))

    match = await config_service.match_device(
        fingerprint="serial-a",
        labels={"location": "room-b"},
    )

    assert match is not None
    assert match.id == "generic"


@pytest.mark.asyncio
async def test_match_device_rejects_ambiguous_same_specificity(
    config_service,
    tmp_path,
):
    one = _make_config("one", fingerprint="serial-a")
    two = _make_config("two", fingerprint="serial-a")
    (tmp_path / "one.yml").write_text(_config_to_yaml(one))
    (tmp_path / "two.yml").write_text(_config_to_yaml(two))

    with pytest.raises(ValueError, match="Ambiguous device config match"):
        await config_service.match_device(
            fingerprint="serial-a",
            labels={"location": "room-a"},
        )


@pytest.mark.asyncio
async def test_scan_retains_last_validated_config_when_file_is_unreadable(
    config_service, tmp_path
):
    config = _make_config("retained")
    path = tmp_path / "retained.yml"
    path.write_text(_config_to_yaml(config))
    await config_service.refresh()
    path.write_text("profiles: [broken yaml")
    await config_service.refresh()
    assert await config_service.get_config("retained") == config
    path.unlink()
    await config_service.refresh()
    assert await config_service.get_config("retained") is None


@pytest.mark.asyncio
async def test_scan_retains_snapshot_when_source_directory_disappears(
    config_service, tmp_path
):
    config = _make_config("retained")
    (tmp_path / "retained.yml").write_text(_config_to_yaml(config))
    await config_service.refresh()
    moved = tmp_path.with_name(tmp_path.name + "-offline")
    tmp_path.rename(moved)
    try:
        await config_service.refresh()
        assert await config_service.get_config("retained") == config
        assert not config_service.source_available
    finally:
        moved.rename(tmp_path)
    await config_service.refresh()
    assert config_service.source_available
    assert await config_service.get_config("retained") == config
