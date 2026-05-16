"""Tests for source CLI commands."""

import contextlib
import importlib
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import (
    Source,
    SourceFulltext,
    SourceNotFoundError,
    SourceProcessingError,
    SourceStatus,
    SourceTimeoutError,
)

from .conftest import create_mock_client, patch_client_for_module

source_module = importlib.import_module("notebooklm.cli.source")
helpers_module = importlib.import_module("notebooklm.cli.helpers")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


# =============================================================================
# SOURCE LIST TESTS
# =============================================================================


class TestSourceList:
    def test_source_list(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(id="src_1", title="Source One"),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "list", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Source One" in result.output or "src_1" in result.output

    def test_source_list_json_output(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(
                        id="src_1",
                        title="Test Source",
                        url="https://example.com",
                    ),
                ]
            )
            mock_client.notebooks.get = AsyncMock(return_value=MagicMock(title="Test Notebook"))
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "list", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "sources" in data
            assert data["count"] == 1
            assert data["sources"][0]["id"] == "src_1"

    def test_source_list_limit_caps_rows(self, runner, mock_auth):
        """`source list --limit N` returns at most N data rows (P6.T1 / I16)."""
        many = [Source(id=f"src_{i:02d}", title=f"Source {i:02d}") for i in range(20)]
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=many)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "list", "-n", "nb_123", "--limit", "4"])

            assert result.exit_code == 0, result.output
            for i in range(4):
                assert f"src_{i:02d}" in result.output
            for i in range(4, 20):
                assert f"src_{i:02d}" not in result.output

    def test_source_list_limit_json_caps_rows(self, runner, mock_auth):
        """`source list --limit N --json` caps the JSON `sources` array (P6.T1 / I16)."""
        many = [Source(id=f"src_{i:02d}", title=f"Source {i:02d}") for i in range(20)]
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=many)
            mock_client.notebooks.get = AsyncMock(return_value=MagicMock(title="Test"))
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "list", "-n", "nb_123", "--limit", "2", "--json"]
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["count"] == 2
            assert len(data["sources"]) == 2
            assert [s["id"] for s in data["sources"]] == ["src_00", "src_01"]

    def test_source_list_no_truncate_disables_ellipsis(self, runner, mock_auth):
        """`source list --no-truncate` shows full title without ellipsis (P6.T1 / I16).

        The default Title column uses Rich's ``overflow="ellipsis"`` so a
        title that exceeds the auto-detected terminal width is truncated
        with ``…``. ``--no-truncate`` flips the column to ``overflow="fold"``
        so the title wraps instead, preserving every character.
        """
        long_title = "X" * 200
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_long", title=long_title)]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "list", "-n", "nb_123", "--no-truncate"])

            assert result.exit_code == 0, result.output
            assert result.output.count("X") >= 200
            assert "…" not in result.output

    def test_source_list_default_truncates_long_title(self, runner, mock_auth):
        """Default rendering inserts an ellipsis for over-wide titles (P6.T1 / I16)."""
        long_title = "X" * 200
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_long", title=long_title)]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "list", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            assert result.output.count("X") < 200
            assert "…" in result.output


# =============================================================================
# SOURCE ADD TESTS
# =============================================================================


