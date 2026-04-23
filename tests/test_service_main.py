from __future__ import annotations

from pathlib import Path

import pytest

from deckr.controller import (
    _service as service_mod,
)
from deckr.controller import (
    controller_config_from_document,
    default_config_document_text,
)


def test_parse_args_accepts_config_path() -> None:
    args = service_mod._parse_args(["--config", "/tmp/deckr.toml"])

    assert args.config_path == "/tmp/deckr.toml"
    assert args.print_default_config is False


def test_parse_args_rejects_removed_legacy_flag() -> None:
    with pytest.raises(SystemExit):
        service_mod._parse_args(["--pluginhost", "mqtt"])


def test_main_prints_default_config(capsys: pytest.CaptureFixture[str]) -> None:
    service_mod.main(["--print-default-config"])

    output = capsys.readouterr().out
    assert output == f"{default_config_document_text()}\n"


def test_main_loads_explicit_config_and_runs_anyio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.controller]
log_level = "warning"
""".strip()
    )
    captured: dict[str, object] = {}

    def fake_anyio_run(fn, document):
        captured["fn"] = fn
        captured["document"] = document

    monkeypatch.setattr(service_mod.anyio, "run", fake_anyio_run)
    monkeypatch.setattr(service_mod, "_configure_logging", lambda level: None)

    service_mod.main(["--config", str(config_path)])

    assert captured["fn"] is service_mod.async_main
    document = captured["document"]
    assert document.source_path == config_path.resolve()
    assert controller_config_from_document(document).log_level == "warning"


def test_main_auto_loads_cwd_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.controller]
log_level = "error"
""".strip()
    )
    captured: dict[str, object] = {}

    def fake_anyio_run(fn, document):
        captured["document"] = document

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(service_mod.anyio, "run", fake_anyio_run)
    monkeypatch.setattr(service_mod, "_configure_logging", lambda level: None)

    service_mod.main([])

    document = captured["document"]
    assert document.source_path == config_path.resolve()
    assert controller_config_from_document(document).log_level == "error"
