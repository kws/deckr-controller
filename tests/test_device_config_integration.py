"""Configuration-stream integration for DeviceSession."""

import anyio
import pytest

from test_support.device_session import session


@pytest.mark.asyncio
async def test_config_stream_updates_session_state_and_explicit_removal_clears_it():
    async with session() as device:
        changed = device.config.model_copy(update={"name": "Reloaded"})
        finished = anyio.Event()

        async def configurations():
            yield changed
            yield None
            finished.set()

        device._config_stream = configurations()
        stopping = anyio.Event()
        async with anyio.create_task_group() as tg:
            await device.start(tg, stopping)
            with anyio.fail_after(1):
                await finished.wait()
            assert device.config == changed
            assert not device.config_active
            assert not device.snapshot().binding_leases
            await device.aclose()
            stopping.set()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_descriptor_change_is_owned_by_session_state():
    async with session() as device:
        await device.set_page()
        await device.wait_idle()
        changed = device.device.model_copy(update={"display_name": "Updated"})
        await device.on_device_descriptor_changed(changed)
        await device.wait_idle()
        assert device.state.device is changed
        assert device.context_for_control("0,0") is not None