class TestSourceAdd:
    def test_source_add_url(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_url = AsyncMock(
                return_value=Source(
                    id="src_new",
                    title="Example",
                    url="https://example.com",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add", "https://example.com", "-n", "nb_123"]
                )

            assert result.exit_code == 0

    def test_source_add_youtube_url(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_url = AsyncMock(
                return_value=Source(
                    id="src_yt",
                    title="YouTube Video",
                    url="https://youtube.com/watch?v=abc",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add", "https://youtube.com/watch?v=abc123", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            mock_client.sources.add_url.assert_called()

    def test_source_add_text(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="My Text Source")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", "Some text content", "--type", "text", "-n", "nb_123"],
                )

            assert result.exit_code == 0

    def test_source_add_text_with_title(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Custom Title")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add",
                        "My notes",
                        "--type",
                        "text",
                        "--title",
                        "Custom Title",
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0

    def test_source_add_file(self, runner, mock_auth, tmp_path):
        # Create a temp file
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_file = AsyncMock(
                return_value=Source(id="src_file", title="test.pdf")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", str(test_file), "--type", "file", "-n", "nb_123"],
                )

            assert result.exit_code == 0

    def test_source_add_file_with_mime_type_emits_deprecation(self, runner, mock_auth, tmp_path):
        """``--mime-type`` on the file-source path is a no-op; warn user.

        The Drive path keeps ``--mime-type`` as a live, functional option, so
        the deprecation echo MUST be gated on ``detected_type == 'file'`` and
        must not fire on Drive sources. ``add_file`` is called WITHOUT the
        positional ``mime_type`` so the library-level ``DeprecationWarning``
        does not double up the signal.
        """
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_file = AsyncMock(
                return_value=Source(id="src_file", title="test.pdf")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add",
                        str(test_file),
                        "--type",
                        "file",
                        "--mime-type",
                        "application/pdf",
                        "-n",
                        "nb_123",
                    ],
                )

        assert result.exit_code == 0
        # ``click.echo(..., err=True)`` writes to stderr; Click's ``CliRunner``
        # mixes stderr into ``result.output`` by default (``mix_stderr=True``).
        # If the runner fixture is ever switched to ``mix_stderr=False`` this
        # assertion would silently pass — flip to ``result.stderr`` if so.
        assert "unused for file sources" in result.output
        # ``add_file`` must NOT receive the unused mime_type — passing it would
        # also trip the library-level DeprecationWarning, doubling the noise.
        call_kwargs = mock_client.sources.add_file.call_args.kwargs
        call_args = mock_client.sources.add_file.call_args.args
        assert "mime_type" not in call_kwargs
        # Positional args: (notebook_id, file_path). The third positional slot
        # is the deprecated mime_type; the CLI must not fill it.
        assert len(call_args) == 2

    def test_source_add_file_mime_type_suppressed_by_env(
        self, runner, mock_auth, tmp_path, monkeypatch
    ):
        """``NOTEBOOKLM_QUIET_DEPRECATIONS=1`` suppresses the file-source notice.

        Keeps CI logs clean for projects that have already migrated off the
        flag but cannot drop it from a shared invocation overnight.
        """
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_file = AsyncMock(
                return_value=Source(id="src_file", title="test.pdf")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add",
                        str(test_file),
                        "--type",
                        "file",
                        "--mime-type",
                        "application/pdf",
                        "-n",
                        "nb_123",
                    ],
                )

        assert result.exit_code == 0
        assert "unused for file sources" not in result.output

    def test_source_add_json_output(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_url = AsyncMock(
                return_value=Source(
                    id="src_new",
                    title="Example",
                    url="https://example.com",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add", "https://example.com", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["source"]["id"] == "src_new"

    def test_source_add_timeout_flag_threaded_to_client(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_url = AsyncMock(
                return_value=Source(id="src_t", title="X", url="https://example.com")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add",
                        "https://example.com",
                        "-n",
                        "nb_123",
                        "--timeout",
                        "120",
                    ],
                )

            assert result.exit_code == 0
            assert mock_client_cls.call_args.kwargs["timeout"] == 120.0

    def test_source_add_default_does_not_override_client_timeout(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_url = AsyncMock(
                return_value=Source(id="src_d", title="Y", url="https://example.com")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add", "https://example.com", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "timeout" not in mock_client_cls.call_args.kwargs


# =============================================================================
# SOURCE GET TESTS
# =============================================================================


class TestSourceGet:
    def test_source_get(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get = AsyncMock(
                return_value=Source(
                    id="src_123",
                    title="Test Source",
                    url="https://example.com",
                    created_at=datetime(2024, 1, 1),
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "get", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Test Source" in result.output
            assert "src_123" in result.output

    def test_source_get_not_found(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list to return empty (no match for resolve_source_id)
            mock_client.sources.list = AsyncMock(return_value=[])
            mock_client.sources.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "get", "nonexistent", "-n", "nb_123"])

            # Now exits with error from resolve_source_id (no match)
            assert result.exit_code == 1
            assert "No source found" in result.output

    # -------------------------------------------------------------------------
    # C1 (Phase 3) — get-on-not-found now exits 1 (was 0). The new tests below
    # cover BOTH code paths for the not-found branch:
    #
    #   * Path A: input ID is ≥20 chars, so ``_resolve_partial_id`` skips the
    #     list() round-trip (helpers.py:783-785) and returns the input as-is.
    #     The backend ``client.sources.get`` then returns ``None``.
    #   * Path B: input ID is <20 chars, ``_resolve_partial_id`` resolves it via
    #     a list() match (so the partial-resolve "not found" ClickException
    #     branch is intentionally NOT exercised here — that path is unchanged
    #     and tested by ``test_source_get_not_found`` above), but the backend
    #     get returns ``None`` (e.g. concurrent delete from another session).
    #
    # Each path is exercised in text and ``--json`` mode.
    # -------------------------------------------------------------------------

    def test_source_get_not_found_pathA_long_id_text_exits_1(self, runner, mock_auth):
        """Path A: ID ≥20 chars skips partial-resolve; backend None → exit 1."""
        long_id = "a" * 24  # ≥20 chars triggers the skip in _resolve_partial_id
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # list() must NOT be called on this path; assert below.
            mock_client.sources.list = AsyncMock(return_value=[])
            mock_client.sources.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "get", long_id, "-n", "nb_123"])

            assert result.exit_code == 1, result.output
            assert "Source not found" in result.output
            mock_client.sources.list.assert_not_called()

    def test_source_get_not_found_pathA_long_id_json_exits_1(self, runner, mock_auth):
        """Path A under ``--json``: typed JSON error doc + exit 1."""
        long_id = "a" * 24
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=[])
            mock_client.sources.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "get", long_id, "-n", "nb_123", "--json"])

            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "NOT_FOUND"
            assert "Source not found" in data["message"]
            assert data["source_id"] == long_id
            mock_client.sources.list.assert_not_called()

    def test_source_get_not_found_pathB_resolved_then_none_text_exits_1(self, runner, mock_auth):
        """Path B: partial-resolve succeeds, backend get() returns None → exit 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Resolution succeeds (list contains a matching item) but the
            # subsequent get() returns None (race: source deleted between
            # list and get).
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_resolved", title="Doomed")]
            )
            mock_client.sources.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "get", "src_resolved", "-n", "nb_123"])

            assert result.exit_code == 1, result.output
            assert "Source not found" in result.output

    def test_source_get_not_found_pathB_resolved_then_none_json_exits_1(self, runner, mock_auth):
        """Path B under ``--json``: typed JSON error doc + exit 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_resolved", title="Doomed")]
            )
            mock_client.sources.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "get", "src_resolved", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "NOT_FOUND"
            assert "Source not found" in data["message"]
            assert data["source_id"] == "src_resolved"


# =============================================================================
# SOURCE DELETE TESTS
# =============================================================================


class TestSourceDelete:
    def test_source_delete(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for source delete resolution
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", "src_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Deleted source" in result.output
            mock_client.sources.delete.assert_called_once_with("nb_123", "src_123")

    def test_source_delete_failure(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for source delete resolution
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", "src_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Delete may have failed" in result.output

    def test_source_delete_full_uuid_skips_source_list(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock()
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            source_id = "03abe51c-d8df-43ba-ae2d-0efe02c71c4a"
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", source_id, "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            mock_client.sources.list.assert_not_called()
            mock_client.sources.delete.assert_called_once_with("nb_123", source_id)

    def test_source_delete_long_hex_string_does_not_skip_source_list(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=[])
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            source_id = "03abe51cd8df43baae2d0efe02c71c4a"
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", source_id, "-n", "nb_123", "-y"])

            assert result.exit_code == 1
            mock_client.sources.list.assert_called_once_with("nb_123")
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_title_suggests_delete_by_title(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(
                        id="03abe51c-d8df-43ba-ae2d-0efe02c71c4a",
                        title="Emails_Verzonden_2026_02",
                    )
                ]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "delete", "Emails_Verzonden_2026_02", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 1
            assert "delete-by-title" in result.output
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_unknown_long_string_fails_locally(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "delete", "Emails_Verzonden_2025-Q1.txt", "-n", "nb_123", "-y"],
                )

            assert result.exit_code == 1
            assert "No source found starting with" in result.output
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_ambiguous_partial_id(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(id="src_123", title="First Source"),
                    Source(id="src_456", title="Second Source"),
                ]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", "src", "-n", "nb_123", "-y"])

            assert result.exit_code == 1
            assert "Ambiguous ID 'src' matches 2 sources" in result.output
            mock_client.sources.delete.assert_not_called()


class TestSourceDeleteByTitle:
    def test_source_delete_by_title(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "delete-by-title", "Test Source", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 0
            assert "Deleted source" in result.output
            mock_client.sources.delete.assert_called_once_with("nb_123", "src_123")

    def test_source_delete_by_title_duplicate_titles(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(id="src_123", title="Duplicate"),
                    Source(id="src_456", title="Duplicate"),
                ]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "delete-by-title", "Duplicate", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 1
            assert "Delete by ID instead" in result.output
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_by_title_not_found(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=[])
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "delete-by-title", "Missing", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 1
            assert "No source found with title" in result.output
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_by_title_confirmation_shows_title_and_id(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "delete-by-title", "Test Source", "-n", "nb_123"],
                    input="y\n",
                )

            assert result.exit_code == 0
            assert "Delete source 'Test Source' (src_123)?" in result.output
            mock_client.sources.delete.assert_called_once_with("nb_123", "src_123")


# =============================================================================
# SOURCE RENAME TESTS
# =============================================================================


class TestSourceRename:
    def test_source_rename(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Old Title")]
            )
            mock_client.sources.rename = AsyncMock(
                return_value=Source(id="src_123", title="New Title")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "rename", "src_123", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Renamed source" in result.output
            assert "New Title" in result.output


# =============================================================================
# SOURCE REFRESH TESTS
# =============================================================================


class TestSourceRefresh:
    def test_source_refresh(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Original Source")]
            )
            mock_client.sources.refresh = AsyncMock(
                return_value=Source(id="src_123", title="Refreshed Source")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "refresh", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Source refreshed" in result.output

    def test_source_refresh_no_result(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Original Source")]
            )
            mock_client.sources.refresh = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "refresh", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Refresh returned no result" in result.output


# =============================================================================
# SOURCE ADD-DRIVE TESTS
# =============================================================================


class TestSourceAddDrive:
    def test_source_add_drive_google_doc(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_drive = AsyncMock(
                return_value=Source(
                    id="src_drive",
                    title="My Google Doc",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add-drive", "drive_file_id", "My Google Doc", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Added Drive source" in result.output

    def test_source_add_drive_pdf(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_drive = AsyncMock(
                return_value=Source(
                    id="src_drive",
                    title="PDF from Drive",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add-drive",
                        "file_id",
                        "PDF Title",
                        "--mime-type",
                        "pdf",
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0

    def test_source_add_drive_mime_type_no_deprecation_warning(self, runner, mock_auth):
        """Regression guard: Drive ``--mime-type`` MUST stay deprecation-free.

        The Drive ``--mime-type`` flag is live — it selects the ``DriveMimeType``
        value the API consumes (``google-doc``/``google-slides``/
        ``google-sheets``/``pdf``). The file-source deprecation message must
        NEVER appear on this command, regardless of which Drive MIME the user
        picks.
        """
        for choice in ("google-doc", "google-slides", "google-sheets", "pdf"):
            with patch_client_for_module("source") as mock_client_cls:
                mock_client = create_mock_client()
                mock_client.sources.add_drive = AsyncMock(
                    return_value=Source(id="src_drive", title="My Drive Source")
                )
                mock_client_cls.return_value = mock_client

                with patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch:
                    mock_fetch.return_value = ("csrf", "session")
                    result = runner.invoke(
                        cli,
                        [
                            "source",
                            "add-drive",
                            "file_id",
                            "Drive Title",
                            "--mime-type",
                            choice,
                            "-n",
                            "nb_123",
                        ],
                    )

            assert result.exit_code == 0, f"Drive --mime-type {choice} failed: {result.output}"
            assert "unused for file sources" not in result.output, (
                f"Drive --mime-type={choice} unexpectedly triggered the "
                f"file-source deprecation notice"
            )


# =============================================================================
# SOURCE ADD-RESEARCH TESTS
# =============================================================================


class TestSourceAddResearch:
    def test_add_research_with_import_all_uses_retry_helper(self, runner, mock_auth):
        with (
            patch_client_for_module("source") as mock_client_cls,
            patch.object(
                helpers_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value={"task_id": "task_123"})
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "report": "# Report",
                }
            )
            mock_import.return_value = [{"id": "src_1", "title": "Source 1"}]
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add-research",
                        "AI papers",
                        "--mode",
                        "deep",
                        "--import-all",
                        "-n",
                        "nb_123",
                    ],
                )

        assert result.exit_code == 0
        assert "Imported 1 sources" in result.output
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_123",
            [{"title": "Source 1", "url": "http://example.com"}],
            max_elapsed=1800,
        )

    def test_add_research_with_import_all_cited_only(self, runner, mock_auth):
        with (
            patch_client_for_module("source") as mock_client_cls,
            patch.object(
                helpers_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value={"task_id": "task_123"})
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "sources": [
                        {"title": "Cited", "url": "https://example.com/cited"},
                        {"title": "Uncited", "url": "https://example.com/uncited"},
                    ],
                    "report": "Report cites https://example.com/cited",
                }
            )
            mock_import.return_value = [{"id": "src_1", "title": "Cited"}]
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add-research",
                        "AI papers",
                        "--import-all",
                        "--cited-only",
                        "-n",
                        "nb_123",
                    ],
                )

        assert result.exit_code == 0
        assert "Imported 1 sources" in result.output
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_123",
            [{"title": "Cited", "url": "https://example.com/cited"}],
            max_elapsed=1800,
        )

    def test_add_research_cited_only_requires_import_all(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        result = runner.invoke(
            cli,
            ["source", "add-research", "AI papers", "--cited-only", "-n", "nb_123"],
        )

        # ``click.UsageError`` exits 2 — Click's standard convention.
        assert result.exit_code == 2
        assert "--cited-only requires --import-all" in result.output

    def test_add_research_timeout_flag_threaded_to_import_with_retry(self, runner, mock_auth):
        with (
            patch_client_for_module("source") as mock_client_cls,
            patch.object(
                helpers_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value={"task_id": "task_t1"})
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_t1",
                    "sources": [{"title": "S", "url": "http://example.com"}],
                    "report": "",
                }
            )
            mock_import.return_value = [{"id": "src_t1", "title": "S"}]
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add-research",
                        "topic",
                        "--import-all",
                        "--timeout",
                        "600",
                        "-n",
                        "nb_123",
                    ],
                )

        assert result.exit_code == 0
        assert mock_import.await_args.kwargs["max_elapsed"] == 600


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


# =============================================================================
# SOURCE GUIDE TESTS
# =============================================================================


class TestSourceGuide:
    def test_source_guide_with_summary_and_keywords(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(
                return_value={
                    "summary": "This is a **test** summary about AI.",
                    "keywords": ["AI", "machine learning", "data science"],
                }
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "guide", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Summary" in result.output
            assert "test" in result.output
            assert "Keywords" in result.output
            assert "AI" in result.output

    def test_source_guide_no_guide_available(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(return_value={"summary": "", "keywords": []})
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "guide", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No guide available" in result.output

    def test_source_guide_json_output(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(
                return_value={"summary": "Test summary", "keywords": ["keyword1", "keyword2"]}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "guide", "src_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["source_id"] == "src_123"
            assert data["summary"] == "Test summary"
            assert data["keywords"] == ["keyword1", "keyword2"]

    def test_source_guide_summary_only(self, runner, mock_auth):
        """Test that summary is displayed even when keywords are empty."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(
                return_value={"summary": "Summary without keywords", "keywords": []}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "guide", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Summary" in result.output
            assert "Summary without keywords" in result.output
            assert "No guide available" not in result.output

    def test_source_guide_keywords_only(self, runner, mock_auth):
        """Test that keywords are displayed even when summary is empty."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(
                return_value={"summary": "", "keywords": ["AI", "ML", "Data"]}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "guide", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Keywords" in result.output
            assert "AI" in result.output
            assert "No guide available" not in result.output


# =============================================================================
# SOURCE STALE TESTS
# =============================================================================


class TestSourceStale:
    def test_source_stale_is_stale(self, runner, mock_auth):
        """Test exit code 0 when source is stale (needs refresh)."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.check_freshness = AsyncMock(return_value=False)  # Not fresh = stale
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "stale", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0  # 0 = stale (condition is true)
            assert "stale" in result.output.lower()
            assert "refresh" in result.output.lower()

    def test_source_stale_is_fresh(self, runner, mock_auth):
        """Test exit code 1 when source is fresh (no refresh needed)."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.check_freshness = AsyncMock(return_value=True)  # Fresh
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "stale", "src_123", "-n", "nb_123"])

            assert result.exit_code == 1  # 1 = not stale (condition is false)
            assert "fresh" in result.output.lower()


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


class TestSourceCommandsExist:
    def test_source_group_exists(self, runner):
        result = runner.invoke(cli, ["source", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "add" in result.output
        assert "delete" in result.output
        assert "guide" in result.output
        assert "stale" in result.output

    def test_source_add_command_exists(self, runner):
        result = runner.invoke(cli, ["source", "add", "--help"])
        assert result.exit_code == 0
        assert "CONTENT" in result.output
        assert "--type" in result.output
        assert "--notebook" in result.output or "-n" in result.output

    def test_source_list_command_exists(self, runner):
        result = runner.invoke(cli, ["source", "list", "--help"])
        assert result.exit_code == 0
        assert "--notebook" in result.output or "-n" in result.output

    def test_source_guide_command_exists(self, runner):
        result = runner.invoke(cli, ["source", "guide", "--help"])
        assert result.exit_code == 0
        assert "SOURCE_ID" in result.output
        assert "--json" in result.output

    def test_source_stale_command_exists(self, runner):
        result = runner.invoke(cli, ["source", "stale", "--help"])
        assert result.exit_code == 0
        assert "SOURCE_ID" in result.output
        assert "exit code" in result.output.lower()


# =============================================================================
# SOURCE ADD AUTO-DETECT TESTS
# =============================================================================


class TestSourceAddAutoDetect:
    def test_source_add_autodetect_file(self, runner, mock_auth, tmp_path):
        """Pass a real file path without --type; should auto-detect as 'file'."""
        test_file = tmp_path / "notes.txt"
        test_file.write_text("Some file content")

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_file = AsyncMock(
                return_value=Source(id="src_file", title="notes.txt")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", str(test_file), "-n", "nb_123"],
                )

            assert result.exit_code == 0
            mock_client.sources.add_file.assert_called_once()

    def test_source_add_autodetect_plain_text(self, runner, mock_auth):
        """Pass plain text (not URL, not existing path) without --type.

        Should auto-detect as 'text' with default title 'Pasted Text'.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Pasted Text")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", "This is just some plain text content", "-n", "nb_123"],
                )

            assert result.exit_code == 0
            # Verify add_text was called with the default "Pasted Text" title
            mock_client.sources.add_text.assert_called_once()
            call_args = mock_client.sources.add_text.call_args
            assert call_args[0][1] == "Pasted Text"  # title arg

    def test_source_add_autodetect_text_with_custom_title(self, runner, mock_auth):
        """Pass plain text without --type but with --title.

        Title should be the custom title, not 'Pasted Text'.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Custom Title")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add",
                        "This is just some plain text content",
                        "--title",
                        "Custom Title",
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0
            mock_client.sources.add_text.assert_called_once()
            call_args = mock_client.sources.add_text.call_args
            assert call_args[0][1] == "Custom Title"  # title arg

    def test_source_add_file_with_custom_title_passes_title_through(
        self, runner, mock_auth, tmp_path
    ):
        """Regression test for #313: ``--title`` must reach add_file when the
        argument is an existing file path (auto-detected as 'file').
        """
        test_file = tmp_path / "boring-filename.md"
        test_file.write_text("# content\n")

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_file = AsyncMock(
                return_value=Source(id="src_md", title="Real Intended Title")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add",
                        str(test_file),
                        "--title",
                        "Real Intended Title",
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0
            mock_client.sources.add_file.assert_called_once()
            call_kwargs = mock_client.sources.add_file.call_args.kwargs
            assert call_kwargs.get("title") == "Real Intended Title"


# =============================================================================
# SOURCE ADD PATH-SHAPED MISSING-FILE WARNING (P4.T4 / I8)
# =============================================================================
#
# Audit row I8: ``notebooklm source add ./missing.md`` currently silently
# falls through to inline-text ingestion. Users (and AI agents) reading the
# CLI back-channel cannot distinguish "I sent the literal string ``./missing.md``
# as note content" from "the file uploaded successfully" — the success line
# looks identical. The remediation here is a stderr warning when the input
# *looks like* a path (contains ``/`` OR ends in a known file extension)
# AND the path does not exist; the source is still added as text so the
# inferred-text behavior is preserved (no breaking exit-code change). Explicit
# ``--type text`` suppresses the warning because the user has stated intent
# unambiguously.


class TestSourceAddPathShapedMissing:
    """P4.T4 / I8: warn when path-shaped arg doesn't exist on disk."""

    def test_path_shaped_missing_emits_stderr_warning(self, runner, mock_auth):
        """``./missing.md`` (slash + known ext, doesn't exist) -> stderr warn.

        Source is still added as text — warning is advisory, not fatal.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Pasted Text")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "add", "./missing.md", "-n", "nb_123"])

        # Source must still be added (advisory warn, not fatal).
        assert result.exit_code == 0
        mock_client.sources.add_text.assert_called_once()
        # Click's CliRunner mixes stderr into ``result.output`` by default;
        # both the path-shape phrasing and the original argument should
        # surface so a user can see what triggered the heuristic. Assert
        # the full phrase (not an OR) so a future edit that drops "looks
        # like a path" or "does not exist" individually still trips this.
        assert "looks like a path but does not exist" in result.output
        assert "./missing.md" in result.output

    def test_path_shaped_missing_with_extension_only_warns(self, runner, mock_auth):
        """No slash but a known extension still triggers the heuristic.

        ``missing.md`` (no slash, has ``.md``) is path-shaped: ``./missing.md``
        without the leading dot-slash is a common shell mistake.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Pasted Text")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "add", "missing.md", "-n", "nb_123"])

        assert result.exit_code == 0
        mock_client.sources.add_text.assert_called_once()
        # Tight full-phrase match — see the slash-case test above for rationale.
        assert "looks like a path but does not exist" in result.output
        assert "missing.md" in result.output

    def test_explicit_type_text_suppresses_warning(self, runner, mock_auth):
        """``--type text`` is an explicit user override — no warning.

        The user has stated intent: "treat this as text, not a path." The
        heuristic must respect that and stay silent.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Pasted Text")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add",
                        "./missing.md",
                        "--type",
                        "text",
                        "-n",
                        "nb_123",
                    ],
                )

        assert result.exit_code == 0
        mock_client.sources.add_text.assert_called_once()
        assert "looks like a path" not in result.output
        assert "does not exist" not in result.output

    def test_pure_text_no_path_shape_no_warning(self, runner, mock_auth):
        """Plain prose with no slash and no known extension -> no warning.

        Regression guard: the heuristic must not fire on legitimate inline
        text content like ``"My notes here"``.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Pasted Text")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", "This is just some plain prose content", "-n", "nb_123"],
                )

        assert result.exit_code == 0
        mock_client.sources.add_text.assert_called_once()
        assert "looks like a path" not in result.output
        assert "does not exist" not in result.output

    def test_existing_path_no_warning_uploads_as_file(self, runner, mock_auth, tmp_path):
        """Existing path-shaped arg uploads as ``file`` — no warning emitted.

        The warning fires only when the path-shape heuristic matches AND
        the file does not exist; an existing file follows the file-upload
        branch as before.
        """
        test_file = tmp_path / "real.md"
        test_file.write_text("# real content\n")

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_file = AsyncMock(
                return_value=Source(id="src_file", title="real.md")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "add", str(test_file), "-n", "nb_123"])

        assert result.exit_code == 0
        mock_client.sources.add_file.assert_called_once()
        assert "looks like a path" not in result.output
        assert "does not exist" not in result.output


# =============================================================================
# SOURCE FULLTEXT TESTS
# =============================================================================


class TestSourceFulltext:
    def test_source_fulltext_console_output(self, runner, mock_auth):
        """Short content (<= 2000 chars) is displayed in full."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Test Source",
                    content="This is the full text content.",
                    char_count=30,
                    url=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "fulltext", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "src_123" in result.output
            assert "Test Source" in result.output
            assert "This is the full text content." in result.output
            # Should NOT show truncation message for short content
            assert "more chars" not in result.output

    def test_source_fulltext_truncated_output(self, runner, mock_auth):
        """Long content (> 2000 chars) is truncated with a 'more chars' message."""
        long_content = "A" * 3000
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Long Source",
                    content=long_content,
                    char_count=3000,
                    url=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "fulltext", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "more chars" in result.output

    def test_source_fulltext_save_to_file(self, runner, mock_auth, tmp_path):
        """-o flag saves content to file."""
        output_file = tmp_path / "output.txt"
        content = "Full text content to save."

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Test Source",
                    content=content,
                    char_count=len(content),
                    url=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "fulltext", "src_123", "-n", "nb_123", "-o", str(output_file)],
                )

            assert result.exit_code == 0
            assert "Saved" in result.output
            assert output_file.read_text(encoding="utf-8") == content

    def test_source_fulltext_json_output(self, runner, mock_auth):
        """--json outputs JSON with fulltext fields."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Test Source",
                    content="Some content",
                    char_count=12,
                    url=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "fulltext", "src_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["source_id"] == "src_123"
            assert data["title"] == "Test Source"
            assert data["content"] == "Some content"
            assert data["char_count"] == 12

    def test_source_fulltext_format_markdown_propagates(self, runner, mock_auth):
        """`-f markdown` propagates output_format='markdown' to the API."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="MD Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="MD Source",
                    content="# Heading\n\n[link](https://example.com)",
                    char_count=39,
                    url=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "fulltext", "src_123", "-n", "nb_123", "-f", "markdown"]
                )

            assert result.exit_code == 0
            mock_client.sources.get_fulltext.assert_awaited_once()
            _, kwargs = mock_client.sources.get_fulltext.call_args
            assert kwargs["output_format"] == "markdown"
            # Markdown link should round-trip verbatim, not be eaten by Rich markup
            assert "[link](https://example.com)" in result.output

    def test_source_fulltext_with_url(self, runner, mock_auth):
        """Shows URL field when present in fulltext."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Web Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Web Source",
                    content="Web page content.",
                    char_count=17,
                    url="https://example.com/page",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "fulltext", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "https://example.com/page" in result.output


# =============================================================================
# SOURCE WAIT TESTS
# =============================================================================


class TestSourceWait:
    def test_source_wait_success(self, runner, mock_auth):
        """wait_until_ready returns a Source → prints 'ready'."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                return_value=Source(id="src_123", title="Test Source", status=2)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "ready" in result.output.lower()

    def test_source_wait_success_with_title(self, runner, mock_auth):
        """Source has a title → prints the title after 'ready' message."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="My Source Title")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                return_value=Source(id="src_123", title="My Source Title", status=2)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "My Source Title" in result.output

    def test_source_wait_success_json(self, runner, mock_auth):
        """--json output on successful wait."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                return_value=Source(id="src_123", title="Test Source", status=2)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["source_id"] == "src_123"
            assert data["status"] == "ready"

    def test_source_wait_not_found(self, runner, mock_auth):
        """Raises SourceNotFoundError → exit code 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceNotFoundError("src_123")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 1
            assert "not found" in result.output.lower()

    def test_source_wait_not_found_json(self, runner, mock_auth):
        """--json on SourceNotFoundError → JSON with status 'not_found', exit 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceNotFoundError("src_123")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["status"] == "not_found"
            assert data["source_id"] == "src_123"

    def test_source_wait_processing_error(self, runner, mock_auth):
        """Raises SourceProcessingError → exit code 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceProcessingError("src_123", status=3)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 1
            assert "processing failed" in result.output.lower()

    def test_source_wait_processing_error_json(self, runner, mock_auth):
        """--json on SourceProcessingError → JSON with status 'error', exit 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceProcessingError("src_123", status=3)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["status"] == "error"
            assert data["source_id"] == "src_123"
            assert data["status_code"] == 3

    def test_source_wait_timeout(self, runner, mock_auth):
        """Raises SourceTimeoutError → exit code 2."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceTimeoutError("src_123", timeout=30.0, last_status=1)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 2
            assert "timeout" in result.output.lower()

    def test_source_wait_timeout_interval_forwarded(self, runner, mock_auth):
        """`source wait <id> --timeout 60 --interval 5` plumbs both into
        wait_until_ready (P5.T1 / I6).

        The `--interval` flag is NEW for `source wait` (previously it accepted
        only `--timeout`). After this change, `source wait` matches the
        uniform `--timeout`/`--interval` surface shared by `artifact wait` and
        `generate <kind> --wait`. The three-way exit policy (0 ready / 1
        not-found-or-error / 2 timeout) is preserved by the success path here
        — exit code stays 0 when the source becomes ready.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                return_value=Source(id="src_123", title="Test Source", status=2)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "wait",
                        "src_123",
                        "-n",
                        "nb_123",
                        "--timeout",
                        "60",
                        "--interval",
                        "5",
                    ],
                )

            assert result.exit_code == 0, result.output
            mock_client.sources.wait_until_ready.assert_awaited_once()
            kwargs = mock_client.sources.wait_until_ready.await_args.kwargs
            assert kwargs.get("timeout") == 60.0
            # `wait_until_ready` exposes the cadence as `initial_interval`.
            assert kwargs.get("initial_interval") == 5.0, (
                f"expected --interval=5 to plumb into wait_until_ready, got kwargs={kwargs}"
            )

    def test_source_wait_timeout_json(self, runner, mock_auth):
        """--json on SourceTimeoutError → JSON with status 'timeout', exit 2."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceTimeoutError("src_123", timeout=30.0, last_status=1)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 2
            data = json.loads(result.output)
            assert data["status"] == "timeout"
            assert data["source_id"] == "src_123"
            assert data["timeout_seconds"] == 30
            assert data["last_status_code"] == 1

    def test_source_wait_invokes_console_status(self, runner, mock_auth):
        """`source wait` wraps the polling call in `console.status` (P5.T2 / I7).

        The spinner replaces the static "Waiting for source ..." print with a
        live transient line that includes the source ID. Asserts the wrap by
        patching `notebooklm.cli.source.console.status` and confirming it is
        invoked exactly once with a message that mentions the source. Does not
        assert under `--json` because the JSON path intentionally suppresses
        the spinner to keep stdout pure JSON.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                return_value=Source(id="src_123", title="Test Source", status=2)
            )
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
                patch.object(source_module.console, "status") as mock_status,
            ):
                mock_fetch.return_value = ("csrf", "session")
                mock_status.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mock_status.return_value.__exit__ = MagicMock(return_value=False)
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            assert mock_status.called, "expected console.status to wrap the wait call"
            status_msg = mock_status.call_args.args[0]
            assert "source" in status_msg.lower() or "src_123" in status_msg, (
                f"expected status message to describe the source wait, got: {status_msg!r}"
            )

    def test_source_wait_json_skips_console_status(self, runner, mock_auth):
        """`source wait --json` must NOT invoke console.status (stdout stays JSON).

        The spinner is suppressed under JSON mode so automation parsing stdout
        does not see Rich escape sequences leak in (P5.T2 / I7).
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                return_value=Source(id="src_123", title="Test Source", status=2)
            )
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
                patch.object(source_module.console, "status") as mock_status,
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 0, result.output
            assert not mock_status.called, (
                "console.status must NOT be invoked under --json (would leak ANSI into stdout)"
            )

    def test_source_wait_sigint_prints_resume_hint_and_exits_130(self, runner, mock_auth):
        """Ctrl-C during ``source wait`` exits 130 with a parallel resume hint
        naming the source id (M2 / P5.T3).

        Sources have no separate ``poll`` command — re-running the same wait
        IS the resume — so the hint shape is
        ``Cancelled. Resume with: notebooklm source wait <source_id>`` rather
        than the ``artifact poll <task_id>`` shape used by the other two
        long-running paths.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_sigint", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(side_effect=KeyboardInterrupt)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_sigint", "-n", "nb_123"])

        assert result.exit_code == 130, (
            f"expected SIGINT exit 130, got {result.exit_code}; output={result.output!r}"
        )
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "Cancelled. Resume with: notebooklm source wait src_sigint" in combined, (
            f"expected parallel resume hint with source id; got: {combined!r}"
        )

    def test_source_wait_sigint_json_emits_cancelled_envelope(self, runner, mock_auth):
        """Ctrl-C under ``source wait --json`` emits a CANCELLED envelope, exits 130
        (M2 / P5.T3)."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_json_sigint", title="T")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(side_effect=KeyboardInterrupt)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "wait", "src_json_sigint", "-n", "nb_123", "--json"]
                )

        assert result.exit_code == 130
        assert '"code": "CANCELLED"' in result.output
        assert "notebooklm source wait src_json_sigint" in result.output


# =============================================================================
# SOURCE CLEAN TESTS
# =============================================================================


def _src(
    sid: str,
    *,
    title: str | None = None,
    url: str | None = None,
    status: int = SourceStatus.READY,
    created_at: datetime | None = None,
) -> Source:
    """Build a Source fixture with sensible defaults for clean tests."""
    return Source(id=sid, title=title, url=url, status=status, created_at=created_at)


class TestSourceCleanClassify:
    """Unit tests for the pure-function ``_classify_junk_sources`` helper.

    These are independent of Click/CLI invocation so they're fast and exercise
    every branch of the classification logic without mocking the client.
    """

    def test_empty_notebook_returns_nothing(self):
        assert source_module._classify_junk_sources([]) == []

    def test_error_status_is_flagged(self):
        s = _src("src_e", status=SourceStatus.ERROR, url="https://ex.com/a")
        out = source_module._classify_junk_sources([s])
        assert [(c[0], c[3]) for c in out] == [("src_e", "error_status")]

    def test_unknown_status_is_not_flagged(self):
        # Unrecognized status codes must NOT be auto-deleted; they may
        # represent future NotebookLM states or missing-status payloads.
        s = _src("src_u", status=99, url="https://ex.com/a")
        assert source_module._classify_junk_sources([s]) == []

    def test_zero_status_is_not_flagged(self):
        # status=0 maps to "unknown" via the truthy fallback. Must NOT delete.
        s = _src("src_z", status=0, url="https://ex.com/a")
        assert source_module._classify_junk_sources([s]) == []

    def test_processing_status_is_not_flagged(self):
        s = _src("src_p", status=SourceStatus.PROCESSING, url="https://ex.com/a")
        assert source_module._classify_junk_sources([s]) == []

    @pytest.mark.parametrize(
        "title",
        [
            "Access Denied",
            "403 Forbidden",
            "404 Not Found",
            "Just a Moment...",
            "Attention Required! | Cloudflare",
            "Security check",
            "CAPTCHA verification",
            "  403  ",
        ],
    )
    def test_gateway_titles_are_flagged(self, title):
        s = _src("src_g", title=title, url="https://ex.com/a")
        out = source_module._classify_junk_sources([s])
        assert [(c[0], c[3]) for c in out] == [("src_g", "gateway_title")]

    def test_legitimate_titles_starting_with_digits_are_not_flagged(self):
        # "404 Not Found" is a gateway title, but "100 Ways to ..." is not.
        s = _src("src_ok", title="100 Ways to Cook Pasta", url="https://ex.com/a")
        assert source_module._classify_junk_sources([s]) == []

    def test_url_title_on_ready_source_is_not_deleted(self):
        # Regression: PR review caught that URL-as-title was being treated as
        # junk, which deletes legitimate in-flight sources (Source.title is
        # documented to "may be URL if not yet processed").
        s = _src(
            "src_url",
            title="https://example.com/article",
            url="https://example.com/article",
        )
        assert source_module._classify_junk_sources([s]) == []

    def test_dedup_keeps_oldest_and_flags_later_copies(self):
        # Oldest at t=0; two later duplicates.
        sources = [
            _src("src_3", url="https://ex.com/a", created_at=datetime(2024, 3, 1)),
            _src("src_1", url="https://ex.com/a", created_at=datetime(2024, 1, 1)),
            _src("src_2", url="https://ex.com/a", created_at=datetime(2024, 2, 1)),
        ]
        out = source_module._classify_junk_sources(sources)
        deleted_ids = sorted(c[0] for c in out)
        assert deleted_ids == ["src_2", "src_3"]
        assert all(c[3].startswith("duplicate_of:src_1"[:21]) for c in out)

    def test_dedup_when_oldest_is_error(self):
        # First copy (oldest) is error → flagged as error_status, NOT recorded
        # in seen_urls. Second copy becomes the kept anchor; third copy
        # deduped against it. Both deletions report their own reason.
        sources = [
            _src(
                "src_e",
                url="https://ex.com/a",
                status=SourceStatus.ERROR,
                created_at=datetime(2024, 1, 1),
            ),
            _src("src_ok", url="https://ex.com/a", created_at=datetime(2024, 2, 1)),
            _src("src_dup", url="https://ex.com/a", created_at=datetime(2024, 3, 1)),
        ]
        out = source_module._classify_junk_sources(sources)
        by_id = {c[0]: c[3] for c in out}
        assert set(by_id) == {"src_e", "src_dup"}
        assert by_id["src_e"] == "error_status"
        assert by_id["src_dup"].startswith("duplicate_of:")

    def test_dedup_preserves_query_string(self):
        # Different YouTube video IDs (via ?v=) must NOT be collapsed.
        sources = [
            _src("yt_a", url="https://youtube.com/watch?v=AAA"),
            _src("yt_b", url="https://youtube.com/watch?v=BBB"),
        ]
        assert source_module._classify_junk_sources(sources) == []

    def test_dedup_strips_fragment(self):
        sources = [
            _src("src_1", url="https://ex.com/a#top", created_at=datetime(2024, 1, 1)),
            _src("src_2", url="https://ex.com/a#bottom", created_at=datetime(2024, 2, 1)),
        ]
        out = source_module._classify_junk_sources(sources)
        assert [c[0] for c in out] == ["src_2"]

    def test_dedup_is_case_insensitive_on_scheme_and_host(self):
        # Per RFC 3986, scheme and host are case-insensitive, so mixed-case
        # copies of the same URL must be recognised as duplicates.
        sources = [
            _src("src_1", url="https://Example.COM/a", created_at=datetime(2024, 1, 1)),
            _src("src_2", url="HTTPS://example.com/a", created_at=datetime(2024, 2, 1)),
        ]
        out = source_module._classify_junk_sources(sources)
        assert [c[0] for c in out] == ["src_2"]

    def test_undated_sources_go_to_end_of_sort(self):
        # If src_undated were placed at position 0 (epoch sentinel), it would
        # be kept and src_dated deleted as a duplicate. With float('inf') the
        # dated one wins.
        sources = [
            _src("src_undated", url="https://ex.com/a", created_at=None),
            _src("src_dated", url="https://ex.com/a", created_at=datetime(2024, 1, 1)),
        ]
        out = source_module._classify_junk_sources(sources)
        assert [c[0] for c in out] == ["src_undated"]

    def test_source_with_no_url_is_not_deduped(self):
        # Text-only sources have url=None — they must never be deduped together.
        sources = [
            _src("src_1", title="Note A"),
            _src("src_2", title="Note B"),
        ]
        assert source_module._classify_junk_sources(sources) == []


class TestSourceCleanCommand:
    """End-to-end Click invocation tests for the ``source clean`` command."""

    def _patch_clean(self, sources):
        """Return a context manager that wires up a mock client returning sources."""
        return _CleanPatch(sources)

    def test_already_clean_short_circuits(self, runner, mock_auth):
        with self._patch_clean([_src("src_1", title="Page", url="https://ex.com/a")]) as mc:
            result = runner.invoke(cli, ["source", "clean", "-n", "nb_123", "-y"])
            assert result.exit_code == 0
            assert "already clean" in result.output.lower()
            mc.sources.delete.assert_not_called()

    def test_dry_run_shows_table_and_skips_delete(self, runner, mock_auth):
        sources = [
            _src("src_err", title="oops", status=SourceStatus.ERROR),
            _src("src_block", title="Just a Moment...", url="https://ex.com/x"),
        ]
        with self._patch_clean(sources) as mc:
            result = runner.invoke(cli, ["source", "clean", "-n", "nb_123", "--dry-run"])
            assert result.exit_code == 0
            assert "Dry run" in result.output
            assert "error_status" in result.output
            assert "gateway_title" in result.output
            mc.sources.delete.assert_not_called()

    def test_yes_skips_confirmation_and_deletes(self, runner, mock_auth):
        sources = [_src("src_err", status=SourceStatus.ERROR)]
        with self._patch_clean(sources) as mc:
            result = runner.invoke(cli, ["source", "clean", "-n", "nb_123", "-y"])
            assert result.exit_code == 0
            mc.sources.delete.assert_awaited_once_with("nb_123", "src_err")
            assert "Successfully cleaned" in result.output

    def test_user_declines_confirmation_aborts(self, runner, mock_auth):
        sources = [_src("src_err", status=SourceStatus.ERROR)]
        with self._patch_clean(sources) as mc:
            result = runner.invoke(cli, ["source", "clean", "-n", "nb_123"], input="n\n")
            assert result.exit_code == 0
            mc.sources.delete.assert_not_called()

    def test_partial_failure_reports_failing_ids(self, runner, mock_auth):
        sources = [
            _src("src_a", status=SourceStatus.ERROR),
            _src("src_b", status=SourceStatus.ERROR),
        ]

        async def fake_delete(nb, sid):
            if sid == "src_b":
                raise RuntimeError("boom")

        with self._patch_clean(sources) as mc:
            mc.sources.delete = AsyncMock(side_effect=fake_delete)
            result = runner.invoke(cli, ["source", "clean", "-n", "nb_123", "-y"])
            assert result.exit_code == 0
            assert "1 deletion(s) failed" in result.output
            assert "src_b" in result.output
            assert "boom" in result.output


class _CleanPatch:
    """Context manager that patches the NotebookLMClient for source-clean tests.

    Uses ``ExitStack`` so any patch that raises mid-setup correctly unwinds the
    patches that already entered, instead of leaking them into the next test.
    """

    def __init__(self, sources):
        self._sources = sources
        self._exit_stack = contextlib.ExitStack()

    def __enter__(self):
        mock_client_cls = self._exit_stack.enter_context(patch_client_for_module("source"))

        mock_client = create_mock_client()
        mock_client.sources.list = AsyncMock(return_value=self._sources)
        mock_client.sources.delete = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client
        self.sources = mock_client.sources

        fetch_mock = self._exit_stack.enter_context(
            patch("notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock)
        )
        fetch_mock.return_value = ("csrf", "session")
        return self

    def __exit__(self, *exc):
        return self._exit_stack.__exit__(*exc)


# =============================================================================
# JSON OUTPUT SMOKE TESTS (P2.T3 / I3)
# =============================================================================


class TestSourceJsonOutput:
    """Smoke coverage for ``--json`` on the eight source subcommands shipped in
    P2.T3: delete, rename, refresh, clean, get, delete-by-title, add-drive,
    stale.

    These tests exercise the JSON branch end-to-end through Click and assert:

    1. Stdout is parseable JSON (no Rich color codes leaking onto stdout).
    2. The shape exposes the fields automation needs (``source_id``,
       ``status``, etc.).
    3. ``source stale --json`` PRESERVES the inverted exit-code semantics
       documented in ``docs/cli-exit-codes.md`` (stale=0, fresh=1).
    """

    def _patch_fetch_tokens(self):
        return patch(
            "notebooklm.auth.fetch_tokens_with_domains",
            new_callable=AsyncMock,
            return_value=("csrf", "session"),
        )

    def test_source_get_json(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="My Source")]
            )
            mock_client.sources.get = AsyncMock(
                return_value=Source(
                    id="src_123",
                    title="My Source",
                    url="https://example.com",
                    created_at=datetime(2024, 1, 1, 12, 0),
                )
            )
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(cli, ["source", "get", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["found"] is True
            assert data["source"]["id"] == "src_123"
            assert data["source"]["title"] == "My Source"
            assert data["source"]["url"] == "https://example.com"
            assert data["source"]["created_at"] == "2024-01-01T12:00:00"

    def test_source_get_json_not_found_exits_1_with_typed_json(self, runner, mock_auth):
        # C1 (Phase 3) flipped this contract: ``get`` on not-found now exits 1
        # and emits the standard typed JSON error envelope (``{error, code,
        # message}``) instead of the previous exit-0 ``{found: false}``
        # placeholder. See ``docs/cli-exit-codes.md`` and the BREAKING entry
        # in ``CHANGELOG.md`` (Unreleased → Changed).
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_resolved", title="Existing")]
            )
            mock_client.sources.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli, ["source", "get", "src_resolved", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "NOT_FOUND"
            assert "Source not found" in data["message"]
            assert data["source_id"] == "src_resolved"

    def test_source_delete_json(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="My Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli, ["source", "delete", "src_123", "-n", "nb_123", "-y", "--json"]
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["action"] == "delete"
            assert data["source_id"] == "src_123"
            assert data["notebook_id"] == "nb_123"
            assert data["success"] is True
            assert data["status"] == "deleted"

    def test_source_delete_by_title_json(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_999", title="Doomed")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "delete-by-title",
                        "Doomed",
                        "-n",
                        "nb_123",
                        "-y",
                        "--json",
                    ],
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["action"] == "delete-by-title"
            assert data["source_id"] == "src_999"
            assert data["title"] == "Doomed"
            assert data["success"] is True
            assert data["status"] == "deleted"

    def test_source_rename_json(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=[Source(id="src_123", title="Old")])
            mock_client.sources.rename = AsyncMock(return_value=Source(id="src_123", title="New"))
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli,
                    ["source", "rename", "src_123", "New", "-n", "nb_123", "--json"],
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["action"] == "rename"
            assert data["source_id"] == "src_123"
            assert data["title"] == "New"
            assert data["status"] == "renamed"

    def test_source_refresh_json(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Original")]
            )
            mock_client.sources.refresh = AsyncMock(
                return_value=Source(id="src_123", title="Refreshed")
            )
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli, ["source", "refresh", "src_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["action"] == "refresh"
            assert data["source_id"] == "src_123"
            assert data["title"] == "Refreshed"
            assert data["status"] == "refreshed"

    def test_source_refresh_json_no_result(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Original")]
            )
            mock_client.sources.refresh = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli, ["source", "refresh", "src_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["action"] == "refresh"
            assert data["source_id"] == "src_123"
            assert data["status"] == "no_result"

    def test_source_add_drive_json(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_drive = AsyncMock(
                return_value=Source(id="src_drive", title="My Drive Doc")
            )
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add-drive",
                        "drive_file_xyz",
                        "My Drive Doc",
                        "--mime-type",
                        "pdf",
                        "-n",
                        "nb_123",
                        "--json",
                    ],
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["action"] == "add-drive"
            assert data["source"]["id"] == "src_drive"
            assert data["source"]["title"] == "My Drive Doc"
            assert data["source"]["drive_file_id"] == "drive_file_xyz"
            assert data["source"]["mime_type"] == "pdf"
            assert data["notebook_id"] == "nb_123"

    def test_source_stale_json_is_stale_exits_zero(self, runner, mock_auth):
        """``source stale --json`` PRESERVES the inverted exit-code semantics:
        exit 0 when stale (predicate true), so the shell idiom
        ``if notebooklm source stale ID; then refresh; fi`` still works in
        JSON mode. See docs/cli-exit-codes.md.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=[Source(id="src_123", title="Stale")])
            mock_client.sources.check_freshness = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli, ["source", "stale", "src_123", "-n", "nb_123", "--json"]
                )

            # Inverted exit code preserved in JSON mode.
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["stale"] is True
            assert data["fresh"] is False
            assert data["source_id"] == "src_123"

    def test_source_stale_json_is_fresh_exits_one(self, runner, mock_auth):
        """Inverted exit-code semantics, fresh branch: exit 1 (predicate false)."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=[Source(id="src_123", title="Fresh")])
            mock_client.sources.check_freshness = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli, ["source", "stale", "src_123", "-n", "nb_123", "--json"]
                )

            # Inverted exit code preserved in JSON mode.
            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["stale"] is False
            assert data["fresh"] is True
            assert data["source_id"] == "src_123"

    def test_source_clean_json_already_clean(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[_src("src_1", title="Page", url="https://ex.com/a")]
            )
            mock_client.sources.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(cli, ["source", "clean", "-n", "nb_123", "-y", "--json"])

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["action"] == "clean"
            assert data["status"] == "already_clean"
            assert data["candidates"] == []
            assert data["deleted_count"] == 0

    def test_source_clean_json_dry_run(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[_src("src_err", title="oops", status=SourceStatus.ERROR)]
            )
            mock_client.sources.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(
                    cli, ["source", "clean", "-n", "nb_123", "--dry-run", "--json"]
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "dry_run"
            assert data["candidate_count"] == 1
            assert data["candidates"][0]["id"] == "src_err"
            assert data["candidates"][0]["reason"] == "error_status"
            assert data["deleted_count"] == 0
            mock_client.sources.delete.assert_not_called()

    def test_source_clean_json_completed(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[_src("src_err", title="oops", status=SourceStatus.ERROR)]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with self._patch_fetch_tokens():
                result = runner.invoke(cli, ["source", "clean", "-n", "nb_123", "-y", "--json"])

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "completed"
            assert data["deleted_count"] == 1
            assert data["failure_count"] == 0
            assert data["failures"] == []
            mock_client.sources.delete.assert_awaited_once_with("nb_123", "src_err")


# =============================================================================
# P7.T2 / M3 — Stdin (`-`) convention for ``source add``
# =============================================================================
#
# Unix tradition: ``source add -`` reads source content from stdin and
# ingests it as inline text. This is the natural pipeline for ``cat
# content.txt | notebooklm source add -`` or ``some-cmd | notebooklm
# source add -``. The literal ``-`` is intercepted BEFORE auto-detection
# so it never accidentally falls into the path-shaped warning branch.


class TestSourceAddStdinDash:
    """``notebooklm source add -`` reads inline text from stdin."""

    def test_source_add_dash_reads_stdin(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Pasted Text")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", "-", "-n", "nb_123"],
                    input="content from stdin\n",
                )

            assert result.exit_code == 0, result.output
            mock_client.sources.add_text.assert_awaited_once()
            call = mock_client.sources.add_text.call_args
            # signature: add_text(notebook_id, title, content)
            assert call.args[0] == "nb_123"
            assert call.args[2] == "content from stdin"

    def test_source_add_dash_with_title(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="My Title")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", "-", "--title", "My Title", "-n", "nb_123"],
                    input="hello world\n",
                )

            assert result.exit_code == 0, result.output
            call = mock_client.sources.add_text.call_args
            assert call.args[1] == "My Title"
            assert call.args[2] == "hello world"

    def test_source_add_literal_dash_path_unchanged(self, runner, mock_auth):
        """Regression: a normal text argument is not treated as stdin."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Pasted Text")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", "literal text", "-n", "nb_123"],
                    input="ignored stdin\n",
                )

            assert result.exit_code == 0, result.output
            call = mock_client.sources.add_text.call_args
            assert call.args[2] == "literal text"
