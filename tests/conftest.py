"""Pytest configuration for deckr-controller package tests."""

import pytest

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session")
def anyio_backend():
    """Use anyio as the async backend for all tests."""
    return "anyio"
