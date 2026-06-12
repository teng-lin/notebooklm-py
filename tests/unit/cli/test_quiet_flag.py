"""Tests for root ``--quiet`` flag threading across status-emitting commands.

Coverage matrix:

* The root ``--quiet`` flag is threaded into ``ctx.obj`` so any command can
  read it via the shared ``is_quiet(ctx)`` helper or via Click's root params.
* Representative status-emitting commands honor ``--quiet``:
  - ``artifact delete -y`` (acceptance criterion)
  - ``source clean -y`` (acceptance criterion)
  - ``note delete -y``
  - ``create`` (top-level notebook create)
  - ``source delete -y``
* ``--quiet`` does NOT change ``--json`` behavior: ``--quiet --json`` still
  emits the JSON payload on stdout. JSON is the deliverable, not "status".
* ``--quiet`` does NOT change ``--verbose`` behavior (the mutual-exclusion
  invariant is pinned by ``test_root_group.py``; here we pin that ``-v``
  alone keeps prose output flowing).
* Errors continue to reach stderr in ``--quiet`` mode — ``--quiet`` silences
  *status*, not *errors*.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
import notebooklm.cli.helpers as helpers_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import Artifact, Note, Notebook, Source

from .conftest import (
    create_mock_client,
    inject_client,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_notebooklm_logger():
    """Restore the package logger level after CLI invocations."""
    pkg_logger = logging.getLogger("notebooklm")
    saved = pkg_logger.level
    try:
        yield
    finally:
        pkg_logger.setLevel(saved)


@pytest.fixture
def mock_auth():
    """Auth fixture local to this file (mirrors the cli conftest pattern)."""
    with patch.object(helpers_module, "load_auth_from_storage") as mock:
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
def fetch_tokens():
    """Mock ``fetch_tokens_with_domains`` so the auth path resolves cleanly."""
    with patch.object(
        auth_module,
        "fetch_tokens_with_domains",
        new_callable=AsyncMock,
        return_value=("csrf", "session"),
    ) as mock:
        yield mock


# =============================================================================
# is_quiet() helper + ctx.obj plumbing
# =============================================================================


class TestQuietHelper:
    """``cli.runtime.is_quiet(ctx)`` is the single source of truth for whether
    the active Click invocation is in quiet mode. It must:

    1. Resolve from the *root* Click context (so subcommand contexts inherit
       the flag without re-declaring it).
    2. Default to False outside any Click context.
    3. Mirror ``ctx.obj["quiet"]`` (which the root group stamps for cheap
       lookup from contexts that prefer dict access).
    """

    def test_is_quiet_reads_root_param(self, runner):
        """``is_quiet(ctx)`` returns True when ``--quiet`` was passed."""
        import click

        from notebooklm.cli.runtime import is_quiet

        observed: dict[str, bool] = {}

        # Use a real Click context via a throw-away command registered on
        # the shared ``cli`` group. The probe is hidden + detached in the
        # ``finally`` block so it does not leak into other tests (which
        # would surface as a polluted ``--help`` output elsewhere).
        @cli.command("__probe_is_quiet__", hidden=True)
        @click.pass_context
        def _probe_cmd(ctx):
            observed["quiet"] = is_quiet(ctx)

        try:
            result = runner.invoke(cli, ["--quiet", "__probe_is_quiet__"])
            assert result.exit_code == 0, result.output
            assert observed["quiet"] is True

            observed.clear()
            result = runner.invoke(cli, ["__probe_is_quiet__"])
            assert result.exit_code == 0, result.output
            assert observed["quiet"] is False
        finally:
            # Detach the probe command so it does not leak into other tests.
            cli.commands.pop("__probe_is_quiet__", None)

    def test_is_quiet_outside_click_context_returns_false(self):
        """Calling ``is_quiet()`` with no ctx (and no active Click context)
        returns False — library importers must not see surprise suppression.
        """
        from notebooklm.cli.runtime import is_quiet

        assert is_quiet(None) is False
        assert is_quiet() is False

    def test_is_quiet_non_bool_param_degrades_false(self):
        """A malformed root param must not accidentally enable quiet mode."""
        import click

        from notebooklm.cli.runtime import is_quiet

        ctx = click.Context(click.Command("probe"))
        ctx.params["quiet"] = "false"

        assert is_quiet(ctx) is False

    def test_ctx_obj_quiet_mirrors_flag(self, runner):
        """The root group must stamp ``ctx.obj["quiet"]`` so non-runtime
        callers (which historically read ``ctx.obj``) can read it too.
        """
        import click

        observed: dict[str, object] = {}

        @cli.command("__probe_ctx_obj_quiet__", hidden=True)
        @click.pass_context
        def _probe(ctx):
            observed["quiet"] = ctx.obj.get("quiet")

        try:
            result = runner.invoke(cli, ["--quiet", "__probe_ctx_obj_quiet__"])
            assert result.exit_code == 0, result.output
            assert observed["quiet"] is True

            observed.clear()
            result = runner.invoke(cli, ["__probe_ctx_obj_quiet__"])
            assert result.exit_code == 0
            assert observed["quiet"] is False
        finally:
            cli.commands.pop("__probe_ctx_obj_quiet__", None)


# =============================================================================
# Acceptance criteria: --quiet artifact delete <id> --yes
# =============================================================================


class TestQuietArtifactDelete:
    def test_quiet_artifact_delete_emits_nothing(self, runner, mock_auth, fetch_tokens):
        """``notebooklm --quiet artifact delete <id> --yes`` exits 0 with no
        stdout and no Rich-decorated output.
        """
        mock_client = create_mock_client()
        mock_client.artifacts.list = AsyncMock(
            return_value=[Artifact(id="art_123", title="Test", _artifact_type=4, status=3)]
        )
        mock_client.mind_maps.list_note_backed = AsyncMock(return_value=[])
        mock_client.artifacts.delete = AsyncMock(return_value=None)

        result = runner.invoke(
            cli,
            ["--quiet", "artifact", "delete", "art_123", "-n", "nb_123", "-y"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0, result.output
        # ``CliRunner.output`` is stdout+stderr mixed. Quiet must suppress the
        # "Deleted artifact: ..." prose entirely.
        assert "Deleted artifact" not in result.output
        # And no ANSI/Rich color tokens leak (Rich would emit ESC[... codes
        # under a real terminal; ``CliRunner`` strips them by default but the
        # bracket-tag markup ``[green]...[/green]`` would survive if any prose
        # were emitted in the first place).
        assert "[green]" not in result.output
        assert result.output == ""

    def test_quiet_plus_json_still_emits_json(self, runner, mock_auth, fetch_tokens):
        """``--quiet --json artifact delete -y`` keeps the JSON payload — the
        JSON is the deliverable, not "status". Quiet suppresses prose; JSON
        is structured output.
        """
        mock_client = create_mock_client()
        mock_client.artifacts.list = AsyncMock(
            return_value=[Artifact(id="art_456", title="JsonTest", _artifact_type=4, status=3)]
        )
        mock_client.mind_maps.list_note_backed = AsyncMock(return_value=[])
        mock_client.artifacts.delete = AsyncMock(return_value=None)

        result = runner.invoke(
            cli,
            [
                "--quiet",
                "artifact",
                "delete",
                "art_456",
                "-n",
                "nb_123",
                "-y",
                "--json",
            ],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {"id": "art_456", "deleted": True}

    def test_non_quiet_artifact_delete_still_prints_prose(self, runner, mock_auth, fetch_tokens):
        """Baseline: without ``--quiet``, the success prose still reaches
        stdout. Pinned so the quiet plumbing cannot accidentally suppress
        the default-mode UX.
        """
        mock_client = create_mock_client()
        mock_client.artifacts.list = AsyncMock(
            return_value=[Artifact(id="art_789", title="Loud", _artifact_type=4, status=3)]
        )
        mock_client.mind_maps.list_note_backed = AsyncMock(return_value=[])
        mock_client.artifacts.delete = AsyncMock(return_value=None)

        result = runner.invoke(
            cli,
            ["artifact", "delete", "art_789", "-n", "nb_123", "-y"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0, result.output
        assert "Deleted artifact" in result.output


# =============================================================================
# Acceptance criteria: --quiet source clean --yes
# =============================================================================


class TestQuietSourceClean:
    def test_quiet_source_clean_already_clean_emits_nothing(self, runner, mock_auth, fetch_tokens):
        """``notebooklm --quiet source clean -y`` with no junk sources exits 0
        with no stdout — the "Notebook is already clean" line is status prose.
        """
        mock_client = create_mock_client()
        mock_client.sources.list = AsyncMock(return_value=[])

        result = runner.invoke(
            cli,
            ["--quiet", "source", "clean", "-n", "nb_123", "-y"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0, result.output
        assert result.output == ""

    def test_quiet_source_clean_with_junk_emits_nothing(self, runner, mock_auth, fetch_tokens):
        """With junk candidates and ``-y``, the clean still runs but produces
        no status prose (no candidate table, no "Cleaning N sources" line, no
        success line) under ``--quiet``.
        """
        # A source with status=FAILED is treated as junk by the classifier.
        junk = Source(id="src_junk_1", title="Junk Source", status=5)  # 5 = FAILED
        mock_client = create_mock_client()
        mock_client.sources.list = AsyncMock(return_value=[junk])
        mock_client.sources.delete = AsyncMock(return_value=None)

        result = runner.invoke(
            cli,
            ["--quiet", "source", "clean", "-n", "nb_123", "-y"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0, result.output
        # No candidate table or "Successfully cleaned" prose.
        assert "Cleaning" not in result.output
        assert "Successfully cleaned" not in result.output
        assert "Junk Source" not in result.output  # candidate title would only
        # appear in the candidate table, which is the prose we suppress.
        assert result.output == ""

    def test_quiet_source_clean_partial_failure_still_emits_error(
        self, runner, mock_auth, fetch_tokens
    ):
        """``--quiet`` suppresses status prose, not deletion failure diagnostics."""
        junk = Source(id="src_junk_1", title="Access Denied", url="https://example.test/a")

        async def fail_delete(_notebook_id, _source_id):
            raise RuntimeError("delete failed")

        mock_client = create_mock_client()
        mock_client.sources.list = AsyncMock(return_value=[junk])
        mock_client.sources.delete = AsyncMock(side_effect=fail_delete)

        result = runner.invoke(
            cli,
            ["--quiet", "source", "clean", "-n", "nb_123", "-y"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code != 0, result.output
        assert "1 deletion(s) failed" in result.output
        assert "src_junk_1" in result.output
        assert "delete failed" in result.output

    def test_non_quiet_source_clean_still_prints_success(self, runner, mock_auth, fetch_tokens):
        """Baseline: non-quiet ``source clean -y`` still emits the success
        line (or already-clean line)."""
        mock_client = create_mock_client()
        mock_client.sources.list = AsyncMock(return_value=[])

        result = runner.invoke(
            cli, ["source", "clean", "-n", "nb_123", "-y"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0, result.output
        assert "already clean" in result.output.lower()


# =============================================================================
# Additional representative commands (one per CLI module that emits prose)
# =============================================================================


class TestQuietRepresentativeCommands:
    """One quiet-honored assertion per CLI module that historically printed
    prose via ``console.print``. This guards against the next reach-in-print
    site silently re-emerging unprotected.
    """

    def test_quiet_source_delete(self, runner, mock_auth, fetch_tokens):
        mock_client = create_mock_client()
        mock_client.sources.list = AsyncMock(return_value=[Source(id="src_1", title="Doomed")])
        mock_client.sources.delete = AsyncMock(return_value=True)

        result = runner.invoke(
            cli,
            ["--quiet", "source", "delete", "src_1", "-n", "nb_123", "-y"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0, result.output
        assert "Deleted source" not in result.output
        assert result.output == ""

    def test_quiet_note_delete(self, runner, mock_auth, fetch_tokens):
        mock_client = create_mock_client()
        mock_client.notes.list = AsyncMock(
            return_value=[
                Note(
                    id="note_1",
                    notebook_id="nb_123",
                    title="Doomed Note",
                    content="body",
                )
            ]
        )
        mock_client.notes.delete = AsyncMock(return_value=None)

        result = runner.invoke(
            cli,
            ["--quiet", "note", "delete", "note_1", "-n", "nb_123", "-y"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0, result.output
        assert "Deleted" not in result.output
        # ``note delete`` historically emits "Deleted note: <id>" — suppressed.
        assert result.output == ""

    def test_quiet_notebook_create(self, runner, mock_auth, fetch_tokens):
        """``notebooklm --quiet create "Title"`` exits 0 with no prose."""
        new_nb = Notebook(id="nb_new", title="My Notebook")
        # ``create`` lives in cli/notebook_cmd.py; the injected factory serves it.
        mock_client = create_mock_client()
        mock_client.notebooks.create = AsyncMock(return_value=new_nb)

        result = runner.invoke(
            cli, ["--quiet", "create", "My Notebook"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0, result.output
        # Non-quiet would emit "Created notebook: nb_new" — suppressed.
        assert "Created notebook" not in result.output

    def test_non_quiet_notebook_create_still_prints(self, runner, mock_auth, fetch_tokens):
        """Baseline: ``create`` without ``--quiet`` still prints success."""
        new_nb = Notebook(id="nb_loud", title="Loud Notebook")
        mock_client = create_mock_client()
        mock_client.notebooks.create = AsyncMock(return_value=new_nb)

        result = runner.invoke(cli, ["create", "Loud Notebook"], obj=inject_client(mock_client))

        assert result.exit_code == 0, result.output
        # ``create`` emits some success prose. The exact phrasing varies; we
        # pin only that *some* non-empty stdout reaches the user.
        assert result.output.strip() != ""


# =============================================================================
# emit_status / rendering helpers honor quiet
# =============================================================================


class TestEmitStatusQuiet:
    """``rendering.emit_status`` gains a ``quiet`` keyword so any module that
    already routes through the helper inherits the suppression automatically.
    """

    def test_emit_status_quiet_true_suppresses(self):
        from io import StringIO

        from rich.console import Console

        from notebooklm.cli.rendering import emit_status

        out = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
        err = Console(file=StringIO(), stderr=True, force_terminal=False, no_color=True, width=120)

        emit_status(
            "hello",
            json_output=False,
            quiet=True,
            stdout_console=out,
            stderr_output_console=err,
        )

        assert out.file.getvalue() == ""
        assert err.file.getvalue() == ""

    def test_emit_status_quiet_false_prints(self):
        from io import StringIO

        from rich.console import Console

        from notebooklm.cli.rendering import emit_status

        out = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
        err = Console(file=StringIO(), stderr=True, force_terminal=False, no_color=True, width=120)

        emit_status(
            "hello",
            json_output=False,
            quiet=False,
            stdout_console=out,
            stderr_output_console=err,
        )

        assert "hello" in out.file.getvalue()

    def test_emit_status_inherits_root_quiet(self, runner):
        """Status helpers suppress automatically inside a quiet CLI context."""
        import click

        from notebooklm.cli.rendering import emit_status

        @cli.command("__probe_emit_status_quiet__", hidden=True)
        @click.pass_context
        def _probe(ctx):
            emit_status("should not print", json_output=False)

        try:
            result = runner.invoke(cli, ["--quiet", "__probe_emit_status_quiet__"])
            assert result.exit_code == 0, result.output
            assert result.output == ""
        finally:
            cli.commands.pop("__probe_emit_status_quiet__", None)


# =============================================================================
# Errors still reach stderr in quiet mode
# =============================================================================


class TestQuietPreservesErrors:
    """``--quiet`` silences *status*, not *errors*. Authentication failures
    and other ``_output_error`` calls must still surface to stderr so quiet
    automation still observes failures.
    """

    def test_quiet_with_missing_storage_still_errors(self, runner, tmp_path):
        """Missing storage file with ``--quiet`` still emits the auth-error
        diagnostic on stderr and exits non-zero.
        """
        missing = tmp_path / "no_such_storage.json"
        result = runner.invoke(
            cli,
            ["--storage", str(missing), "--quiet", "list"],
        )
        # Either an explicit non-zero exit OR a clean SystemExit; we only
        # require non-success.
        assert result.exit_code != 0
        # The auth-error UX writes "Not authenticated" / login hints to
        # stderr. Mixed stdout+stderr in result.output makes the check
        # straightforward.
        assert (
            "auth" in result.output.lower()
            or "not authenticated" in result.output.lower()
            or "login" in result.output.lower()
        ), (
            "Quiet mode must NOT suppress auth error diagnostics — only "
            f"status prose. Got output: {result.output!r}"
        )
