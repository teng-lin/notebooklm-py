"""Tests for ``notebooklm use`` fail-closed verification (PR).

Before PR, ``notebooklm use <id>`` persisted the supplied notebook ID to
``context.json`` *even when the existence check failed* — either because the
RPC errored, or because the server returned a degenerate "empty notebook"
payload for an unknown ID. The result was poisoned saved state that broke
downstream commands until the user manually cleared the context.

This module pins the post-fix contract:

* Successful ``client.notebooks.get`` → context is persisted, exit 0.
* ``NotebookNotFoundError`` from ``client.notebooks.get`` → exit 1, context
  file is NOT created.
* ``--force`` flag → context is persisted regardless of verification.

Each test mocks the entire client surface; nothing here hits the network.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.exceptions import NotebookNotFoundError, RPCError
from notebooklm.notebooklm_cli import cli
from notebooklm.types import Notebook

from .conftest import create_mock_client, patch_main_cli_client


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_auth():
    """Patch storage auth so the `use` command sees real-looking cookies."""
    with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "__Secure-1PSIDTS": "test_1psidts",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


@pytest.fixture
def mock_context_file(tmp_path):
    """Provide a temporary context path; both call sites are patched."""
    context_file = tmp_path / "context.json"
    with (
        patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
        patch("notebooklm.cli.session.get_context_path", return_value=context_file),
    ):
        yield context_file


def _make_notebook(notebook_id: str = "nb_real", title: str = "Real Notebook") -> Notebook:
    return Notebook(
        id=notebook_id,
        title=title,
        created_at=datetime(2026, 1, 15),
        is_owner=True,
    )


class TestNotebookNotFoundIsRPCError:
    """``NotebookNotFoundError`` must still be catchable as ``RPCError``."""

    def test_inherits_from_rpc_error(self):
        # ``except RPCError`` at higher layers must still match — this is the
        # whole point of widening the base class in the fail-closed fix.
        assert issubclass(NotebookNotFoundError, RPCError)

    def test_carries_notebook_id(self):
        err = NotebookNotFoundError("nb_typo")
        assert err.notebook_id == "nb_typo"
        assert "nb_typo" in str(err)

    def test_accepts_method_id(self):
        # The CLI/_notebooks.get site forwards method_id for diagnostics.
        err = NotebookNotFoundError("nb_x", method_id="rwIQyf")
        assert err.method_id == "rwIQyf"


class TestUseFailsClosedBadId:
    """Bad notebook ID → no context write, exit 1."""

    def test_notebook_not_found_does_not_persist(self, runner, mock_auth, mock_context_file):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get = AsyncMock(
                side_effect=NotebookNotFoundError("nb_missing", method_id="rwIQyf"),
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                with patch(
                    "notebooklm.cli.session.resolve_notebook_id", new_callable=AsyncMock
                ) as mock_resolve:
                    mock_resolve.return_value = "nb_missing"

                    result = runner.invoke(cli, ["use", "nb_missing"])

        # Exit non-zero, never touched context.json.
        assert result.exit_code == 1
        assert not mock_context_file.exists()
        # Surface a clear "not found" message so the user can self-correct.
        assert "nb_missing" in result.output
        assert "not found" in result.output.lower() or "force" in result.output.lower()

    def test_generic_rpc_error_does_not_persist(self, runner, mock_auth, mock_context_file):
        """Network / RPC errors also fail closed — we can't confirm existence."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get = AsyncMock(
                side_effect=RPCError("server hung up", method_id="rwIQyf"),
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                with patch(
                    "notebooklm.cli.session.resolve_notebook_id", new_callable=AsyncMock
                ) as mock_resolve:
                    mock_resolve.return_value = "nb_rpc_fail"

                    result = runner.invoke(cli, ["use", "nb_rpc_fail"])

        assert result.exit_code == 1
        assert not mock_context_file.exists()


class TestUseFailsClosedGoodId:
    """Good notebook ID → context persisted, exit 0."""

    def test_good_id_persists_context(self, runner, mock_auth, mock_context_file):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get = AsyncMock(
                return_value=_make_notebook("nb_real", "Real Notebook"),
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                with patch(
                    "notebooklm.cli.session.resolve_notebook_id", new_callable=AsyncMock
                ) as mock_resolve:
                    mock_resolve.return_value = "nb_real"

                    result = runner.invoke(cli, ["use", "nb_real"])

        assert result.exit_code == 0
        assert mock_context_file.exists()
        data = json.loads(mock_context_file.read_text())
        assert data["notebook_id"] == "nb_real"
        assert data["title"] == "Real Notebook"


class TestUseForceFlag:
    """``--force`` bypasses the existence check entirely."""

    def test_force_with_bad_id_still_persists(self, runner, mock_context_file):
        """No client mocking needed — --force never calls the network."""
        result = runner.invoke(cli, ["use", "--force", "nb_offline"])

        assert result.exit_code == 0
        assert mock_context_file.exists()
        data = json.loads(mock_context_file.read_text())
        assert data["notebook_id"] == "nb_offline"
        # Banner clarifies the ID was not verified, so the user isn't misled.
        assert "not verified" in result.output.lower() or "force" in result.output.lower()

    def test_force_does_not_call_get(self, runner, mock_auth, mock_context_file):
        """--force is offline-safe: even a guaranteed-raise ``get`` is bypassed."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            # If --force *did* call get, this mock would surface the error.
            mock_client.notebooks.get = AsyncMock(
                side_effect=NotebookNotFoundError("should-not-be-called"),
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["use", "--force", "nb_force_id"])

        assert result.exit_code == 0
        assert mock_context_file.exists()
        # The get-mock side_effect would have surfaced if it had been invoked.
        mock_client.notebooks.get.assert_not_called()
