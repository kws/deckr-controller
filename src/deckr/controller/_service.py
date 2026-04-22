from __future__ import annotations

import argparse
import importlib
import logging
import re
import socket
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
from deckr.core.component import ComponentManager, ComponentState
from deckr.core.messaging import EventBus
from deckr.core.mqtt import MqttGatewayConfig
from deckr.core.util.anyio import add_signal_handler

from deckr.controller._config_document import (
    ControllerRuntimeConfig,
    DeckrConfigDocument,
    default_config_document_text,
    load_config_document,
)
from deckr.controller._controller_service import ControllerService
from deckr.controller._driver_service import DriverService, available_driver_names
from deckr.controller._remote_hardware import RemoteHardwareWebSocketServer
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


def _resolve_host_runtime_id(value: str | None) -> str:
    candidates = [value, socket.gethostname(), str(uuid.uuid4())]
    for candidate in candidates:
        if not candidate:
            continue
        normalized = _normalize_runtime_id(candidate)
        if normalized:
            return normalized
    raise ValueError("Unable to resolve a host ID")


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


def _config_section_present(controller: ControllerRuntimeConfig, name: str) -> bool:
    return getattr(controller, name) is not None


def _namespace_children(
    document: DeckrConfigDocument,
    path: str,
) -> dict[str, Mapping[str, Any]]:
    namespace = document.namespace(path)
    if namespace is None:
        return {}
    return {
        str(name): value
        for name, value in namespace.items()
        if isinstance(value, Mapping)
    }


def _resolve_local_driver_names(
    controller: ControllerRuntimeConfig,
) -> list[str] | None:
    local_drivers = controller.local_drivers
    if local_drivers is None:
        return None
    if local_drivers.mode == "all_installed":
        return available_driver_names()
    return list(local_drivers.names)


def _resolved_driver_configs(
    document: DeckrConfigDocument,
) -> dict[str, Mapping[str, Any]]:
    configs = {name: dict(config) for name, config in _namespace_children(document, "driver").items()}
    device_config = document.controller.device_config
    if (
        device_config is not None
        and device_config.file is not None
        and "mqtt" in configs
        and "config_path" not in configs["mqtt"]
    ):
        configs["mqtt"]["config_path"] = str(device_config.file.path)
    return configs


def _build_local_runtime_config(document: DeckrConfigDocument) -> dict[str, Any]:
    """Return local plugin host runtime fields as a plain mapping for host_factory."""
    local = document.controller.plugin_hosts.local
    if local is None:
        raise ValueError("Local plugin host configuration is missing")
    runtime = local.runtime
    return {
        "bind_host": runtime.bind_host,
        "bind_port": runtime.bind_port,
        "public_base_url": runtime.public_base_url,
        "descriptor_roots": tuple(str(path) for path in runtime.descriptor_roots),
        "docker_binary": runtime.docker_binary,
        "python_executable": runtime.python_executable or sys.executable,
        "claim_token_ttl": runtime.claim_token_ttl,
        "session_token_ttl": runtime.session_token_ttl,
        "restart_backoff_initial": runtime.restart_backoff_initial,
        "restart_backoff_max": runtime.restart_backoff_max,
        "log_level": runtime.log_level or document.controller.log_level,
    }


async def _add_local_plugin_host(
    document: DeckrConfigDocument,
    plugin_bus: EventBus,
    component_manager: ComponentManager,
    *,
    controller_id: str,
):
    plugin_hosts = document.controller.plugin_hosts
    if plugin_hosts is None or plugin_hosts.local is None:
        return None

    local = plugin_hosts.local
    try:
        module = importlib.import_module("deckr.plugin_hosts.python_local")
    except (ImportError, ModuleNotFoundError) as exc:
        if local.mode == "auto":
            logger.info("Skipping local plugin host: %s", exc)
            return None
        raise RuntimeError("Configured local plugin host is not installed") from exc

    factory = module.host_factory
    host = factory(
        plugin_bus,
        host_id=_resolve_host_runtime_id(local.host_id),
        controller_id=controller_id,
        config=_build_local_runtime_config(document),
        plugin_configs=_namespace_children(document, "plugin"),
    )
    await component_manager.add_component(host)
    return host


async def _add_mqtt_plugin_host(
    document: DeckrConfigDocument,
    plugin_bus: EventBus,
    component_manager: ComponentManager,
):
    plugin_hosts = document.controller.plugin_hosts
    if plugin_hosts is None or plugin_hosts.mqtt is None:
        return None

    section = plugin_hosts.mqtt
    from deckr.controller.mqtt import host_factory

    host = host_factory(
        plugin_bus,
        config=MqttGatewayConfig(
            hostname=section.hostname,
            port=section.port,
            topic=section.topic,
            username=section.username,
            password=section.password,
        ),
    )
    await component_manager.add_component(host)
    return host


async def _wait_for_components_ready(
    component_manager: ComponentManager,
    components: list[object],
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


async def service_runner(document: DeckrConfigDocument) -> None:
    controller_id = _resolve_controller_runtime_id(document.controller.id)
    driver_bus = EventBus()
    plugin_bus = EventBus(buffer_size=100, send_timeout=1.0)
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)

        config_service = _build_config_service(document)
        await component_manager.add_component(config_service)
        settings_service = _build_settings_service(document)

        from deckr.controller.plugin.action_registry import ActionRegistry

        action_registry = ActionRegistry(
            event_bus=plugin_bus,
            controller_id=controller_id,
        )
        await component_manager.add_component(action_registry)

        components_to_wait_for: list[object] = []
        local_host = await _add_local_plugin_host(
            document,
            plugin_bus,
            component_manager,
            controller_id=controller_id,
        )
        if local_host is not None:
            components_to_wait_for.append(local_host)

        mqtt_host = await _add_mqtt_plugin_host(
            document,
            plugin_bus,
            component_manager,
        )
        if mqtt_host is not None:
            components_to_wait_for.append(mqtt_host)

        await _wait_for_components_ready(component_manager, components_to_wait_for)

        controller_service = ControllerService(
            driver_bus=driver_bus,
            config_service=config_service,
            settings_service=settings_service,
            controller_id=controller_id,
            action_registry=action_registry,
            plugin_bus=plugin_bus,
        )
        await component_manager.add_component(controller_service)

        local_drivers = _resolve_local_driver_names(document.controller)
        if local_drivers is not None:
            driver_service = DriverService(
                driver_bus=driver_bus,
                enabled_drivers=local_drivers,
                driver_configs=_resolved_driver_configs(document),
            )
            await component_manager.add_component(driver_service)

        remote_hardware = document.controller.remote_hardware
        if (
            remote_hardware is not None
            and remote_hardware.websocket_server is not None
        ):
            section = remote_hardware.websocket_server
            remote_hw_server = RemoteHardwareWebSocketServer(
                driver_bus=driver_bus,
                controller_id=controller_id,
                host=section.host,
                port=section.port,
            )
            await component_manager.add_component(remote_hw_server)

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
