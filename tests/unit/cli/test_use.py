"""Tests for the ``notebooklm use`` command (context-setting + auth-aware errors).

This file was extracted from the legacy ``test_session.py`` as part of
D1 PR-3 (test-monkeypatch-policy migration). The shared fixtures and
helpers live in ``_session_helpers.py``; the proxy-block-aware
``patch_session_login_dual`` lives in ``tests/_fixtures``.
"""

import inspect
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

import notebooklm.auth as auth_module
import notebooklm.cli.helpers as helpers_module
import notebooklm.cli.session_cmd as session_cmd_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import Notebook

from .conftest import create_mock_client, inject_client


def _split_stream_runner() -> CliRunner:
    """Return a CliRunner whose stdout and stderr are captured separately.

    The shared ``runner`` fixture in ``conftest.py`` uses ``CliRunner()`` with
    default settings, which on Click 8.1.x means ``mix_stderr=True`` —
    ``result.stdout`` then contains a mixed stream and ``result.stderr``
    raises ``ValueError("stderr not separately captured")``. The
    ``--json`` purity test needs pure-stdout / pure-stderr access to verify
    that a diagnostic does not leak into stdout. Click 8.2+ removed
    ``mix_stderr`` entirely (streams are always separate); 8.1.x supports
    ``mix_stderr=False``. Detect via ``inspect.signature`` so this is
    portable across the project's supported Click range (``>=8.0.0,<9``).
    """
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


