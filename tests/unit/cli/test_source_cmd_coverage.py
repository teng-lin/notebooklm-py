"""Coverage gap tests for ``notebooklm.cli.source_cmd`` render helpers.
These exercise the small pure render/handler helpers that own the
text-vs-JSON branching and exit-code policy for ``source`` subcommands:
* ``_resolve_source_fulltext_output_path`` force/no-clobber conflict
* ``_handle_source_mutation_error`` status-message → JSON-extra vs text-hint
* ``_render_source_delete_result`` status-message emission
* ``_render_source_refresh_result`` ``result is True`` branch
* ``_exit_with_add_research_status`` JSON envelope + exit
* ``_render_add_research_result`` JSON branches for ``no_research``,
  ``failed``/``timeout``, ``unknown_status``
* ``_dispatch_source_clean_result`` text partial-failure overflow + exit
Plus one ``source add`` CLI invocation to cover the validation-error
JSON branch in the command body.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import click
import pytest

import notebooklm.auth as auth_module
from notebooklm._app.source_clean import SourceCleanResult
from notebooklm.cli import source_cmd
from notebooklm.cli.services.source_mutations import (
    SourceDeleteResult,
    SourceMutationError,
    SourceRefreshResult,
)
from notebooklm.cli.services.source_research import (
    SourceAddResearchPlan,
    SourceAddResearchResult,
)
from notebooklm.notebooklm_cli import cli

from .conftest import create_mock_client, inject_client


def _research_result(outcome: str, **kw) -> SourceAddResearchResult:
    plan = SourceAddResearchPlan(
        notebook_id="nb_1",
        query="q",
        search_source="web",
        mode="fast",
        import_all=False,
        cited_only=False,
        no_wait=False,
        timeout=60,
        json_output=True,
    )
    return SourceAddResearchResult(outcome=outcome, plan=plan, **kw)


# ---------------------------------------------------------------------------
# _resolve_source_fulltext_output_path — force + no-clobber conflict
# ---------------------------------------------------------------------------
class TestResolveFulltextOutputPath:
    def test_force_and_no_clobber_conflict_text_raises_usage_error(self, tmp_path):
        path = tmp_path / "out.md"
        path.write_text("existing", encoding="utf-8")
        with pytest.raises(click.UsageError, match="both --force and --no-clobber"):
            source_cmd._resolve_source_fulltext_output_path(
                str(path), force=True, no_clobber=True, json_output=False
            )

    def test_force_and_no_clobber_conflict_json_exits(self, tmp_path, capsys):
        path = tmp_path / "out.md"
        path.write_text("existing", encoding="utf-8")
        with pytest.raises(SystemExit):
            source_cmd._resolve_source_fulltext_output_path(
                str(path), force=True, no_clobber=True, json_output=True
            )
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# _render_source_wait_outcome — defensive unreachable guard
# ---------------------------------------------------------------------------
class TestRenderSourceWaitOutcomeGuard:
    def test_unknown_outcome_type_raises_assertion(self):
        class _Bogus:
            pass

        with pytest.raises(AssertionError, match="unreachable"):
            source_cmd._render_source_wait_outcome(_Bogus(), json_output=False)


# ---------------------------------------------------------------------------
# _handle_source_mutation_error — status-message branches
# ---------------------------------------------------------------------------
class TestHandleSourceMutationError:
    def test_status_message_routed_to_json_extra(self, capsys):
        exc = SourceMutationError(
            "boom", "DELETE_FAILED", extra={"k": "v"}, status_message="[red]bad[/red]"
        )
        with pytest.raises(SystemExit):
            source_cmd._handle_source_mutation_error(exc, json_output=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "DELETE_FAILED"
        # Markup stripped to plain text in the JSON extra.
        assert payload["status_message"] == "bad"
        assert payload["k"] == "v"

    def test_status_message_routed_to_text_hint(self, capsys):
        exc = SourceMutationError("boom", "DELETE_FAILED", status_message="[red]hint here[/red]")
        with pytest.raises(SystemExit):
            source_cmd._handle_source_mutation_error(exc, json_output=False)
        err = capsys.readouterr().err
        assert "boom" in err
        assert "hint here" in err


# ---------------------------------------------------------------------------
# _render_source_delete_result — status-message emission
# ---------------------------------------------------------------------------
class TestRenderSourceDeleteResult:
    def test_status_message_emitted_text_mode(self, capsys):
        ctx = click.Context(click.Command("x"))
        result = SourceDeleteResult(
            source_id="src_1",
            notebook_id="nb_1",
            success=True,
            status="completed",
            status_message="[dim]queued[/dim]",
        )
        source_cmd._render_source_delete_result(result, json_output=False, ctx=ctx)
        out = capsys.readouterr().out
        assert "queued" in out
        assert "Deleted source" in out


# ---------------------------------------------------------------------------
# _render_source_refresh_result — None-success branch
# ---------------------------------------------------------------------------
class TestRenderSourceRefreshResult:
    def test_result_none_prints_source_id(self, capsys):
        # v0.8.0 (#1290): refresh() returns None on success; the renderer prints
        # "Source refreshed: <source_id>" for the non-Source success path.
        ctx = click.Context(click.Command("x"))
        result = SourceRefreshResult(source_id="src_1", notebook_id="nb_1", result=None)
        source_cmd._render_source_refresh_result(result, json_output=False, ctx=ctx)
        out = capsys.readouterr().out
        assert "Source refreshed" in out
        assert "src_1" in out


# ---------------------------------------------------------------------------
# _exit_with_add_research_status
# ---------------------------------------------------------------------------
class TestExitWithAddResearchStatus:
    def test_emits_payload_and_exits_one(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            source_cmd._exit_with_add_research_status("failed", "boom", raw_status="weird")
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"status": "failed", "error": "boom", "raw_status": "weird"}


# ---------------------------------------------------------------------------
# _render_add_research_result — JSON outcome branches
# ---------------------------------------------------------------------------
class TestRenderAddResearchResultJson:
    def test_no_research_json(self, capsys):
        with pytest.raises(SystemExit):
            source_cmd._render_add_research_result(
                _research_result("no_research"), json_output=True
            )
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "no_research"

    def test_failed_json(self, capsys):
        with pytest.raises(SystemExit):
            source_cmd._render_add_research_result(_research_result("failed"), json_output=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "failed"
        assert payload["error"] == "Research failed"

    def test_timeout_json(self, capsys):
        with pytest.raises(SystemExit):
            source_cmd._render_add_research_result(_research_result("timeout"), json_output=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "timeout"
        assert payload["error"] == "Research timed out"

    def test_unknown_status_json(self, capsys):
        with pytest.raises(SystemExit):
            source_cmd._render_add_research_result(
                _research_result("unknown_status", status="weird"), json_output=True
            )
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "unknown_status"
        assert payload["raw_status"] == "weird"


class TestRenderAddResearchResultText:
    def test_no_research_text_exits(self, capsys):
        with pytest.raises(SystemExit):
            source_cmd._render_add_research_result(
                _research_result("no_research"), json_output=False
            )
        assert "Research failed to start" in capsys.readouterr().out

    def test_failed_text_exits(self, capsys):
        with pytest.raises(SystemExit):
            source_cmd._render_add_research_result(_research_result("failed"), json_output=False)
        assert "Research failed" in capsys.readouterr().out

    def test_unknown_status_text_exits(self, capsys):
        with pytest.raises(SystemExit):
            source_cmd._render_add_research_result(
                _research_result("unknown_status", status="weird"), json_output=False
            )
        assert "weird" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _dispatch_source_clean_result — text partial-failure overflow
# ---------------------------------------------------------------------------
class TestDispatchSourceCleanResultTextFailures:
    def test_text_partial_failure_overflow_and_exit(self, capsys):
        ctx = click.Context(click.Command("x"))
        failures = tuple((f"src_{i}", f"err {i}") for i in range(8))
        result = SourceCleanResult(
            notebook_id="nb_1",
            status="completed",
            candidates=(),
            deleted_count=2,
            failures=failures,
        )
        with pytest.raises(SystemExit) as exc_info:
            source_cmd._dispatch_source_clean_result(result, json_output=False, yes=True, ctx=ctx)
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        # Only the first 5 failures are listed, then an overflow summary.
        assert "and 3 more" in out
        assert "8 deletion(s) failed" in out


# ---------------------------------------------------------------------------
# source add — validation-error JSON branch in the command body
# ---------------------------------------------------------------------------
class TestSourceAddValidationErrorJson:
    def test_invalid_url_json_emits_validation_error(self, runner, mock_auth):
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "source",
                    "add",
                    "http://",  # malformed URL — no host component
                    "--type",
                    "url",
                    "-n",
                    "nb_123",
                    "--json",
                ],
                obj=inject_client(create_mock_client()),
            )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["code"] == "VALIDATION_ERROR"
