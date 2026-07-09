"""Tests for FileBackedDeviceConfigService subscribe and matching behavior."""

import anyio
import pytest
import yaml
from deckr.components import RunContext

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
