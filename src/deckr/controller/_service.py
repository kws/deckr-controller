from __future__ import annotations

import argparse
import logging
from pathlib import Path

import anyio
from deckr.core.components import run_components
from deckr.core.config import ConfigDocument
from deckr.core.util.anyio import add_signal_handler

from deckr.controller._config_document import (
    ControllerRuntimeConfig,
    controller_config_from_document,
    default_config_document_text,
    load_config_document,
)
from deckr.controller.config import (
    FileBackedDeviceConfigService,
    NullDeviceConfigService,
)
from deckr.controller.settings import FileBackedSettingsService, InMemorySettingsService

logger = logging.getLogger(__name__)


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


def _build_config_service(config: ControllerRuntimeConfig):
    device_config = config.device_config
    if device_config is None or device_config.file is None:
        return NullDeviceConfigService()
    return FileBackedDeviceConfigService(config_dir=device_config.file.path)


def _build_settings_service(config: ControllerRuntimeConfig):
    settings = config.settings
    if settings is None or settings.file is None:
        return InMemorySettingsService()
    return FileBackedSettingsService(settings_dir=settings.file.path)


def _controller_component_filter(component_id: str) -> bool:
    return (
        component_id == "deckr.controller"
        or component_id.startswith("deckr.plugin_hosts.")
        or component_id.startswith("deckr.drivers.")
    )


async def component_runner(document: ConfigDocument) -> None:
    await run_components(
        document,
        component_filter=_controller_component_filter,
    )


async def async_main(document: ConfigDocument) -> None:
    async with anyio.create_task_group() as tg:
        await add_signal_handler(tg)
        await component_runner(document)
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

    _configure_logging(controller_config_from_document(document).log_level)
    anyio.run(async_main, document)
