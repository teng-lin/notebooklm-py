"""CLI integration tests for generate commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

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
                    "00000000-0000-0000-0000-000000000000",
                    "--artifact",
                    "00000000-0000-0000-0000-000000000001",
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
                    "-n",
                    "bb00c9e3-656c-4fd2-b890-2b71e1cf3814",
                    "--source",
                    "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad",
                ],
            )
            assert_command_success(result)
