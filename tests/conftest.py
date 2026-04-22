"""Pytest configuration for deckr-controller package tests."""

import pytest

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session")
def anyio_backend():
    """Use anyio as the async backend for all tests."""
    return "anyio"


@pytest.fixture(autouse=True)
def settings_tmp_dir(monkeypatch, tmp_path):
    """Keep file-backed settings writes inside a per-test temp directory."""

    monkeypatch.setenv("DECKR_SETTINGS_DIR", str(tmp_path))
    yield tmp_path
