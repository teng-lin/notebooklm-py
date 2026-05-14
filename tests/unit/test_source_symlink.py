"""Symlink hardening for `notebooklm source add` file uploads (PR-T5.C).

The CLI must reject symlinked paths unless the user explicitly opts in via
``--follow-symlinks``. This guards against the footgun where a workspace
file like ``~/Downloads/foo.pdf`` is silently a symlink into ``/etc/`` or
similar — uploading the symlink target would leak sensitive content.

Tests cover both the **auto-detect** path (no ``--type``) and the
**explicit** ``--type file`` path — both must enforce the same gate.
The gate also rejects:

- Leaf symlinks (default).
- Parent-directory symlinks (``dir_link/file.pdf``).
- Broken symlinks (``exists()`` returns False but ``is_symlink()`` is True —
  without the special-case we'd silently treat the path string as text
  content).

When ``--follow-symlinks`` is passed, the resolved path is forwarded to
``add_file()`` so the upload opens exactly the file we validated (closes
the TOCTOU window).
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

# ``cli/__init__.py`` re-exports the ``source`` click Group under that
# name, which shadows the underlying module. ``import notebooklm.cli.source``
# *does* bind to the module via the dotted-path machinery, but
# ``from notebooklm.cli import source`` would bind to the Group. We use
# ``importlib.import_module`` here as the explicit, unambiguous form so
# ``patch.object(_source_module, "NotebookLMClient")`` targets the module
# attribute the CLI handler actually reads.
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

    # Default upload stubs; individual tests override as needed.
    client.sources.add_file = AsyncMock(return_value=Source(id="src_default", title="x"))
    client.sources.add_text = AsyncMock(return_value=Source(id="src_text", title="x"))
    return client


def _invoke(runner: CliRunner, mock_client: MagicMock, argv: list[str]):
    """Run the CLI with NotebookLMClient patched to ``mock_client``."""
    with patch.object(_source_module, "NotebookLMClient", return_value=mock_client):
        return runner.invoke(cli, argv, catch_exceptions=False)


class TestAutoDetectFilePath:
    """Auto-detect (no ``--type``) branch — the original PR-T5.C surface."""

    def test_plain_regular_file_uploads(self, runner, mock_auth, tmp_path: Path) -> None:
        """A non-symlinked file passes the gate and is uploaded.

        Also locks down the *exact* path forwarded to ``add_file()``: the
        resolved Path, not the raw argument. This is what closes the
        TOCTOU window (issue called out by coderabbit on the initial
        review pass).
        """
        real_file = tmp_path / "doc.pdf"
        real_file.write_bytes(b"fake pdf content")
        assert not real_file.is_symlink()

        mock_client = _make_client()
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="src_real", title="doc.pdf")
        )

        result = _invoke(runner, mock_client, ["source", "add", str(real_file), "-n", "nb_123"])

        assert result.exit_code == 0, result.output
        mock_client.sources.add_file.assert_awaited_once()
        called_path = mock_client.sources.add_file.await_args.args[1]
        # The CLI passes the resolved path so add_file opens the same
        # file we validated. For a non-symlink in a real tmp_path, the
        # resolved path equals the raw input after expanduser().
        assert called_path == str(real_file.expanduser().resolve())

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

        result = _invoke(runner, mock_client, ["source", "add", str(link), "-n", "nb_123"])

        assert result.exit_code == 1
        assert "symlink" in result.output.lower()
        assert "--follow-symlinks" in result.output
        assert str(link) in result.output
        mock_client.sources.add_file.assert_not_awaited()

    def test_symlink_with_flag_is_followed_and_uploaded(
        self, runner, mock_auth, tmp_path: Path
    ) -> None:
        """``--follow-symlinks`` resolves the symlink and uploads the target.

        Verifies the *resolved* path is what's handed to ``add_file()``
        (not the symlink path), closing the TOCTOU window.
        """
        target = tmp_path / "target.pdf"
        target.write_bytes(b"real content")
        link = tmp_path / "link.pdf"
        os.symlink(target, link)
        assert link.is_symlink()

        mock_client = _make_client()
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="src_resolved", title="target.pdf")
        )

        result = _invoke(
            runner,
            mock_client,
            ["source", "add", str(link), "-n", "nb_123", "--follow-symlinks"],
        )

        assert result.exit_code == 0, result.output
        mock_client.sources.add_file.assert_awaited_once()
        called_path = mock_client.sources.add_file.await_args.args[1]
        # The upload must hit the *target*, not the link, after opt-in.
        assert called_path == str(target.expanduser().resolve())

    def test_broken_symlink_without_flag_is_rejected(
        self, runner, mock_auth, tmp_path: Path
    ) -> None:
        """Broken symlink without flag is rejected by the symlink gate.

        ``exists()`` follows symlinks and returns False for dangling
        links — without our special-case OR, the CLI would silently
        treat the broken-link *path string* as text content. We assert
        the symlink-rejection error fires.
        """
        missing = tmp_path / "does_not_exist.pdf"
        link = tmp_path / "broken_link.pdf"
        os.symlink(missing, link)
        assert link.is_symlink()
        assert not link.exists()  # broken — exists() follows the link

        mock_client = _make_client()
        mock_client.sources.add_text = AsyncMock(
            return_value=Source(id="should_not_be_used_text", title="broken_link.pdf")
        )

        result = _invoke(runner, mock_client, ["source", "add", str(link), "-n", "nb_123"])

        assert result.exit_code == 1
        assert "symlink" in result.output.lower()
        assert "--follow-symlinks" in result.output
        # Critical: no upload of any kind. We must not silently turn the
        # link string into a text source.
        mock_client.sources.add_file.assert_not_awaited()
        mock_client.sources.add_text.assert_not_awaited()

    def test_broken_symlink_with_flag_falls_through_to_not_a_file(
        self, runner, mock_auth, tmp_path: Path
    ) -> None:
        """Broken symlink + ``--follow-symlinks`` hits the regular-file gate.

        After the symlink gate is bypassed, ``resolve()`` produces a path
        whose target doesn't exist, so ``is_file()`` is False and we
        surface "Not a regular file" instead of attempting an upload.
        """
        missing = tmp_path / "does_not_exist.pdf"
        link = tmp_path / "broken_link.pdf"
        os.symlink(missing, link)
        assert link.is_symlink()
        assert not link.exists()

        mock_client = _make_client()
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="should_not_be_used", title="should_not_be_used")
        )

        result = _invoke(
            runner,
            mock_client,
            ["source", "add", str(link), "-n", "nb_123", "--follow-symlinks"],
        )

        assert result.exit_code == 1
        assert "not a regular file" in result.output.lower()
        mock_client.sources.add_file.assert_not_awaited()

    def test_parent_directory_symlink_is_rejected(self, runner, mock_auth, tmp_path: Path) -> None:
        """A symlinked *parent* directory triggers the gate.

        ``raw.is_symlink()`` only inspects the leaf path, so
        ``dir_link/file.pdf`` would slip through a leaf-only check. The
        helper walks parents — assert here that the gate fires.
        """
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()
        real_file = real_dir / "doc.pdf"
        real_file.write_bytes(b"content")

        dir_link = tmp_path / "dir_link"
        os.symlink(real_dir, dir_link)
        path_through_link = dir_link / "doc.pdf"
        assert path_through_link.exists()  # the leaf itself is a real file
        assert not path_through_link.is_symlink()  # leaf alone looks clean
        assert dir_link.is_symlink()  # but the parent is a link

        mock_client = _make_client()

        result = _invoke(
            runner, mock_client, ["source", "add", str(path_through_link), "-n", "nb_123"]
        )

        assert result.exit_code == 1
        assert "symlink" in result.output.lower()
        mock_client.sources.add_file.assert_not_awaited()


class TestExplicitTypeFile:
    """``--type file`` path — the security gap closed in the review pass.

    Without these tests, ``source add link.pdf --type file`` would
    silently bypass the symlink gate and leak the link target. Locked
    down by parametrized variants that mirror the auto-detect tests.
    """

    def test_explicit_type_file_with_plain_file_uploads(
        self, runner, mock_auth, tmp_path: Path
    ) -> None:
        """Sanity check: ``--type file`` on a normal file still works."""
        real_file = tmp_path / "doc.pdf"
        real_file.write_bytes(b"fake pdf content")

        mock_client = _make_client()
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="src_real", title="doc.pdf")
        )

        result = _invoke(
            runner,
            mock_client,
            ["source", "add", str(real_file), "--type", "file", "-n", "nb_123"],
        )

        assert result.exit_code == 0, result.output
        mock_client.sources.add_file.assert_awaited_once()
        called_path = mock_client.sources.add_file.await_args.args[1]
        assert called_path == str(real_file.expanduser().resolve())

    def test_explicit_type_file_with_symlink_no_flag_is_rejected(
        self, runner, mock_auth, tmp_path: Path
    ) -> None:
        """Critical: explicit ``--type file`` MUST honor the symlink gate.

        This is the path most likely used by automation scripts (which
        often hardcode ``--type``), so it's the higher-risk surface.
        Pre-fix, this case bypassed the guard completely.
        """
        target = tmp_path / "target.pdf"
        target.write_bytes(b"sensitive content")
        link = tmp_path / "link.pdf"
        os.symlink(target, link)

        mock_client = _make_client()
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="should_not_be_used", title="should_not_be_used")
        )

        result = _invoke(
            runner,
            mock_client,
            ["source", "add", str(link), "--type", "file", "-n", "nb_123"],
        )

        assert result.exit_code == 1
        assert "symlink" in result.output.lower()
        assert "--follow-symlinks" in result.output
        mock_client.sources.add_file.assert_not_awaited()

    def test_explicit_type_file_with_symlink_and_flag_is_followed(
        self, runner, mock_auth, tmp_path: Path
    ) -> None:
        """``--type file --follow-symlinks`` resolves and uploads."""
        target = tmp_path / "target.pdf"
        target.write_bytes(b"real content")
        link = tmp_path / "link.pdf"
        os.symlink(target, link)

        mock_client = _make_client()
        mock_client.sources.add_file = AsyncMock(
            return_value=Source(id="src_resolved", title="target.pdf")
        )

        result = _invoke(
            runner,
            mock_client,
            [
                "source",
                "add",
                str(link),
                "--type",
                "file",
                "-n",
                "nb_123",
                "--follow-symlinks",
            ],
        )

        assert result.exit_code == 0, result.output
        mock_client.sources.add_file.assert_awaited_once()
        called_path = mock_client.sources.add_file.await_args.args[1]
        assert called_path == str(target.expanduser().resolve())
