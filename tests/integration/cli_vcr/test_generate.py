"""CLI integration tests for generate commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from ._fixtures import (
    ARTIFACT_NOTEBOOK_ID,
    GENERATE_NOTEBOOK_ID,
    GENERATE_PLACEHOLDER_NOTEBOOK_ID,
    GENERATE_PLACEHOLDER_SOURCE_ID,
    GENERATE_SOURCE_ID,
)
from .conftest import assert_command_success, notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestGenerateCommands:
    """Test 'notebooklm generate' commands."""

    @pytest.mark.parametrize(
        ("command", "cassette", "extra_args"),
        [
            ("quiz", "artifacts_generate_quiz.yaml", []),
            ("flashcards", "artifacts_generate_flashcards.yaml", []),
            ("report", "artifacts_generate_report.yaml", ["--format", "briefing-doc"]),
            ("report", "artifacts_generate_study_guide.yaml", ["--format", "study-guide"]),
        ],
    )
    def test_generate(self, runner, mock_auth_for_vcr, mock_context, command, cassette, extra_args):
        """Generate commands work with real client."""
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, ["generate", command, *extra_args])
            assert_command_success(result)

    def test_revise_slide(self, runner, mock_auth_for_vcr, mock_context):
        """revise-slide command sends REVISE_SLIDE RPC with correct args.

        Uses an explicit ``-n <36-char UUID>`` so ``resolve_notebook_id``
        short-circuits (its prefix-resolution path needs ``LIST_NOTEBOOKS``
        which the cassette doesn't carry). The UUID value doesn't have to
        match what was recorded — the VCR matcher only compares path +
        rpcids, not source-path query parameters. The artifact_id is
        likewise passed verbatim through the request body, which the
        matcher ignores.
        """
        with notebooklm_vcr.use_cassette("artifacts_revise_slide.yaml"):
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "revise-slide",
                    "Move the title up",
                    "-n",
                    GENERATE_PLACEHOLDER_NOTEBOOK_ID,
                    "--artifact",
                    GENERATE_PLACEHOLDER_SOURCE_ID,
                    "--slide",
                    "0",
                ],
            )
            assert_command_success(result)

    def test_mind_map(self, runner, mock_auth_for_vcr, mock_context):
        """mind-map command drives the same 3-RPC chain captured by the API tests.

        ``notebooklm generate mind-map`` calls ``client.artifacts.generate_mind_map``,
        which emits a sequential ``GENERATE_MIND_MAP`` → ``CREATE_NOTE`` →
        ``UPDATE_NOTE`` chain. The API test suite already recorded that exact chain in
        ``generate_mind_map_chain.yaml`` for the Python-API path; this CLI
        replay test **reuses** that cassette rather than re-recording.

        Reuse works because:

        - Both ``-n`` (full 36-char UUID) and ``--source`` (full 36-char UUID)
          are passed explicitly so ``resolve_notebook_id`` /
          ``resolve_source_ids`` short-circuit (the 20+ char branch in
          ``_resolve_partial_id``) and no extra ``LIST_NOTEBOOKS`` /
          ``LIST_SOURCES`` RPC enters the wire sequence.
        - The default VCR matcher only inspects ``method, scheme, host, port,
          path, rpcids`` (see ``tests/vcr_config.py``). Notebook / source IDs
          live in the URL's source-path query param and the request body, both
          of which the matcher ignores — so the CLI-side IDs can differ from
          the recorded payload without breaking replay.
        """
        with notebooklm_vcr.use_cassette("generate_mind_map_chain.yaml"):
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "mind-map",
                    "--kind",
                    "note-backed",
                    "-n",
                    GENERATE_NOTEBOOK_ID,
                    "--source",
                    GENERATE_SOURCE_ID,
                ],
            )
            assert_command_success(result)

    def test_mind_map_interactive(self, runner, mock_auth_for_vcr, mock_context, fast_sleep):
        """`generate mind-map --kind interactive` drives CREATE_ARTIFACT + poll + tree.

        Replays the recorded interactive flow (``generate_mind_map_interactive.yaml``):
        ``CREATE_ARTIFACT`` (variant 4) → ``LIST_ARTIFACTS`` poll-to-completion →
        ``GET_INTERACTIVE_HTML`` (``[0][9][3]``). ``fast_sleep`` collapses the poll
        backoff so replay is instant; ``--json`` emits the converged
        ``{mind_map, note_id, kind}`` payload with the tree inline (issue #1256).
        """
        import json

        with notebooklm_vcr.use_cassette("generate_mind_map_interactive.yaml"):
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "mind-map",
                    "--kind",
                    "interactive",
                    "--json",
                    "-n",
                    ARTIFACT_NOTEBOOK_ID,
                ],
            )
            assert_command_success(result)
        data = json.loads(result.output)
        assert data["kind"] == "interactive"
        assert isinstance(data["mind_map"], dict)
        assert "name" in data["mind_map"]  # the node tree is fetched and inlined
        assert data["note_id"]  # the interactive artifact id
