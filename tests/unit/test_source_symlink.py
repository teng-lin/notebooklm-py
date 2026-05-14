"""Symlink hardening for `notebooklm source add` file uploads (PR-T5.C).

The CLI must reject symlinked paths unless the user explicitly opts in via
``--follow-symlinks``. This guards against the footgun where a workspace
file like ``~/Downloads/foo.pdf`` is silently a symlink into ``/etc/`` or
similar — uploading the symlink target would leak sensitive content.

Tests:
- Plain regular file: uploaded normally (``sources.add_file`` is called).
- Symlink without flag: ``ClickException`` + exit code 1, **no upload**.
- Symlink with ``--follow-symlinks``: target resolved and uploaded.
- Broken symlink with flag: no upload attempted (broken symlinks report
  ``exists() == False`` and fall through to the text branch — the point
  is that the symlink target is never read).
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Source

# Resolve the actual source module (cli/__init__ shadows `source` with the
# click group of the same name; we need the module to patch NotebookLMClient).
_source_module = importlib.import_module("notebooklm.cli.source")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_auth():
    """Stub auth loading + token fetch so CLI paths run offline."""
    with (
        patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load,
        patch("notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock) as mock_fetch,
    ):
        mock_load.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        mock_fetch.return_value = ("csrf", "session")
        yield mock_load


def _make_client() -> MagicMock:
    """Build a mock NotebookLMClient suitable for ``source add`` paths.

    Pre-seeds notebook list so ``-n nb_123`` resolves cleanly.
    """
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    for ns in ("notebooks", "sources", "artifacts", "chat", "research", "notes", "sharing"):
        setattr(client, ns, MagicMock())

    nb_obj = MagicMock()
    nb_obj.id = "nb_123"
    nb_obj.title = "Test Notebook"
    client.notebooks.list = AsyncMock(return_value=[nb_obj])
    client.notebooks.get = AsyncMock(return_value=nb_obj)

    # Default upload stubs; tests override as needed.
    client.sources.add_file = AsyncMock(return_value=Source(id="src_default", title="x"))
    client.sources.add_text = AsyncMock(return_value=Source(id="src_text", title="x"))
    return client


class TestSourceAddSymlinkHardening:
    """Symlink rejection logic in the auto-detect file path."""

    def test_plain_regular_file_uploads(self, runner, mock_auth, tmp_path: Path) -> None:
        """A non-symlinked file passes the symlink gate and is uploaded."""
        real_file = tmp_path / "doc.pdf"
        real_file.write_bytes(b"fake pdf content")
        assert not real_file.is_symlink()

        mock_client = _make_client()
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="src_real", title="doc.pdf")
        )
        with patch.object(_source_module, "NotebookLMClient", return_value=mock_client):
            result = runner.invoke(
                cli,
                ["source", "add", str(real_file), "-n", "nb_123"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        mock_client.sources.add_file.assert_awaited_once()
        # The path passed to add_file is the raw content string; the
        # CLI's resolution stays local to the symlink/is_file gate and the
        # original argument is forwarded to ``add_file``.
        called_path = mock_client.sources.add_file.await_args.args[1]
        assert called_path == str(real_file)

    def test_symlink_without_flag_is_rejected(self, runner, mock_auth, tmp_path: Path) -> None:
        """Symlink without ``--follow-symlinks`` exits 1 with a clear error."""
        target = tmp_path / "target.pdf"
        target.write_bytes(b"sensitive content")
        link = tmp_path / "link.pdf"
        os.symlink(target, link)
        assert link.is_symlink()

        mock_client = _make_client()
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="should_not_be_used", title="should_not_be_used")
        )
        with patch.object(_source_module, "NotebookLMClient", return_value=mock_client):
            result = runner.invoke(
                cli,
                ["source", "add", str(link), "-n", "nb_123"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        # The error message names the offending path AND tells the user
        # exactly which flag opts in.
        assert "symlink" in result.output.lower()
        assert "--follow-symlinks" in result.output
        assert str(link) in result.output
        # Critical: no upload happened.
        mock_client.sources.add_file.assert_not_awaited()

    def test_symlink_with_flag_is_followed_and_uploaded(
        self, runner, mock_auth, tmp_path: Path
    ) -> None:
        """Symlink + ``--follow-symlinks`` resolves and uploads the target."""
        target = tmp_path / "target.pdf"
        target.write_bytes(b"real content")
        link = tmp_path / "link.pdf"
        os.symlink(target, link)
        assert link.is_symlink()

        mock_client = _make_client()
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="src_resolved", title="target.pdf")
        )
        with patch.object(_source_module, "NotebookLMClient", return_value=mock_client):
            result = runner.invoke(
                cli,
                ["source", "add", str(link), "-n", "nb_123", "--follow-symlinks"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        mock_client.sources.add_file.assert_awaited_once()

    def test_broken_symlink_with_flag_does_not_upload(
        self, runner, mock_auth, tmp_path: Path
    ) -> None:
        """Broken symlink + flag must not invoke the file-upload path.

        ``Path.exists()`` follows symlinks and returns ``False`` for a broken
        link, so the auto-detect ``elif Path(content).exists():`` branch is
        skipped entirely and the input falls into the text branch. The
        contract this test locks: *under no circumstances* does the CLI
        attempt to read or upload the (missing) symlink target as a file.
        """
        missing = tmp_path / "does_not_exist.pdf"
        link = tmp_path / "broken_link.pdf"
        os.symlink(missing, link)
        assert link.is_symlink()
        assert not link.exists()  # broken — exists() follows the link

        mock_client = _make_client()
        # If the CLI mistakenly invokes add_file with a broken link, the
        # assertion below catches it.
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="should_not_be_used", title="should_not_be_used")
        )
        mock_client.sources.add_text = AsyncMock(
            return_value=Source(id="src_text", title="broken_link.pdf")
        )
        with patch.object(_source_module, "NotebookLMClient", return_value=mock_client):
            result = runner.invoke(
                cli,
                [
                    "source",
                    "add",
                    str(link),
                    "-n",
                    "nb_123",
                    "--follow-symlinks",
                ],
                catch_exceptions=False,
            )

        # Command must not blow up with a traceback, and must NOT have
        # uploaded the (non-existent) symlink target as a file.
        assert result.exit_code == 0, result.output
        mock_client.sources.add_file.assert_not_awaited()
