"""Shared fixtures for CLI unit tests."""

import pytest
from click.testing import CliRunner
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.fixture
def runner():
    """Create a Click test runner."""
    return CliRunner()


@pytest.fixture
def mock_auth():
    """Mock authentication for CLI commands.

    After CLI refactoring, auth is loaded via cli.helpers module.
    We patch both the main CLI and the helpers module for full coverage.
    """
    with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


@pytest.fixture
def mock_fetch_tokens():
    """Mock fetch_tokens for CLI commands.

    After CLI refactoring, fetch_tokens is called via cli.helpers module.
    """
    with patch("notebooklm.cli.helpers.fetch_tokens") as mock:
        mock.return_value = ("csrf_token", "session_id")
        yield mock


def create_mock_client():
    """Helper to create a properly configured mock client.

    Returns a MagicMock configured as an async context manager
    that can be used with `async with NotebookLMClient(...) as client:`.
    """
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


def patch_client_for_module(module_path: str):
    """Create a context manager that patches NotebookLMClient in the given module.

    Args:
        module_path: The module name within notebooklm.cli (e.g., "source", "artifact")

    Returns:
        A patch context manager for NotebookLMClient

    Example:
        with patch_client_for_module("source") as mock_cls:
            mock_client = create_mock_client()
            mock_cls.return_value = mock_client
            # ... run test
    """
    return patch(f"notebooklm.cli.{module_path}.NotebookLMClient")


class MultiMockProxy:
    """Proxy that forwards attribute access to all underlying mocks.

    When you set return_value on this proxy, it propagates to all mocks.
    Other attribute access is delegated to the primary mock.
    """
    def __init__(self, mocks):
        object.__setattr__(self, '_mocks', mocks)
        object.__setattr__(self, '_primary', mocks[0])

    def __getattr__(self, name):
        return getattr(self._primary, name)

    def __setattr__(self, name, value):
        if name == 'return_value':
            # Propagate return_value to all mocks
            for m in self._mocks:
                m.return_value = value
        else:
            setattr(self._primary, name, value)


class MultiPatcher:
    """Context manager that patches NotebookLMClient in multiple CLI modules.

    After refactoring, commands are spread across multiple modules, so we need
    to patch NotebookLMClient in all of them.
    """
    def __init__(self):
        self.patches = [
            patch("notebooklm.cli.notebook.NotebookLMClient"),
            patch("notebooklm.cli.chat.NotebookLMClient"),
            patch("notebooklm.cli.insights.NotebookLMClient"),
            patch("notebooklm.cli.session.NotebookLMClient"),
        ]
        self.mocks = []

    def __enter__(self):
        # Start all patches and collect mocks
        self.mocks = [p.__enter__() for p in self.patches]
        # Return a proxy that propagates return_value to all mocks
        return MultiMockProxy(self.mocks)

    def __exit__(self, *args):
        for p in reversed(self.patches):
            p.__exit__(*args)


def patch_main_cli_client():
    """Create a context manager that patches NotebookLMClient in CLI command modules.

    After refactoring, top-level commands are in separate modules:
    - notebook.py: list, create, delete, rename, share, featured
    - chat.py: ask, configure, history
    - insights.py: summary, analytics, research
    - session.py: use

    Returns:
        A context manager that patches NotebookLMClient in all relevant modules

    Example:
        with patch_main_cli_client() as mock_cls:
            mock_client = create_mock_client()
            mock_cls.return_value = mock_client
            # ... run test
    """
    return MultiPatcher()


@pytest.fixture
def mock_context_file(tmp_path):
    """Provide a temporary context file for testing context commands."""
    context_file = tmp_path / "context.json"
    with patch("notebooklm.cli.helpers.CONTEXT_FILE", context_file):
        yield context_file
