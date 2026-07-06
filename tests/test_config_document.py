from __future__ import annotations

from pathlib import Path

import pytest

from deckr.controller import (
    controller_config_from_document,
    load_config_document,
)


def test_explicit_config_allows_missing_controller_table(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[deckr]\n")

    document = load_config_document(config_path)
    assert controller_config_from_document(document).device_config is None


def test_load_config_document_accepts_render_observation_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.render]
backend = "thread"

[deckr.components.instances.controller_main.config.render.observation]
enabled = true
sink = "jsonl"
path = "observations/render.jsonl"
include_graph = true
include_context = true
""".strip()
    )

    controller = controller_config_from_document(load_config_document(config_path))

    assert controller.render is not None
    assert controller.render.backend == "thread"
    assert controller.render.observation is not None
    assert controller.render.observation.enabled is True
    assert controller.render.observation.sink == "jsonl"
    assert (
        controller.render.observation.path
        == (tmp_path / "observations/render.jsonl").resolve()
    )
    assert controller.render.observation.include_graph is True
    assert controller.render.observation.include_context is True


def test_load_config_document_rejects_enabled_render_observation_without_path(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.render.observation]
enabled = true
""".strip()
    )

    with pytest.raises(ValueError, match="render observation path"):
        load_config_document(config_path)


def test_load_config_document_rejects_multiple_device_config_sources(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.file]
path = "settings"

[deckr.components.instances.controller_main.config.device_config.materialized]
bucket = "dev_deckr_controller_config_v1"
""".strip()
    )

    with pytest.raises(ValueError, match="either file or materialized"):
        load_config_document(config_path)


def test_load_config_document_rejects_invalid_materialized_bucket(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.materialized]
bucket = "dev.deckr.controller.config.v1"
""".strip()
    )

    with pytest.raises(ValueError, match="JetStream-safe underscore"):
        load_config_document(config_path)


