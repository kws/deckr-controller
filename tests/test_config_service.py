"""Tests for FileBackedDeviceConfigService subscribe and matching behavior."""

import anyio
import pytest
import yaml
from deckr.core.component import RunContext

from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from deckr.controller.config._service import FileBackedDeviceConfigService


@pytest.fixture
def config_service(tmp_path):
    """FileBackedDeviceConfigService with temp config dir (not started)."""
    return FileBackedDeviceConfigService(config_dir=tmp_path)


def _make_config(
    config_id: str,
    name: str = "Test",
    *,
    fingerprint: str | None = None,
    manager_id: str | None = None,
    enabled: bool = True,
) -> DeviceConfig:
    return DeviceConfig(
        id=config_id,
        name=name,
        match={
            "fingerprint": fingerprint or f"fingerprint-{config_id}",
            "manager_id": manager_id,
        },
        enabled=enabled,
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(slot="0,0", action="action.a", settings={}),
                        ]
                    ),
                ],
            ),
        ],
    )


def _config_to_yaml(cfg: DeviceConfig) -> str:
    return yaml.dump(cfg.model_dump(), default_flow_style=False)


@pytest.mark.asyncio
async def test_subscribe_yields_none_when_no_config(config_service, tmp_path):
    """Subscribe with no config file yields None as first emission."""
    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=anyio.Event())
        await config_service.start(ctx)
        try:
            stream = config_service.subscribe("dev1")
            first = await anext(stream)
            assert first is None
        finally:
            await config_service.stop()


@pytest.mark.asyncio
async def test_subscribe_yields_config_when_file_exists(config_service, tmp_path):
    """Subscribe yields current config when file exists."""
    cfg = _make_config("dev1")
    (tmp_path / "dev1.yml").write_text(_config_to_yaml(cfg))

    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=anyio.Event())
        await config_service.start(ctx)
        try:
            stream = config_service.subscribe("dev1")
            first = await anext(stream)
            assert first is not None
            assert first.id == "dev1"
            assert first.name == "Test"
        finally:
            await config_service.stop()


@pytest.mark.asyncio
async def test_subscribe_receives_update_on_file_modify(config_service, tmp_path):
    """Subscribe receives updated config when file is modified."""
    cfg = _make_config("dev1", "Original")
    (tmp_path / "dev1.yml").write_text(_config_to_yaml(cfg))

    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=anyio.Event())
        await config_service.start(ctx)
        try:
            stream = config_service.subscribe("dev1")
            first = await anext(stream)
            assert first is not None
            assert first.name == "Original"

            cfg2 = _make_config("dev1", "Updated")
            (tmp_path / "dev1.yml").write_text(_config_to_yaml(cfg2))

            await anyio.sleep(0.3)  # Allow watch to detect change
            second = await anext(stream)
            assert second is not None
            assert second.name == "Updated"
        finally:
            await config_service.stop()


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
async def test_invalid_yaml_does_not_emit(config_service, tmp_path):
    """Invalid YAML or invalid config does not emit; previous config preserved."""
    cfg = _make_config("dev1")
    (tmp_path / "dev1.yml").write_text(_config_to_yaml(cfg))

    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=anyio.Event())
        await config_service.start(ctx)
        try:
            stream = config_service.subscribe("dev1")
            first = await anext(stream)
            assert first is not None

            (tmp_path / "dev1.yml").write_text("invalid: yaml: [")
            # Watch may emit; we should not get invalid config. Implementation logs and skips.
            await anyio.sleep(0.2)
            (tmp_path / "dev1.yml").write_text(_config_to_yaml(cfg))
            await anyio.sleep(0.3)
            second = await anext(stream)
            assert second is not None
            assert second.id == "dev1"
        finally:
            await config_service.stop()


def test_navigation_service_update_config():
    """NavigationService.update_config resets to root."""
    from deckr.controller._navigation_service import NavigationService, StaticPageRef

    cfg1 = _make_config("dev1", "Config1")
    nav = NavigationService(cfg1)
    nav.set_page(StaticPageRef(profile_name="default", page_index=0))
    nav.set_page(StaticPageRef(profile_name="default", page_index=1))

    cfg2 = _make_config("dev1", "Config2")
    transition = nav.update_config(cfg2)

    assert nav.current_page == StaticPageRef(profile_name="default", page_index=0)
    assert transition.arriving == nav.current_page
    assert nav._config.name == "Config2"


@pytest.mark.asyncio
async def test_match_device_prefers_manager_specific_config(config_service, tmp_path):
    generic = _make_config("generic", fingerprint="serial-a")
    specific = _make_config(
        "specific",
        fingerprint="serial-a",
        manager_id="room-a",
    )
    (tmp_path / "generic.yml").write_text(_config_to_yaml(generic))
    (tmp_path / "specific.yml").write_text(_config_to_yaml(specific))

    match = await config_service.match_device(
        fingerprint="serial-a",
        manager_id="room-a",
    )

    assert match is not None
    assert match.id == "specific"


@pytest.mark.asyncio
async def test_match_device_uses_fingerprint_only_config_for_other_manager(
    config_service,
    tmp_path,
):
    generic = _make_config("generic", fingerprint="serial-a")
    specific = _make_config(
        "specific",
        fingerprint="serial-a",
        manager_id="room-a",
    )
    (tmp_path / "generic.yml").write_text(_config_to_yaml(generic))
    (tmp_path / "specific.yml").write_text(_config_to_yaml(specific))

    match = await config_service.match_device(
        fingerprint="serial-a",
        manager_id="room-b",
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
            manager_id="room-a",
        )


@pytest.mark.asyncio
async def test_match_device_ignores_disabled_configs(config_service, tmp_path):
    disabled = _make_config(
        "disabled",
        fingerprint="serial-a",
        manager_id="room-a",
        enabled=False,
    )
    generic = _make_config("generic", fingerprint="serial-a")
    (tmp_path / "disabled.yml").write_text(_config_to_yaml(disabled))
    (tmp_path / "generic.yml").write_text(_config_to_yaml(generic))

    match = await config_service.match_device(
        fingerprint="serial-a",
        manager_id="room-a",
    )

    assert match is not None
    assert match.id == "generic"
