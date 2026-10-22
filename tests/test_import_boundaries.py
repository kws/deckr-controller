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
        "deckr.controller._device_session",
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


def test_production_dependency_direction():
    import ast

    root = Path(__file__).parents[1] / "src/deckr/controller"
    forbidden_coordinators = {
        "deckr.controller._device_session",
        "deckr.controller._device_components",
        "deckr.controller._controller_service",
        "deckr.controller._runtime_service",
    }
    violations = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module.startswith(("tests", "test_support")):
                violations.append((str(relative), node.lineno, module))
            if (
                relative.parts[0]
                in {"_actions", "_bindings", "_pages", "config", "settings"}
                and module in forbidden_coordinators
            ):
                violations.append((str(relative), node.lineno, module))
            if (
                relative.parts[0] == "_bindings"
                and module == "deckr.controller._actions._service"
            ):
                violations.append((str(relative), node.lineno, module))
            if module.startswith("deckr.") and not module.startswith(
                "deckr.controller"
            ):
                if any(part.startswith("_") for part in module.split(".")):
                    violations.append((str(relative), node.lineno, module))
            if module.startswith("invariant_gfx") and any(
                name.name.startswith("_") for name in node.names
            ):
                violations.append((str(relative), node.lineno, module))
    assert violations == []


def test_tests_do_not_import_other_test_modules():
    import ast

    root = Path(__file__).parents[1]
    for folder in ("tests", "test_support"):
        for path in (root / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    assert not module.startswith(
                        ("test_", "tests.")
                    ) or module.startswith("test_support."), (path, module)
                    assert module != "conftest", (path, module)
