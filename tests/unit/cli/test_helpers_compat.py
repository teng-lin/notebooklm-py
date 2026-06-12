"""Patch-surface compatibility tests for ``notebooklm.cli.helpers``.

After P2.T1 consolidates the canonical resolver implementations into
``notebooklm.cli.resolve``, the ``notebooklm.cli.helpers`` module continues
to expose ``require_notebook``, ``validate_id``, ``_resolve_partial_id``,
``resolve_notebook_id``, etc. as a **compatibility facade**. These names are
patch targets in many existing tests; the facade must survive renames or
shape changes in the canonical implementation.

This test file is the explicit pin for that contract. Critical guarantees:

1. Patching ``helpers.require_notebook`` intercepts calls made through the
   helpers module namespace.
2. Patching ``helpers.get_context_path`` still affects
   ``helpers.require_notebook``'s context-file-fallback behavior. ~20
   existing tests in ``test_helpers.py`` rely on this.
3. Patching ``helpers.console`` still affects
   ``helpers.require_notebook``'s no-notebook diagnostic. Several existing
   tests rely on this.
4. ``helpers.validate_id`` and ``helpers._resolve_partial_id`` remain
   importable and call-compatible with the canonical resolver.

If this file ever fails, the patch surface has regressed and downstream
test files will start breaking - fix the facade, not this test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest

from notebooklm.cli import helpers

# ----------------------------------------------------------------------------
# 1. Patching helpers.require_notebook intercepts CLI calls
# ----------------------------------------------------------------------------


class TestMonkeypatchOnHelpersRequireNotebook:
    """A patch of ``helpers.require_notebook`` must intercept
    ``helpers.require_notebook(...)`` calls.

    This is the explicit acceptance criterion from P2.T1.
    """

    def test_monkeypatch_setattr_intercepts_direct_call(self, monkeypatch):
        sentinel = "INTERCEPTED_NOTEBOOK_ID"

        def fake_require_notebook(notebook_id):
            return sentinel

        monkeypatch.setattr(helpers, "require_notebook", fake_require_notebook)

        result = helpers.require_notebook("ignored")
        assert result == sentinel

    def test_monkeypatch_undone_after_test(self):
        """Sanity: after monkeypatch undoes itself, the real function returns.

        This guards against a bad facade implementation that would cache the
        patched function in a closure and leak between tests.
        """
        # The previous test patched the symbol. After fixture teardown,
        # calling with a real id should still go through the normal path.
        # Without a context file, env var, or arg, it raises SystemExit.
        with pytest.raises(SystemExit):
            helpers.require_notebook(None)


# ----------------------------------------------------------------------------
# 2. patching helpers.get_context_path still steers helpers.require_notebook
# ----------------------------------------------------------------------------


class TestHelpersGetContextPathPatchSurface:
    """Patching ``helpers.get_context_path`` must affect
    ``helpers.require_notebook`` so the ~20 existing tests in
    ``test_helpers.py`` that do this keep passing.
    """

    def test_context_file_path_patch_steers_resolution(self, tmp_path):
        """An empty/non-existent context path leads to SystemExit."""
        with (
            patch.object(
                helpers,
                "get_context_path",
                return_value=tmp_path / "nonexistent.json",
            ),
            pytest.raises(SystemExit),
        ):
            helpers.require_notebook(None)

    def test_context_file_path_patch_returns_context_value(self, tmp_path):
        """A patched ``get_context_path`` pointing at a real file lets the
        context-fallback rung return its notebook id."""
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "nb_from_context"}')
        with patch.object(helpers, "get_context_path", return_value=context_file):
            assert helpers.require_notebook(None) == "nb_from_context"


# ----------------------------------------------------------------------------
# 3. patching helpers.console still steers the error-path output
# ----------------------------------------------------------------------------


class TestHelpersConsolePatchSurface:
    def test_console_patch_captures_no_notebook_diagnostic(self, tmp_path):
        """Patching ``helpers.console`` captures the no-notebook error
        message that ``require_notebook`` prints before raising
        ``SystemExit``."""
        with (
            patch.object(
                helpers,
                "get_context_path",
                return_value=tmp_path / "nonexistent.json",
            ),
            patch.object(helpers, "console") as mock_console,
        ):
            with pytest.raises(SystemExit):
                helpers.require_notebook(None)

            mock_console.print.assert_called_once()
            printed = mock_console.print.call_args[0][0]
            # User-facing flag is named.
            assert "-n/--notebook" in printed


# ----------------------------------------------------------------------------
# 4. validate_id + _resolve_partial_id remain importable from helpers
# ----------------------------------------------------------------------------


class TestResolverFacadeReExports:
    def test_resolver_names_are_canonical_aliases(self):
        from notebooklm.cli import resolve

        assert helpers.require_notebook is resolve.require_notebook
        assert helpers.validate_id is resolve.validate_id
        assert helpers._resolve_partial_id is resolve._resolve_partial_id
        assert helpers.resolve_notebook_id is resolve.resolve_notebook_id
        assert helpers.resolve_source_id is resolve.resolve_source_id
        assert helpers.resolve_artifact_id is resolve.resolve_artifact_id

    def test_validate_id_importable_from_helpers(self):
        from notebooklm.cli.helpers import validate_id

        # Trivial smoke check that the facade name is callable.
        assert validate_id("  abc  ") == "abc"

    def test_validate_id_facade_matches_canonical(self):
        from notebooklm.cli import resolve

        # Same function or same behavior across multiple inputs.
        for case in ("a", "  spaced  ", "abc12345-6789-4abc-def0-1234567890ab"):
            assert helpers.validate_id(case) == resolve.validate_id(case)

    def test_validate_id_facade_raises_same_exception(self):
        with pytest.raises(click.ClickException):
            helpers.validate_id("   ")

    @pytest.mark.asyncio
    async def test_resolve_partial_id_importable_from_helpers(self):
        from notebooklm.cli.helpers import _resolve_partial_id

        class Item:
            def __init__(self, id_, title):
                self.id = id_
                self.title = title

        async def list_fn():
            return [Item("aaa111", "First"), Item("bbb222", "Second")]

        result = await _resolve_partial_id("aaa", list_fn, "thing", "thing list")
        assert result == "aaa111"

    @pytest.mark.asyncio
    async def test_resolve_notebook_id_importable_from_helpers(self):
        from notebooklm.cli.helpers import resolve_notebook_id

        client = MagicMock()

        class Notebook:
            def __init__(self, id_, title):
                self.id = id_
                self.title = title

        client.notebooks = MagicMock()
        client.notebooks.list = AsyncMock(
            return_value=[Notebook("nb_111", "First"), Notebook("nb_222", "Second")]
        )

        result = await resolve_notebook_id(client, "nb_1")
        assert result == "nb_111"


# ----------------------------------------------------------------------------
# 5. helpers.* names remain present in cli/__init__.py public surface
# ----------------------------------------------------------------------------


class TestPublicSurfaceRemainsStable:
    """The ``cli/__init__.py`` public re-exports must not lose
    ``require_notebook``, ``resolve_notebook_id``, etc."""

    def test_public_names_present(self):
        from notebooklm import cli

        for name in (
            "require_notebook",
            "resolve_notebook_id",
            "resolve_source_id",
            "resolve_artifact_id",
        ):
            assert hasattr(cli, name), f"cli public surface lost {name!r}"
