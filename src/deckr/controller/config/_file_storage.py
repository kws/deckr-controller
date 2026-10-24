"""Blocking YAML operations, invoked outside the controller event loop."""

import logging
import os
import tempfile
from pathlib import Path

import yaml

from deckr.controller.config._data import DeviceConfig

logger = logging.getLogger(__name__)


def read_snapshot(directory, previous_paths, previous_configs, previous_documents):
    paths = sorted(
        path for path in directory.iterdir() if path.suffix in {".yml", ".yaml"}
    )
    by_path, by_id, documents = {}, {}, {}
    for path in paths:
        try:
            document = path.read_text()
            prior = previous_configs.get(previous_paths.get(path))
            if prior is not None and previous_documents.get(path) == document:
                config = prior
            else:
                config = DeviceConfig.model_validate(yaml.safe_load(document))
            documents[path] = document
        except (OSError, ValueError, yaml.YAMLError):
            logger.warning("Invalid config file; retaining prior value: %s", path)
            config = previous_configs.get(previous_paths.get(path))
        if config is None:
            continue
        if config.id in by_id:
            raise ValueError(f"Duplicate config ID: {config.id}")
        by_path[path], by_id[config.id] = config.id, config
    return by_path, by_id, documents


def write_config(directory: Path, path: Path, config: DeviceConfig) -> None:
    if (
        not config.id
        or config.id in {".", ".."}
        or "/" in config.id
        or "\\" in config.id
    ):
        raise ValueError("Config ID must be a single filename component")
    root = directory.resolve()
    destination = path.resolve()
    if destination.parent != root:
        raise ValueError("Config path escapes its configured directory")
    root.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=".config-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            yaml.safe_dump(
                config.model_dump(by_alias=True, exclude_none=True, mode="json"),
                stream,
                sort_keys=False,
            )
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
