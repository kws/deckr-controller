"""Cold-import coverage for controller dependency boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module",
    (
        "deckr.controller._actions",
        "deckr.controller._action_interest",
        "deckr.controller._binding_planner",
        "deckr.controller._bindings",
        "deckr.controller._device_manager",
        "deckr.controller._controller_service",
    ),
)
def test_controller_module_cold_imports(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
