import argparse
import importlib
import logging
import os

import anyio
from deckr.core.component import ComponentManager, ComponentState
from deckr.core.messaging import EventBus
from deckr.core.util.anyio import add_signal_handler
from deckr.core.util.host_id import resolve_controller_id, resolve_host_id

from deckr.controller._controller_service import ControllerService
from deckr.controller._driver_service import DriverService, available_driver_names
from deckr.controller._remote_hardware import RemoteHardwareWebSocketServer
from deckr.controller.config import FileSystemConfigService

logger = logging.getLogger(__name__)

PLUGIN_HOST_REGISTRY: dict[str, str] = {
    "local": "deckr.plugin_hosts.python_local:host_factory",
    "mqtt": "deckr.controller.mqtt:host_factory",
}


def _default_plugin_hosts() -> list[str]:
    """If no --pluginhost given: use ['local'] when python_local is importable, else []."""
    try:
        importlib.import_module("deckr.plugin_hosts.python_local")
        return ["local"]
    except ImportError:
        return []


def _default_local_drivers() -> list[str]:
    return available_driver_names()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deckr controller")
    parser.add_argument(
        "--pluginhost",
        action="append",
        dest="plugin_hosts",
        choices=list(PLUGIN_HOST_REGISTRY),
        metavar="HOST",
        help="Plugin host to enable (local, mqtt). Repeatable.",
    )
    parser.add_argument(
        "--controller-id",
        dest="controller_id",
        default=None,
        metavar="ID",
        help="Controller ID (CONTROLLER_ID env, else a new UUID per process).",
    )
    parser.add_argument(
        "--host-id",
        dest="host_id",
        default=None,
        metavar="ID",
        help="Host ID for local plugin host (HOST_ID env, else system hostname).",
    )
    parser.add_argument(
        "--driver",
        action="append",
        dest="drivers",
        choices=available_driver_names() or None,
        metavar="DRIVER",
        help="Local hardware driver to enable. Repeatable.",
    )
    parser.add_argument(
        "--no-local-drivers",
        action="store_true",
        dest="no_local_drivers",
        help="Disable all in-process hardware drivers.",
    )
    parser.add_argument(
        "--device-websocket-host",
        dest="device_websocket_host",
        default=None,
        metavar="HOST",
        help="Bind host for remote device-manager websocket server.",
    )
    parser.add_argument(
        "--device-websocket-port",
        dest="device_websocket_port",
        type=int,
        default=8765,
        metavar="PORT",
        help="Bind port for remote device-manager websocket server.",
    )
    return parser.parse_args()


async def _load_plugin_hosts(
    plugin_hosts: list[str],
    plugin_bus,
    component_manager,
    *,
    controller_id: str,
    host_id: str | None = None,
):
    """Load plugin hosts from PLUGIN_HOST_REGISTRY and add to component manager.

    Returns list of host components for orchestration to wait on.
    host_id applies to the local host only (MQTT host provides its own via deckr-mqtt-host).
    """
    hosts = []
    for host_name in plugin_hosts:
        spec = PLUGIN_HOST_REGISTRY.get(host_name)
        if spec is None:
            logger.warning("Unknown plugin host %r, skipping", host_name)
            continue
        module_path, _, attr = spec.partition(":")
        if not attr:
            logger.warning(
                "Invalid plugin host spec %r for %s, skipping", spec, host_name
            )
            continue
        try:
            mod = importlib.import_module(module_path)
        except (ImportError, ModuleNotFoundError) as e:
            logger.warning(
                "Plugin host %r not available on classpath (%s), skipping",
                host_name,
                e,
            )
            continue
        try:
            factory = getattr(mod, attr)
            if host_name == "local":
                host = factory(
                    plugin_bus,
                    host_id=host_id,
                    controller_id=controller_id,
                )
            else:
                host = factory(plugin_bus)
        except Exception:
            logger.exception("Error creating plugin host %s", host_name)
            continue
        await component_manager.add_component(host)
        hosts.append(host)
    return hosts


async def service_runner(
    plugin_hosts: list[str],
    *,
    controller_id: str,
    host_id: str | None = None,
    local_drivers: list[str] | None = None,
    device_websocket_host: str | None = None,
    device_websocket_port: int = 8765,
):
    driver_bus = EventBus()
    plugin_bus = EventBus(buffer_size=100, send_timeout=1.0)
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)

        config_service = FileSystemConfigService()
        await component_manager.add_component(config_service)

        from deckr.controller.plugin.action_registry import ActionRegistry

        action_registry = ActionRegistry(
            event_bus=plugin_bus,
            controller_id=controller_id,
        )
        await component_manager.add_component(action_registry)

        hosts = await _load_plugin_hosts(
            plugin_hosts,
            plugin_bus,
            component_manager,
            controller_id=controller_id,
            host_id=host_id,
        )

        for host in hosts:
            await component_manager.wait_for_state(
                host, ComponentState.RUNNING, timeout=5.0
            )
        for host in hosts:
            wait_ready = getattr(host, "wait_ready", None)
            if callable(wait_ready):
                ready = await wait_ready(timeout=5.0)
                if not ready:
                    raise TimeoutError(
                        f"Timed out waiting for plugin host readiness: {type(host).__name__}"
                    )

        controller_service = ControllerService(
            driver_bus=driver_bus,
            config_service=config_service,
            controller_id=controller_id,
            action_registry=action_registry,
            plugin_bus=plugin_bus,
        )
        await component_manager.add_component(controller_service)

        if local_drivers:
            driver_service = DriverService(
                driver_bus=driver_bus,
                enabled_drivers=local_drivers,
            )
            await component_manager.add_component(driver_service)

        if device_websocket_host:
            remote_hw_server = RemoteHardwareWebSocketServer(
                driver_bus=driver_bus,
                controller_id=controller_id,
                host=device_websocket_host,
                port=device_websocket_port,
            )
            await component_manager.add_component(remote_hw_server)

        await anyio.sleep_forever()

        tg.cancel_scope.cancel()


async def async_main(
    plugin_hosts: list[str],
    controller_id: str,
    host_id: str | None = None,
    local_drivers: list[str] | None = None,
    device_websocket_host: str | None = None,
    device_websocket_port: int = 8765,
):
    async with anyio.create_task_group() as tg:
        await add_signal_handler(tg)
        await service_runner(
            plugin_hosts,
            controller_id=controller_id,
            host_id=host_id,
            local_drivers=local_drivers,
            device_websocket_host=device_websocket_host,
            device_websocket_port=device_websocket_port,
        )
        tg.cancel_scope.cancel()


def main():
    # Configure logging
    # Set DEBUG=1 environment variable to enable debug logging for HID communication
    log_level = logging.DEBUG if os.getenv("DEBUG") == "1" else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse_args()
    plugin_hosts = args.plugin_hosts if args.plugin_hosts else _default_plugin_hosts()
    controller_id = resolve_controller_id(cli_value=args.controller_id)
    if args.no_local_drivers and args.drivers:
        raise SystemExit("Use either --driver or --no-local-drivers, not both.")
    if args.no_local_drivers:
        local_drivers: list[str] = []
    else:
        local_drivers = args.drivers if args.drivers else _default_local_drivers()
    host_id = None
    if "local" in plugin_hosts:
        host_id = resolve_host_id(cli_value=args.host_id)
    anyio.run(
        async_main,
        plugin_hosts,
        controller_id,
        host_id,
        local_drivers,
        args.device_websocket_host,
        args.device_websocket_port,
    )
