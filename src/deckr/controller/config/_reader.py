import logging
from pathlib import Path

import yaml

from deckr.controller.config._data import DeviceConfig
from deckr.controller.config._service import resolve_default_config_dir

logger = logging.getLogger(__name__)

CONFIG_DIR = resolve_default_config_dir()


def load_config(file: Path):
    try:
        file_content = file.read_text()
        file_data = yaml.safe_load(file_content)
        return DeviceConfig.model_validate(file_data)
    except Exception as e:
        logger.exception(f"Error loading config from {file}: {e}", exc_info=True)


def load_all_configs():
    for file in CONFIG_DIR.glob("*.yml"):
        yield load_config(file)


def get_config(config_id: str) -> DeviceConfig | None:
    for cfg in load_all_configs():
        if cfg.id == config_id:
            return cfg
    return None
