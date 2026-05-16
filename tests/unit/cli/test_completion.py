"""Tests for shell completion (P7.T1 / M1).

Two surfaces are exercised here:

1. ``notebooklm completion <shell>`` — the new top-level command that emits
   the Click-generated completion script for ``bash`` / ``zsh`` / ``fish``.
   The smoke tests assert the script starts with the canonical Click marker
   for each shell (rather than diffing the entire body) so a Click-version
   bump that tweaks the body still passes as long as the protocol entry-point
   name is preserved. Invalid shell values must exit ``2`` with a UsageError
   so the user sees a list of accepted choices.

2. ID-aware ``shell_complete`` callbacks attached to ``-n/--notebook``,
   ``-s/--source``, and ``-a/--artifact`` in ``cli/options.py``. The
   callbacks must (a) return ``CompletionItem`` rows filtered by the
   ``incomplete`` prefix when auth + listing succeeds, and (b) degrade
   silently to ``[]`` on any exception (auth missing, network error, etc.)
   so a fresh shell never gets stack traces printed mid-completion.

The notebook-resolution helper used by ``-s`` / ``-a`` is also pinned so the
documented precedence (flag > env > active context) cannot regress silently.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# `notebooklm completion <shell>`
# ---------------------------------------------------------------------------


class TestCompletionCommand:
    """``notebooklm completion <shell>`` emits the Click completion script."""

    def test_bash_outputs_completion_function(self, runner):
        """Bash script must define ``_notebooklm_completion`` so the
        ``complete -F`` registration in the install snippet can reference it.
        """
        result = runner.invoke(cli, ["completion", "bash"])
        assert result.exit_code == 0, result.output
        assert "_notebooklm_completion" in result.output
        # Click's bash template wires the env-var protocol; pin the exact env
        # var so a refactor that switches the prefix is caught.
        assert "_NOTEBOOKLM_COMPLETE=bash_complete" in result.output

    def test_zsh_outputs_compdef_directive(self, runner):
        """Zsh scripts begin with ``#compdef notebooklm`` so the file can be
        dropped onto ``$fpath`` without further glue.
        """
        result = runner.invoke(cli, ["completion", "zsh"])
        assert result.exit_code == 0, result.output
        assert "#compdef notebooklm" in result.output
        assert "_NOTEBOOKLM_COMPLETE=zsh_complete" in result.output

    def test_fish_outputs_completion_function(self, runner):
        """Fish scripts define a function and end with a ``complete -c``
        registration; both halves must be present.
        """
        result = runner.invoke(cli, ["completion", "fish"])
        assert result.exit_code == 0, result.output
        assert "_notebooklm_completion" in result.output
        assert "_NOTEBOOKLM_COMPLETE=fish_complete" in result.output

    def test_invalid_shell_exits_with_usage_error(self, runner):
        """Click's ``Choice`` parameter type rejects unknown shells with
        exit code 2 (the documented CLI convention for system/usage errors).
        The error message must list the accepted values so the user can
        self-correct without reading docs.
        """
        result = runner.invoke(cli, ["completion", "powershell"])
        assert result.exit_code == 2
        # Click renders the available choices in the error message.
        assert "bash" in result.output
        assert "zsh" in result.output
        assert "fish" in result.output

    def test_no_shell_argument_errors(self, runner):
        """Omitting the SHELL argument must error rather than default to a
        platform guess — emitting the wrong shell's script silently into a
        dotfile would be a footgun.
        """
        result = runner.invoke(cli, ["completion"])
        assert result.exit_code == 2
        assert "shell" in result.output.lower() or "missing" in result.output.lower()

    def test_completion_visible_in_help(self, runner):
        """The completion command must show up in ``notebooklm --help`` so
        users can discover it without grepping the changelog.
        """
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "completion" in result.output

    def test_completion_help_documents_install_one_liners(self, runner):
        """The per-command ``--help`` should include at least one install
        snippet so users see the canonical usage without round-tripping
        through the docs site.
        """
        result = runner.invoke(cli, ["completion", "--help"])
        assert result.exit_code == 0, result.output
        # Must show the canonical zsh install path (the example pinned in
        # the spec) so regressions in the docstring rewrite
        # don't drop the most-asked-for snippet.
        assert "zsh" in result.output.lower()
        assert "_notebooklm" in result.output or ".bashrc" in result.output


# ---------------------------------------------------------------------------
# `_complete_notebooks` callback
# ---------------------------------------------------------------------------


class _Stub:
    """Tiny stand-in for notebook / source / artifact rows used by completers.

    The constructor takes ``stub_id`` (rather than the more natural ``id``)
    to avoid shadowing the Python builtin ``id`` — flagged by Ruff A002.
    The instance attribute is still ``self.id`` so the production code under
    test, which does ``getattr(row, "id", ...)``, sees the same shape it
    would in real ``Notebook`` / ``Source`` / ``Artifact`` dataclasses.
    """

    def __init__(self, stub_id: str, title: str = ""):
        self.id = stub_id
        self.title = title


class TestCompleteNotebooks:
    """The ``-n/--notebook`` ``shell_complete`` callback is best-effort."""

    def test_returns_filtered_completion_items(self):
        """When auth + listing succeed, the callback returns
        ``CompletionItem`` rows whose ``id`` starts with ``incomplete`` and
        whose ``help`` text shows the notebook title.
        """
        from notebooklm.cli import options

        async def fake_list():
            return [
                _Stub("nb_abc123", "Project Alpha"),
                _Stub("nb_abc456", "Project Beta"),
                _Stub("nb_xyz789", "Other Notebook"),
            ]

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None
        fake_client.notebooks.list = AsyncMock(side_effect=fake_list)

        with (
            patch("notebooklm.cli.helpers.get_auth_tokens", return_value=object()),
            patch("notebooklm.client.NotebookLMClient", return_value=fake_client),
        ):
            items = options._complete_notebooks(ctx=None, param=None, incomplete="nb_abc")

        ids = [it.value for it in items]
        assert ids == ["nb_abc123", "nb_abc456"]
        helps = [it.help for it in items]
        assert helps == ["Project Alpha", "Project Beta"]

    def test_returns_empty_on_auth_failure(self):
        """A missing-auth bootstrap must NOT raise out of the completer —
        the user's shell would render the traceback as garbage. Return ``[]``
        so the user just gets no suggestions, matching the no-completion
        contract documented on the option.
        """
        from notebooklm.cli import options

        with patch(
            "notebooklm.cli.helpers.get_auth_tokens",
            side_effect=FileNotFoundError("no auth"),
        ):
            items = options._complete_notebooks(ctx=None, param=None, incomplete="nb_")

        assert items == []

    def test_returns_empty_on_network_error(self):
        """``NetworkError`` (any exception) inside the listing call must also
        degrade silently to no suggestions.
        """
        from notebooklm.cli import options

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None
        fake_client.notebooks.list = AsyncMock(side_effect=RuntimeError("offline"))

        with (
            patch("notebooklm.cli.helpers.get_auth_tokens", return_value=object()),
            patch("notebooklm.client.NotebookLMClient", return_value=fake_client),
        ):
            items = options._complete_notebooks(ctx=None, param=None, incomplete="nb_")

        assert items == []

    def test_caps_results_at_50(self):
        """The completer must cap the result list at 50 to keep the shell
        snappy on profiles with hundreds of notebooks.
        """
        from notebooklm.cli import options

        async def fake_list():
            return [_Stub(f"nb_{i:03d}", f"Notebook {i}") for i in range(100)]

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None
        fake_client.notebooks.list = AsyncMock(side_effect=fake_list)

        with (
            patch("notebooklm.cli.helpers.get_auth_tokens", return_value=object()),
            patch("notebooklm.client.NotebookLMClient", return_value=fake_client),
        ):
            items = options._complete_notebooks(ctx=None, param=None, incomplete="nb_")

        assert len(items) == 50


# ---------------------------------------------------------------------------
# `_resolve_notebook_for_completion` precedence
# ---------------------------------------------------------------------------


class TestResolveNotebookForCompletion:
    """Precedence: flag > env > persisted active context.

    The completer for ``-s`` / ``-a`` must walk the same order as
    ``helpers.require_notebook`` so suggestions match what the command body
    will actually target.
    """

    def test_flag_wins_over_env_and_context(self):
        """A ``notebook_id`` already parsed into a Click context wins over
        both ``NOTEBOOKLM_NOTEBOOK`` and the persisted active notebook.
        """
        from notebooklm.cli import options

        ctx = type("Ctx", (), {"params": {"notebook_id": "nb_from_flag"}, "parent": None})()
        with (
            patch.dict(os.environ, {"NOTEBOOKLM_NOTEBOOK": "nb_from_env"}),
            patch.object(options, "_complete_notebooks"),
            # _resolve_notebook_for_completion imports get_current_notebook lazily
            patch(
                "notebooklm.cli.helpers.get_current_notebook",
                return_value="nb_from_context",
            ),
        ):
            nid = options._resolve_notebook_for_completion(ctx)
        assert nid == "nb_from_flag"

    def test_env_wins_over_context(self):
        """When no flag is parsed, ``NOTEBOOKLM_NOTEBOOK`` is honored next."""
        from notebooklm.cli import options

        ctx = type("Ctx", (), {"params": {}, "parent": None})()
        with (
            patch.dict(os.environ, {"NOTEBOOKLM_NOTEBOOK": "nb_from_env"}),
            patch(
                "notebooklm.cli.helpers.get_current_notebook",
                return_value="nb_from_context",
            ),
        ):
            nid = options._resolve_notebook_for_completion(ctx)
        assert nid == "nb_from_env"

    def test_falls_back_to_persisted_context(self):
        """No flag and no env → reach for the persisted ``notebooklm use``
        context.
        """
        from notebooklm.cli import options

        ctx = type("Ctx", (), {"params": {}, "parent": None})()
        # Ensure env var unset
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTEBOOKLM_NOTEBOOK", None)
            with patch(
                "notebooklm.cli.helpers.get_current_notebook",
                return_value="nb_from_context",
            ):
                nid = options._resolve_notebook_for_completion(ctx)
        assert nid == "nb_from_context"

    def test_returns_none_when_nothing_set(self):
        """No flag, no env, no context → ``None`` (callers must NOT guess)."""
        from notebooklm.cli import options

        ctx = type("Ctx", (), {"params": {}, "parent": None})()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTEBOOKLM_NOTEBOOK", None)
            with patch(
                "notebooklm.cli.helpers.get_current_notebook",
                return_value=None,
            ):
                nid = options._resolve_notebook_for_completion(ctx)
        assert nid is None

    def test_blank_env_falls_through(self):
        """Whitespace-only ``NOTEBOOKLM_NOTEBOOK`` is treated as unset, so a
        bare ``export NOTEBOOKLM_NOTEBOOK=`` does not block context fallback
        — same rule as ``helpers.require_notebook``.
        """
        from notebooklm.cli import options

        ctx = type("Ctx", (), {"params": {}, "parent": None})()
        with (
            patch.dict(os.environ, {"NOTEBOOKLM_NOTEBOOK": "   "}),
            patch(
                "notebooklm.cli.helpers.get_current_notebook",
                return_value="nb_from_context",
            ),
        ):
            nid = options._resolve_notebook_for_completion(ctx)
        assert nid == "nb_from_context"


# ---------------------------------------------------------------------------
# `_complete_sources` / `_complete_artifacts`
# ---------------------------------------------------------------------------


class TestCompleteSourcesAndArtifacts:
    """Sub-resource completers degrade silently when no notebook resolves."""

    def test_complete_sources_returns_empty_without_notebook(self):
        """Without a resolvable notebook the completer must return ``[]``
        instead of guessing or raising. Critical for fresh shells where the
        user has not yet run ``notebooklm use``.
        """
        from notebooklm.cli import options

        with patch.object(options, "_resolve_notebook_for_completion", return_value=None):
            items = options._complete_sources(ctx=None, param=None, incomplete="src_")

        assert items == []

    def test_complete_artifacts_returns_empty_without_notebook(self):
        """Same contract as ``_complete_sources``."""
        from notebooklm.cli import options

        with patch.object(options, "_resolve_notebook_for_completion", return_value=None):
            items = options._complete_artifacts(ctx=None, param=None, incomplete="art_")

        assert items == []

    def test_complete_sources_filters_by_prefix(self):
        """When a notebook resolves, completer lists sources and filters by
        the ``incomplete`` prefix.
        """
        from notebooklm.cli import options

        async def fake_list(_nb_id):
            return [
                _Stub("src_001", "First"),
                _Stub("src_002", "Second"),
                _Stub("src_xyz", "Other"),
            ]

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None
        fake_client.sources.list = AsyncMock(side_effect=fake_list)

        with (
            patch.object(options, "_resolve_notebook_for_completion", return_value="nb_x"),
            patch("notebooklm.cli.helpers.get_auth_tokens", return_value=object()),
            patch("notebooklm.client.NotebookLMClient", return_value=fake_client),
        ):
            items = options._complete_sources(ctx=None, param=None, incomplete="src_0")

        ids = [it.value for it in items]
        assert ids == ["src_001", "src_002"]

    def test_complete_artifacts_filters_by_prefix(self):
        """Same shape as ``test_complete_sources_filters_by_prefix`` but for
        artifacts.
        """
        from notebooklm.cli import options

        async def fake_list(_nb_id):
            return [
                _Stub("art_aaa", "Audio"),
                _Stub("art_bbb", "Video"),
                _Stub("art_aac", "Audio 2"),
            ]

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None
        fake_client.artifacts.list = AsyncMock(side_effect=fake_list)

        with (
            patch.object(options, "_resolve_notebook_for_completion", return_value="nb_x"),
            patch("notebooklm.cli.helpers.get_auth_tokens", return_value=object()),
            patch("notebooklm.client.NotebookLMClient", return_value=fake_client),
        ):
            items = options._complete_artifacts(ctx=None, param=None, incomplete="art_a")

        ids = [it.value for it in items]
        assert ids == ["art_aaa", "art_aac"]

    def test_complete_sources_swallows_listing_error(self):
        """An exception raised during ``sources.list(...)`` must NOT escape
        the completer. Without this guarantee, a transient API failure would
        dump a traceback into the user's shell on every TAB.
        """
        from notebooklm.cli import options

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None
        fake_client.sources.list = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch.object(options, "_resolve_notebook_for_completion", return_value="nb_x"),
            patch("notebooklm.cli.helpers.get_auth_tokens", return_value=object()),
            patch("notebooklm.client.NotebookLMClient", return_value=fake_client),
        ):
            items = options._complete_sources(ctx=None, param=None, incomplete="src_")

        assert items == []

    def test_complete_artifacts_swallows_listing_error(self):
        """Parity with ``test_complete_sources_swallows_listing_error`` —
        the ``-a/--artifact`` completer must also degrade silently on a
        ``artifacts.list(...)`` failure so the "never traceback during TAB"
        contract holds across BOTH sub-resource completers (CodeRabbit
        nitpick on PR #522).
        """
        from notebooklm.cli import options

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None
        fake_client.artifacts.list = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch.object(options, "_resolve_notebook_for_completion", return_value="nb_x"),
            patch("notebooklm.cli.helpers.get_auth_tokens", return_value=object()),
            patch("notebooklm.client.NotebookLMClient", return_value=fake_client),
        ):
            items = options._complete_artifacts(ctx=None, param=None, incomplete="art_")

        assert items == []


# ---------------------------------------------------------------------------
# Wiring assertions: confirm the option decorators bind the callbacks
# ---------------------------------------------------------------------------


class TestShellCompleteWiring:
    """Pin the wiring at the decorator layer so a future refactor that drops
    the ``shell_complete=`` kwarg cannot silently regress completion.
    """

    def test_notebook_option_binds_complete_notebooks(self):
        from click import Option

        from notebooklm.cli.options import _complete_notebooks, notebook_option

        @notebook_option
        def _probe(notebook_id):  # pragma: no cover — never invoked
            pass

        for param in _probe.__click_params__:  # type: ignore[attr-defined]
            if isinstance(param, Option) and "--notebook" in param.opts:
                assert param._custom_shell_complete is _complete_notebooks
                return
        raise AssertionError("notebook_option did not declare a --notebook option")

    def test_source_option_binds_complete_sources(self):
        from click import Option

        from notebooklm.cli.options import _complete_sources, source_option

        @source_option
        def _probe(source_id):  # pragma: no cover
            pass

        for param in _probe.__click_params__:  # type: ignore[attr-defined]
            if isinstance(param, Option) and "--source" in param.opts:
                assert param._custom_shell_complete is _complete_sources
                return
        raise AssertionError("source_option did not declare a --source option")

    def test_artifact_option_binds_complete_artifacts(self):
        from click import Option

        from notebooklm.cli.options import _complete_artifacts, artifact_option

        @artifact_option
        def _probe(artifact_id):  # pragma: no cover
            pass

        for param in _probe.__click_params__:  # type: ignore[attr-defined]
            if isinstance(param, Option) and "--artifact" in param.opts:
                assert param._custom_shell_complete is _complete_artifacts
                return
        raise AssertionError("artifact_option did not declare an --artifact option")
