"""Controller routing, configuration, recovery, and cleanup over managed hardware."""

from functools import partial

import anyio
import pytest

from test_support.managed_hardware import (
    _config,
    device_descriptor,
    eventually,
    setup,
)


@pytest.mark.asyncio
async def test_labels_select_configuration_and_claim_retains_selected_descriptor(
    managed_substrate,
):
    async with setup(
        labels={"room": "studio"},
        config_labels={"room": "studio"},
        substrate=managed_substrate,
    ) as h:
        live = h.service.route_for_config("config-a")
        assert h.service.snapshot().candidates[0].labels == {"room": "studio"}
        await h.manager.set_device(device_descriptor(power=True))
        await h.service.reconcile(reason="unchanged discovery")
        assert h.service.route_for_config("config-a") == live


@pytest.mark.asyncio
async def test_labels_mismatch_does_not_claim_hardware(managed_substrate):
    async with setup(
        labels={"room": "office"},
        config_labels={"room": "studio"},
        wait=False,
        substrate=managed_substrate,
    ) as h:
        await h.service.wait_current()
        await h.service.reconcile(reason="label mismatch")
        assert not h.service.snapshot().owned_claims


@pytest.mark.asyncio
async def test_removed_configuration_rematches_existing_agreement(managed_substrate):
    async with setup(substrate=managed_substrate) as h:
        original = h.service.route_for_config("config-a")
        await h.config_service.remove_config("config-a")
        await h.config_service.write_config(
            _config(config_id="replacement", fingerprint=original.device.fingerprint)
        )
        await h.service.reconcile(reason="configuration replacement")
        live = h.service.route_for_config("replacement")
        assert live is not None and live.contract == original.contract
        assert h.service.route_for_config("config-a") is None
        h.callbacks.on_hardware_disconnected.assert_awaited()


@pytest.mark.asyncio
async def test_descriptor_replacement_withdraws_old_route_and_recovers(
    managed_substrate,
):
    async with setup(substrate=managed_substrate) as h:
        original = h.service.route_for_config("config-a")
        replacement = device_descriptor().model_copy(
            update={"fingerprint": "replacement"}
        )
        await h.config_service.write_config(
            _config(config_id="config-a", fingerprint="replacement")
        )
        await h.manager.set_device(replacement)
        await eventually(
            lambda: (live := h.service.route_for_config("config-a")) is not None
            and live.contract != original.contract
        )
        assert (
            h.service.route_for_config("config-a").device.fingerprint == "replacement"
        )
        h.callbacks.on_hardware_disconnected.assert_awaited()


@pytest.mark.asyncio
async def test_authority_loss_blocks_output_before_controller_reconciliation(
    managed_substrate,
):
    async with setup(substrate=managed_substrate) as h:
        claim = h.service.claim_for_config("config-a")
        await claim.aclose()
        assert h.service.route_for_config("config-a") is None
        await h.service.command_service.clear_raster("config-a", "key-1", "raster")
        h.commands.assert_not_awaited()
        await eventually(lambda: h.callbacks.on_hardware_disconnected.await_count > 0)


@pytest.mark.asyncio
async def test_stopping_releases_claim_and_all_owned_workers(managed_substrate):
    async with setup(substrate=managed_substrate) as h:
        h.stopping.set()
        with anyio.fail_after(3):
            await h.service.aclose()
        assert h.service.route_for_config("config-a") is None
        await eventually(lambda: not h.manager.live_claims)


@pytest.mark.asyncio
async def test_authority_loss_during_configuration_lookup_withdraws_only_route(
    monkeypatch, managed_substrate
):
    async with setup(substrate=managed_substrate) as h:
        entered, release = anyio.Event(), anyio.Event()
        original = h.config_service.get_config

        async def delayed(config_id):
            entered.set()
            await release.wait()
            return await original(config_id)

        monkeypatch.setattr(h.config_service, "get_config", delayed)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(h.service.reconcile, reason="test race"))
            with anyio.fail_after(5):
                await entered.wait()
            await h.manager.remove_device("device-a")
            await eventually(lambda: h.service.route_for_config("config-a") is None)
            release.set()
        assert h.service.route_for_config("config-a") is None
        await h.manager.set_device(device_descriptor(power=True))
        await eventually(lambda: h.service.route_for_config("config-a") is not None)


@pytest.mark.asyncio
async def test_identical_device_ids_on_different_managers_keep_distinct_routes(
    managed_substrate,
):
    async with setup(substrate=managed_substrate) as h:
        first = h.service.route_for_config("config-a")
        second_descriptor = device_descriptor().model_copy(
            update={"fingerprint": "manager-b-device"}
        )
        await h.config_service.write_config(
            _config(config_id="config-b", fingerprint=second_descriptor.fingerprint)
        )
        async with (
            h.runtime.endpoint("hardware_manager:manager-b") as endpoint,
            h.runtime.hardware_manager(
                endpoint,
                manager_id="manager-b",
                devices={"device-a": second_descriptor},
            ),
        ):
            await eventually(lambda: h.service.route_for_config("config-b") is not None)
            second = h.service.route_for_config("config-b")
            assert first.ref.device_id == second.ref.device_id
            assert first.ref.manager_id != second.ref.manager_id
            assert first.contract != second.contract
            assert h.service.route_for_config("config-a") == first
        await eventually(lambda: h.service.route_for_config("config-b") is None)
        assert h.service.route_for_config("config-a") == first
