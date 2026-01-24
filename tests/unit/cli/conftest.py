"""Shared fixtures for CLI unit tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner


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
    Uses AsyncMock since fetch_tokens is an async function.
    """
    with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock:
        mock.return_value = ("csrf_token", "session_id")
        yield mock


def create_mock_client():
    """Helper to create a properly configured mock client.

    Returns a MagicMock configured as an async context manager
    that can be used with `async with NotebookLMClient(...) as client:`.

    IMPORTANT: The mock has pre-created namespace objects (artifacts, sources,
    notebooks, chat, research, notes) to match NotebookLMClient's structure.
    Always use client.artifacts.method(), not client.method() directly.

    Example:
        mock_client = create_mock_client()
        mock_client.artifacts.list = AsyncMock(return_value=[...])
        mock_client.artifacts.download_audio = async_download_fn
    """
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    # Pre-create namespace mocks to match NotebookLMClient structure
    # This ensures consistent attribute access (mock_client.artifacts is always
    # the same object) and reminds developers to use the correct namespace
    mock_client.notebooks = MagicMock()
    mock_client.sources = MagicMock()
    mock_client.artifacts = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.research = MagicMock()
    mock_client.notes = MagicMock()
    mock_client.sharing = MagicMock()

    # Default mock for notebooks.list to support resolve_notebook_id
    # Tests using partial IDs (< 20 chars like "nb_123") trigger ID resolution
    mock_client.notebooks.list = AsyncMock(
        return_value=[MagicMock(id="nb_123", title="Test Notebook")]
    )

    return mock_client


def get_cli_module(module_path: str):
    """Get the actual CLI module by path, bypassing shadowed names.

    In cli/__init__.py, module names are shadowed by click groups with the same name
    (e.g., `from .source import source`). This function uses importlib to get the
    actual module for Python 3.10 compatibility.

    Args:
        module_path: The module name within notebooklm.cli (e.g., "source", "skill")

    Returns:
        The actual module object
    """
    import importlib

    return importlib.import_module(f"notebooklm.cli.{module_path}")


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

    Note:
        Uses importlib to get the actual module, not the click group that shadows
        the module name in cli/__init__.py. This is required for Python 3.10
        compatibility where mock.patch's string path resolution gets the wrong object.
    """
    import importlib

    module = importlib.import_module(f"notebooklm.cli.{module_path}")
    return patch.object(module, "NotebookLMClient")


class MultiMockProxy:
    """Proxy that forwards attribute access to all underlying mocks.

    When you set return_value on this proxy, it propagates to all mocks.
    Other attribute access is delegated to the primary mock.
    """

    def __init__(self, mocks):
        object.__setattr__(self, "_mocks", mocks)
        object.__setattr__(self, "_primary", mocks[0])

    def __getattr__(self, name):
        return getattr(self._primary, name)

    def __setattr__(self, name, value):
        if name == "return_value":
            # Propagate return_value to all mocks
            for m in self._mocks:
                m.return_value = value
        else:
            setattr(self._primary, name, value)


class MultiPatcher:
    """Context manager that patches NotebookLMClient in multiple CLI modules.

    After refactoring, commands are spread across multiple modules, so we need
    to patch NotebookLMClient in all of them.

    Uses importlib to get the actual module objects, bypassing shadowed names
    in cli/__init__.py where click groups share names with modules.
    """

    def __init__(self):
        import importlib

        # Get actual module objects to avoid Python 3.10 shadowing issues
        # where cli/__init__.py exports click groups with same names as modules
        notebook_mod = importlib.import_module("notebooklm.cli.notebook")
        chat_mod = importlib.import_module("notebooklm.cli.chat")
        session_mod = importlib.import_module("notebooklm.cli.session")
        share_mod = importlib.import_module("notebooklm.cli.share")

        self.patches = [
            patch.object(notebook_mod, "NotebookLMClient"),
            patch.object(chat_mod, "NotebookLMClient"),
            patch.object(session_mod, "NotebookLMClient"),
            patch.object(share_mod, "NotebookLMClient"),
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
    - notebook.py: list, create, delete, rename, summary
    - chat.py: ask, configure, history
    - session.py: use
    - share.py: status, public, view-level, add, update, remove

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
    """Provide a temporary context file for testing context commands.

    Patches get_context_path in both helpers and session modules since both
    import the function directly and need consistent behavior.
    """
    context_file = tmp_path / "context.json"
    with (
        patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
        patch("notebooklm.cli.session.get_context_path", return_value=context_file),
    ):
        yield context_file
