from __future__ import annotations

import argparse
import logging
import re
import uuid
from pathlib import Path

import anyio
from deckr.core.backplane import DeckrBackplane
from deckr.core.component import Component, ComponentManager, ComponentState
from deckr.core.util.anyio import add_signal_handler

from deckr.controller._config_document import (
    DeckrConfigDocument,
    default_config_document_text,
    load_config_document,
)
from deckr.controller._controller_service import ControllerService
from deckr.controller._providers import (
    activate_driver_providers,
    activate_plugin_host_providers,
)
from deckr.controller.config import (
    FileBackedDeviceConfigService,
    NullDeviceConfigService,
)
from deckr.controller.settings import FileBackedSettingsService, InMemorySettingsService

logger = logging.getLogger(__name__)

_INVALID_RUNTIME_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]+")


def _normalize_runtime_id(value: str) -> str:
    value = value.strip()
    value = value.replace("::", "-")
    value = _INVALID_RUNTIME_ID_CHARS.sub("-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value


def _resolve_controller_runtime_id(value: str | None) -> str:
    if value:
        normalized = _normalize_runtime_id(value)
        if normalized:
            return normalized
    return _normalize_runtime_id(str(uuid.uuid4()))


def _configure_logging(level: str) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deckr controller")
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        metavar="PATH",
        help="Load configuration from PATH instead of auto-loading ./deckr.toml.",
    )
    parser.add_argument(
        "--print-default-config",
        action="store_true",
        dest="print_default_config",
        help="Print the built-in default deckr.toml document and exit.",
    )
    return parser.parse_args(argv)


def _build_config_service(document: DeckrConfigDocument):
    device_config = document.controller.device_config
    if device_config is None or device_config.file is None:
        return NullDeviceConfigService()
    return FileBackedDeviceConfigService(config_dir=device_config.file.path)


def _build_settings_service(document: DeckrConfigDocument):
    settings = document.controller.settings
    if settings is None or settings.file is None:
        return InMemorySettingsService()
    return FileBackedSettingsService(settings_dir=settings.file.path)


async def _wait_for_components_ready(
    component_manager: ComponentManager,
    components: list[Component],
) -> None:
    for component in components:
        await component_manager.wait_for_state(
            component,
            ComponentState.RUNNING,
            timeout=5.0,
        )
    for component in components:
        wait_ready = getattr(component, "wait_ready", None)
        if callable(wait_ready):
            ready = await wait_ready(timeout=5.0)
            if not ready:
                raise TimeoutError(
                    f"Timed out waiting for component readiness: {type(component).__name__}"
                )


async def _add_plugin_hosts(
    document: DeckrConfigDocument,
    plugin_bus,
    component_manager: ComponentManager,
    *,
    controller_id: str,
) -> list[Component]:
    backplane = DeckrBackplane(plugin_messages=plugin_bus)
    return await activate_plugin_host_providers(
        document,
        backplane,
        component_manager,
        controller_id=controller_id,
    )


async def _compose_controller_runtime(
    document: DeckrConfigDocument,
    *,
    controller_id: str,
    component_manager: ComponentManager,
    backplane: DeckrBackplane,
) -> None:
    config_service = _build_config_service(document)
    await component_manager.add_component(config_service)
    settings_service = _build_settings_service(document)

    from deckr.controller.plugin.action_registry import ActionRegistry

    action_registry = ActionRegistry(
        event_bus=backplane.plugin_messages,
        controller_id=controller_id,
    )
    await component_manager.add_component(action_registry)

    plugin_host_components = await activate_plugin_host_providers(
        document,
        backplane,
        component_manager,
        controller_id=controller_id,
    )
    await _wait_for_components_ready(component_manager, plugin_host_components)

    controller_service = ControllerService(
        driver_bus=backplane.hardware_events,
        config_service=config_service,
        settings_service=settings_service,
        controller_id=controller_id,
        action_registry=action_registry,
        plugin_bus=backplane.plugin_messages,
    )
    await component_manager.add_component(controller_service)

    await activate_driver_providers(document, backplane, component_manager)


async def service_runner(document: DeckrConfigDocument) -> None:
    controller_id = _resolve_controller_runtime_id(document.controller.id)
    backplane = DeckrBackplane()
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await _compose_controller_runtime(
            document,
            controller_id=controller_id,
            component_manager=component_manager,
            backplane=backplane,
        )
        await anyio.sleep_forever()


async def async_main(document: DeckrConfigDocument) -> None:
    async with anyio.create_task_group() as tg:
        await add_signal_handler(tg)
        await service_runner(document)
        tg.cancel_scope.cancel()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.print_default_config:
        print(default_config_document_text())
        return

    try:
        document = load_config_document(
            Path(args.config_path).expanduser().resolve()
            if args.config_path
            else None
        )
    except Exception as exc:
        raise SystemExit(str(exc)) from exc

    _configure_logging(document.controller.log_level)
    anyio.run(async_main, document)