class TestUseCommand:
    def test_use_sets_notebook_context(self, runner, mock_auth, mock_context_file):
        """Test 'use' command sets the current notebook context."""
        mock_client = create_mock_client()
        mock_client.notebooks.get = AsyncMock(
            return_value=Notebook(
                id="nb_123",
                title="Test Notebook",
                created_at=datetime(2024, 1, 15),
                is_owner=True,
            )
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")

            # Patch in session module where it's imported
            with patch.object(
                session_cmd_module, "resolve_notebook_id", new_callable=AsyncMock
            ) as mock_resolve:
                mock_resolve.return_value = "nb_123"

                result = runner.invoke(cli, ["use", "nb_123"], obj=inject_client(mock_client))

        assert result.exit_code == 0
        assert "nb_123" in result.output or "Test Notebook" in result.output

    def test_use_with_partial_id(self, runner, mock_auth, mock_context_file):
        """Test 'use' command resolves partial notebook ID."""
        mock_client = create_mock_client()
        mock_client.notebooks.get = AsyncMock(
            return_value=Notebook(
                id="nb_full_id_123",
                title="Resolved Notebook",
                created_at=datetime(2024, 1, 15),
                is_owner=True,
            )
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")

            # Patch in session module where it's imported
            with patch.object(
                session_cmd_module, "resolve_notebook_id", new_callable=AsyncMock
            ) as mock_resolve:
                mock_resolve.return_value = "nb_full_id_123"

                result = runner.invoke(cli, ["use", "nb_full"], obj=inject_client(mock_client))

        assert result.exit_code == 0
        # Should show resolved full ID
        assert "nb_full_id_123" in result.output or "Resolved Notebook" in result.output

    def test_use_without_auth_fails_closed(self, runner, mock_context_file):
        """'use' fails closed (exit 1) when no auth is available.

        Previously, behavior persisted unverified IDs after auth failure, poisoning
        saved state for downstream commands. The new contract: refuse to write
        context.json and emit a clear "run notebooklm login" message.
        """
        with patch.object(
            helpers_module,
            "load_auth_from_storage",
            side_effect=FileNotFoundError("No auth"),
        ):
            result = runner.invoke(cli, ["use", "nb_noauth"])

        # Refuses to persist; surfaces a remediation hint.
        assert result.exit_code == 1
        assert not mock_context_file.exists()
        assert (
            "notebooklm login" in result.output.lower()
            or "authentication" in result.output.lower()
            or "--force" in result.output
        )

    def test_use_without_auth_force_persists(self, runner, mock_context_file):
        """`use --force` bypasses verification, mirrors offline/debug path."""
        with patch.object(
            helpers_module,
            "load_auth_from_storage",
            side_effect=FileNotFoundError("No auth"),
        ):
            result = runner.invoke(cli, ["use", "--force", "nb_forced"])

        assert result.exit_code == 0
        assert "nb_forced" in result.output
        assert mock_context_file.exists()
        data = json.loads(mock_context_file.read_text())
        assert data["notebook_id"] == "nb_forced"

    def test_use_shows_owner_status(self, runner, mock_auth, mock_context_file):
        """Test 'use' command displays ownership status correctly."""
        mock_client = create_mock_client()
        mock_client.notebooks.get = AsyncMock(
            return_value=Notebook(
                id="nb_shared",
                title="Shared Notebook",
                created_at=datetime(2024, 1, 15),
                is_owner=False,  # Shared notebook
            )
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")

            # Patch in session module where it's imported
            with patch.object(
                session_cmd_module, "resolve_notebook_id", new_callable=AsyncMock
            ) as mock_resolve:
                mock_resolve.return_value = "nb_shared"

                result = runner.invoke(cli, ["use", "nb_shared"], obj=inject_client(mock_client))

        assert result.exit_code == 0
        assert "Shared" in result.output or "nb_shared" in result.output


# =============================================================================
# USE COMMAND --json + auth-aware errors
# =============================================================================


class TestUseJsonOutput:
    """`notebooklm use <id> --json` emits a structured envelope with the new
    active notebook id so script and AI-agent automation does not have
    to scrape the rendered Rich table for the next-step ID.
    """

    def test_use_json_emits_active_notebook_id(self, runner, mock_auth, mock_context_file):
        """`use <id> --json` prints `{"active_notebook_id": "...", "success": true, ...}`."""
        mock_client = create_mock_client()
        mock_client.notebooks.get = AsyncMock(
            return_value=Notebook(
                id="nb_json_use",
                title="Use JSON",
                created_at=datetime(2026, 5, 14),
                is_owner=True,
            )
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            with patch.object(
                session_cmd_module, "resolve_notebook_id", new_callable=AsyncMock
            ) as mock_resolve:
                mock_resolve.return_value = "nb_json_use"

                result = runner.invoke(
                    cli, ["use", "nb_json_use", "--json"], obj=inject_client(mock_client)
                )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Stable, scriptable contract: the new active notebook id is the
        # primary signal; success boolean lets callers branch without
        # parsing the body.
        assert data["active_notebook_id"] == "nb_json_use"
        assert data["success"] is True
        # Notebook metadata is included so callers don't have to round-trip
        # to `notebooklm list` to render a confirmation.
        assert data["notebook"]["id"] == "nb_json_use"
        assert data["notebook"]["title"] == "Use JSON"
        # Context file was persisted as a side effect.
        ctx = json.loads(mock_context_file.read_text())
        assert ctx["notebook_id"] == "nb_json_use"

    def test_use_json_with_force_emits_active_notebook_id(self, runner, mock_context_file):
        """`use --force --json` skips verification but still emits the JSON envelope."""
        result = runner.invoke(cli, ["use", "--force", "nb_forced_json", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["active_notebook_id"] == "nb_forced_json"
        assert data["success"] is True
        # Mark verification status so script callers can detect unverified IDs.
        assert data.get("verified") is False
        assert mock_context_file.exists()

    def test_use_json_with_partial_id_keeps_stdout_pure(self, mock_auth, mock_context_file):
        """`use <partial-id> --json` must NOT print the "Matched: ..." diagnostic to stdout.

        Regression test for the bug where the `use` command called
        ``resolve_notebook_id(client, notebook_id)`` without forwarding
        ``json_output``, so the partial-ID-match "Matched: …" diagnostic
        line went to stdout in JSON mode and broke ``json.loads`` for
        scripted callers.

        Contract: in `--json` mode, stdout MUST be parseable JSON. The
        "Matched: …" diagnostic must route to stderr.

        Uses ``_split_stream_runner()`` instead of the shared ``runner``
        fixture because the shared one defaults to ``mix_stderr=True`` on
        Click 8.1.x — that would (a) put the diagnostic into ``stdout``
        making the bug invisible to this test, and (b) raise on
        ``result.stderr`` access. Project supports Click ``>=8.0.0,<9``.
        """
        runner = _split_stream_runner()
        full_id = "nb_partial_resolved_full_id"
        mock_client = create_mock_client()
        # Real resolver path: list() returns the candidates so the
        # prefix-match branch fires and emits "Matched: …".
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(
                    id=full_id,
                    title="Partial Match Notebook",
                    created_at=datetime(2026, 5, 21),
                    is_owner=True,
                ),
            ]
        )
        mock_client.notebooks.get = AsyncMock(
            return_value=Notebook(
                id=full_id,
                title="Partial Match Notebook",
                created_at=datetime(2026, 5, 21),
                is_owner=True,
            )
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            # NOTE: intentionally do NOT patch resolve_notebook_id —
            # we want the real partial-ID resolver to run so we can
            # verify it doesn't pollute stdout with "Matched: …".
            result = runner.invoke(
                cli, ["use", "nb_partial", "--json"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        # Hard contract: stdout (what `notebooklm use --json > out.json`
        # captures) MUST be pure parseable JSON, regardless of what
        # diagnostics get printed alongside on stderr.
        data = json.loads(result.stdout)
        assert data["active_notebook_id"] == full_id
        assert data["success"] is True
        # The diagnostic must route to stderr, not stdout.
        assert "Matched:" not in result.stdout, (
            f"`use --json` leaked partial-ID diagnostic to stdout: {result.stdout!r}"
        )
        # Sanity-check that the diagnostic DID run somewhere (otherwise
        # this test could silently regress to "resolver didn't emit at
        # all"). The "Matched: …" line should appear on stderr.
        assert "Matched:" in result.stderr, (
            f"resolver diagnostic missing from stderr — test setup may not "
            f"be exercising the partial-ID match branch: stderr={result.stderr!r}"
        )


class TestUseAuthAwareError:
    """When `notebooklm use <id>` hits an `AuthError` (e.g. expired SID
    cookies), the catch must surface the typed "run notebooklm login" UX
    from `helpers.handle_auth_error` rather than the generic "Could not
    verify ... Pass --force" catch-all.
    """

    def test_use_auth_error_suggests_notebooklm_login(self, runner, mock_auth, mock_context_file):
        """AuthError → text mode prints the typed login hint, exit 1, no persist."""
        from notebooklm.exceptions import AuthError

        mock_client = create_mock_client()
        mock_client.notebooks.get = AsyncMock(
            side_effect=AuthError("Auth expired", method_id="rwIQyf"),
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            with patch.object(
                session_cmd_module, "resolve_notebook_id", new_callable=AsyncMock
            ) as mock_resolve:
                mock_resolve.return_value = "nb_auth_expired"

                result = runner.invoke(
                    cli, ["use", "nb_auth_expired"], obj=inject_client(mock_client)
                )

        assert result.exit_code == 1
        # Fail-closed: do not poison context.json on auth expiry.
        assert not mock_context_file.exists()
        # The typed UX: explicit "notebooklm login" remediation.
        assert "notebooklm login" in result.output.lower()
        # The generic catch-all message must NOT be the one shown.
        assert "Pass --force to persist without verification" not in result.output

    def test_use_auth_error_json_emits_typed_envelope(self, runner, mock_auth, mock_context_file):
        """AuthError + --json → typed `{"code": "AUTH_REQUIRED", ...}` envelope, exit 1."""
        from notebooklm.exceptions import AuthError

        mock_client = create_mock_client()
        mock_client.notebooks.get = AsyncMock(
            side_effect=AuthError("Auth expired", method_id="rwIQyf"),
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            with patch.object(
                session_cmd_module, "resolve_notebook_id", new_callable=AsyncMock
            ) as mock_resolve:
                mock_resolve.return_value = "nb_auth_expired"

                result = runner.invoke(
                    cli, ["use", "nb_auth_expired", "--json"], obj=inject_client(mock_client)
                )

        assert result.exit_code == 1
        assert not mock_context_file.exists()
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "AUTH_REQUIRED"
        assert (
            "notebooklm login" in data["message"].lower() or "notebooklm login" in str(data).lower()
        )


# =============================================================================
# STATUS COMMAND TESTS
# =============================================================================
